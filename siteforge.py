#!/usr/bin/env python3
"""
SiteForge: one local site generator for pages, a blog, a wiki and a story library.

Run it:
    python3 siteforge.py                opens the last site you used
    python3 siteforge.py ~/my-site      opens (or creates) a site in that folder
    python3 siteforge.py --help

It needs Python 3.8 or newer and nothing else (standard library only). It starts a small web
server on 127.0.0.1 and opens the editor in your browser. The editor uses JavaScript. The site
it builds is HTML and CSS; the only script it can emit is the wiki's optional search.js.

A site always has pages. On top of that you can switch on any of three modules:
    Blog      posts, drafts, tags, a feed page and RSS
    Wiki      sections of articles, a sidebar with dropdowns, search, [[wiki links]], callouts
    Library   short stories and serials with chapters, covers, genres and reading typography
The homepage is either a normal page or the landing page of one module.

Folder layout (inside the site folder you choose):
    index.html, about.html, ...     pages (and the homepage)
    blog/                           posts, tag pages, blog/rss.xml
    wiki/                           wiki sections and articles, wiki/search.js
    stories/                        stories, chapters, genre pages, stories/rss.xml
    rss, rss.xml                    one feed of new posts and chapters, when feeds are on
    style.css                       your stylesheet (edited through "Edit CSS", never overwritten)
    assets/                         images you add
    .siteforge/                     your markdown sources and settings

The wiki or the library can instead be mounted at the site root when it is the homepage, which
reproduces the URLs WikiGen and StoryGen used. Existing SiteGen, WikiGen and StoryGen folders can
be imported from the Site structure view.

Do not hand-edit the generated .html files; they are rewritten on every save.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import email.utils
import ftplib
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import traceback
import unicodedata
import webbrowser
from html import escape as _escape, unescape as _unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

APP = "SiteForge"
META_DIR = ".siteforge"
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.RLock()
CURRENT = {"site": None}
SERVER = {"httpd": None, "port": 0}
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "siteforge"
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_UPLOAD = 25 * 1024 * 1024
SEARCH_TEXT_LIMIT = 8000
MODULES = ("blog", "wiki", "library")
MODULE_NAMES = {"blog": "Blog", "wiki": "Wiki", "library": "Stories"}
MOUNT_DEFAULT = {"blog": "blog", "wiki": "wiki", "library": "stories"}
HOME_MODES = ("page",) + MODULES
# Folder names a page, wiki section or story can never take, because something else lives there.
RESERVED = {"index", "style", "assets", "blog", "wiki", "stories", "genre", "tag", "posts", "rss", "sitemap",
            "robots", "search", "site", "api", "404"}
CALLOUTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "WARNING": "Warning", "CAUTION": "Caution"}
STATUSES = [("draft", "Draft"), ("ongoing", "Ongoing"), ("hiatus", "On hiatus"), ("complete", "Complete")]
STATUS_LABELS = dict(STATUSES)
SEARCH_MODES = ("builtin", "web", "off")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def esc(s):
    return _escape(s, quote=False)


def attr(s):
    return _escape(s, quote=True)


_ENTITY_AMP = re.compile(r"&(?!#?\w+;)")


def esc_text(s):
    """Escape text but leave existing entities such as &copy; alone."""
    return _ENTITY_AMP.sub("&amp;", s).replace("<", "&lt;").replace(">", "&gt;")


def slugify(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:80].strip("-")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def clean_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10]).isoformat()
    except (TypeError, ValueError):
        return datetime.date.today().isoformat()


def nice_date(s):
    d = datetime.date.fromisoformat(clean_date(s))
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def fix_url(url, prefix):
    """Relative URLs written in markdown are relative to the site root.
    Prefix them so they work from pages that live in sub folders."""
    url = str(url).strip()
    if not url:
        return "#"
    low = re.sub(r"[\s\x00-\x1f]+", "", url.lower())
    if low.startswith(("javascript:", "vbscript:", "data:text")):
        return "#"
    if _SCHEME.match(url) or url.startswith(("#", "/", "//", "?")):
        return url
    while url.startswith("./"):
        url = url[2:]
    return prefix + url


def abs_url(site_url, rel):
    """Join a site's base URL with a site-root-relative path. Empty base -> empty (no absolute URL possible)."""
    site_url = str(site_url or "").strip()
    if not site_url:
        return ""
    return site_url.rstrip("/") + "/" + str(rel).lstrip("/")


def resolve_img(site, rel):
    """Absolute URL for a social image path. Relative paths need the site URL; only http(s) absolutes are kept."""
    rel = str(rel or "").strip()
    if not rel:
        return ""
    if _SCHEME.match(rel):
        return rel if re.match(r"^https?://", rel, re.I) else ""
    return abs_url(site.get("url", ""), rel)


# ----------------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------------

BLOCK_TAGS = set(
    "div p figure figcaption details summary section article aside header footer nav table "
    "thead tbody tr td th ul ol li dl dt dd h1 h2 h3 h4 h5 h6 blockquote hr iframe video audio "
    "picture source form fieldset legend label input textarea select option button center address".split()
)
INLINE_TAGS = set(
    "a abbr b bdi bdo br cite code data del dfn em i img ins kbd mark q s samp small span strong "
    "sub sup time u var wbr".split()
)

LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*).*$")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
RAW_RE = re.compile(r"^\s{0,3}<(!--|/?([a-zA-Z][\w-]*))")
# Raw HTML inside markdown is passed through an allowlist sanitizer built on html.parser.
# The parser decodes entities in attribute values the same way a browser does, so tricks such as
# href="&#106;avascript:..." are caught. Tags outside the allowlist are dropped (script and style
# lose their contents too), event handler attributes are removed and URLs are checked by scheme.
_SAFE_TAGS = BLOCK_TAGS | INLINE_TAGS | set(
    "pre caption colgroup col tfoot track main small".split()
)
_DROP_CONTENT = {"script", "style", "template", "noscript"}
_URL_ATTRS = {"href", "src", "poster", "action", "formaction", "cite", "data", "background", "longdesc"}
_SAFE_SCHEMES = ("http:", "https:", "mailto:", "tel:")
_ATTR_NAME = re.compile(r"^[a-zA-Z_:][-a-zA-Z0-9_:.]*$")
_BAD_CSS = re.compile(r"expression|javascript|vbscript|behavior|-moz-binding|@import", re.I)


def safe_url(value, prefix, attr_name="href"):
    """Return a URL that is safe to emit, or '#'. Relative URLs get the page prefix."""
    v = str(value or "").strip()
    compact = re.sub(r"[\s\x00-\x1f\x7f]+", "", v).lower()
    if not compact:
        return "#" if attr_name == "href" else ""
    if _SCHEME.match(compact):
        if compact.startswith(_SAFE_SCHEMES):
            return v
        if attr_name in ("src", "poster") and re.match(r"^data:image/(png|jpe?g|gif|webp|avif);", compact):
            return v
        return "#" if attr_name in ("href", "action") else ""
    return fix_url(v, prefix)


def _safe_srcset(value, prefix):
    out = []
    for cand in str(value).split(","):
        bits = cand.strip().split()
        if not bits:
            continue
        url = safe_url(bits[0], prefix, "src")
        if not url or url == "#":
            return ""
        out.append(" ".join([url] + bits[1:]))
    return ", ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self, prefix):
        super().__init__(convert_charrefs=True)
        self.prefix = prefix
        self.out = []
        self.skip = 0

    def _attrs(self, attrs):
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            if not _ATTR_NAME.match(name) or name.startswith("on") or name in ("srcdoc", "formaction"):
                continue
            if value is None:
                parts.append(" " + name)
                continue
            if name in _URL_ATTRS or name.endswith(":href"):
                value = safe_url(value, self.prefix, name)
                if not value:
                    continue
            elif name == "srcset":
                value = _safe_srcset(value, self.prefix)
                if not value:
                    continue
            elif name == "style":
                if _BAD_CSS.search(re.sub(r"/\*.*?\*/|\\", "", value, flags=re.S)):
                    continue
            parts.append(' %s="%s"' % (name, attr(value)))
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._attrs(attrs)))

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._attrs(attrs)))

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(esc(data))

    # comments, doctype, processing instructions and CDATA are dropped
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitize_html(s, prefix=""):
    p = _Sanitizer(prefix)
    try:
        p.feed(s)
        p.close()
    except Exception:  # malformed input: fall back to showing it as text
        return esc(s)
    return "".join(p.out)


class Markdown:
    """Markdown subset renderer. resolver(target) maps a [[wiki link]] target to a site-root
    relative URL, or None when no article matches."""

    def __init__(self, prefix="", resolver=None):
        self.prefix = prefix
        self.resolver = resolver
        self.ids = {}
        self.headings = []
        self.missing = []

    # ---- entry points
    def render(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").expandtabs(4)
        return self.blocks(text.split("\n"))

    def raw(self, s):
        return sanitize_html(s, self.prefix)

    # ---- blocks
    def unique_id(self, text):
        base = slugify(re.sub(r"<[^>]+>", "", text)) or "section"
        n = self.ids.get(base, 0)
        self.ids[base] = n + 1
        return base if n == 0 else "%s-%d" % (base, n)

    def starts_block(self, line, para=False):
        if FENCE_RE.match(line) or HEAD_RE.match(line) or HR_RE.match(line) or QUOTE_RE.match(line):
            return True
        m = LIST_RE.match(line)
        if m:
            if not para:
                return True
            marker = m.group(2)
            return (not marker[0].isdigit()) or marker[:-1] == "1"
        m = RAW_RE.match(line)
        if m and (m.group(1) == "!--" or (m.group(2) or "").lower() in BLOCK_TAGS):
            return True
        return False

    def blocks(self, lines):
        out = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = FENCE_RE.match(line)
            if m:
                fence, lang = m.group(1), m.group(2)
                i += 1
                buf = []
                while i < n:
                    s = lines[i].strip()
                    if s.startswith(fence) and set(s) <= set(fence[0]):
                        break
                    buf.append(lines[i])
                    i += 1
                i += 1
                cls = ' class="language-%s"' % attr(lang) if lang else ""
                out.append("<pre><code%s>%s</code></pre>" % (cls, esc("\n".join(buf))))
                continue

            m = HEAD_RE.match(line)
            if m:
                level, text = len(m.group(1)), m.group(2)
                hid, inner = self.unique_id(text), self.inline(text)
                if level in (2, 3):
                    self.headings.append((level, hid, re.sub(r"<[^>]+>", "", inner)))
                anchor = ""
                if 2 <= level <= 4:
                    anchor = ' <a class="anchor" href="#%s" aria-label="Link to this section">#</a>' % hid
                out.append('<h%d id="%s">%s%s</h%d>' % (level, hid, inner, anchor, level))
                i += 1
                continue

            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if QUOTE_RE.match(line):
                buf = []
                while i < n and QUOTE_RE.match(lines[i]):
                    buf.append(QUOTE_RE.match(lines[i]).group(1))
                    i += 1
                out.append(self.quote(buf))
                continue

            if LIST_RE.match(line):
                html_, i = self.list_block(lines, i)
                out.append(html_)
                continue

            m = RAW_RE.match(line)
            if m and (m.group(1) == "!--" or (m.group(2) or "").lower() in BLOCK_TAGS):
                buf = []
                while i < n and lines[i].strip():
                    buf.append(lines[i])
                    i += 1
                out.append(self.raw("\n".join(buf)))
                continue

            if "|" in line and i + 1 < n and SEP_RE.match(lines[i + 1]):
                html_, i = self.table(lines, i)
                out.append(html_)
                continue

            buf = []
            while i < n and lines[i].strip() and (not buf or not self.starts_block(lines[i], True)):
                buf.append(lines[i].lstrip())
                i += 1
            text = "\n".join(buf).strip()
            html_ = self.inline(text)
            html_ = re.sub(r"(?: {2,}|\\)\n", "<br>\n", html_)
            out.append("<p>%s</p>" % html_)
        return "\n".join(out)

    def quote(self, buf):
        """Blockquotes, plus GitHub style callouts: a first line of [!NOTE], [!TIP], [!IMPORTANT],
        [!WARNING] or [!CAUTION] turns the quote into a styled box."""
        m = re.match(r"^\s*\[!(\w+)\]\s*(.*)$", buf[0]) if buf else None
        if m and m.group(1).upper() in CALLOUTS:
            kind = m.group(1).upper()
            label = m.group(2).strip() or CALLOUTS[kind]
            body = self.blocks(buf[1:])
            return '<div class="callout callout-%s">\n<p class="callout-title">%s</p>\n%s\n</div>' % (
                kind.lower(), self.inline(label), body)
        return "<blockquote>\n%s\n</blockquote>" % self.blocks(buf)

    def list_block(self, lines, i):
        n = len(lines)
        m = LIST_RE.match(lines[i])
        base = len(m.group(1))
        ordered = m.group(2)[0].isdigit()
        start = int(m.group(2)[:-1]) if ordered else 1
        items = []
        loose = False

        def is_sibling(mm):
            return bool(mm) and len(mm.group(1)) == base and mm.group(2)[0].isdigit() == ordered

        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j >= n:
                    break
                nxt = lines[j]
                ind = len(nxt) - len(nxt.lstrip())
                if is_sibling(LIST_RE.match(nxt)):
                    loose = True
                    i = j
                    continue
                if ind > base and items:
                    items[-1]["lines"].extend([""] * (j - i))
                    i = j
                    continue
                break
            m = LIST_RE.match(line)
            ind = len(line) - len(line.lstrip())
            if is_sibling(m):
                items.append({"first": m.group(3), "width": base + len(m.group(2)) + 1, "lines": []})
                i += 1
                continue
            if ind > base and items:
                strip = min(ind, items[-1]["width"])
                items[-1]["lines"].append(line[strip:])
                i += 1
                continue
            if m:
                break
            if items and lines[i - 1].strip() and not self.starts_block(line, True):
                items[-1]["lines"].append(line.lstrip())
                i += 1
                continue
            break

        if any("" in it["lines"] for it in items):
            loose = True
        rendered = []
        for it in items:
            content = [it["first"]] + it["lines"]
            task, cls = "", ""
            tm = re.match(r"^\[( |x|X)\]\s+(.*)$", content[0])
            if tm:
                task = '<input type="checkbox" disabled%s> ' % (" checked" if tm.group(1) != " " else "")
                content[0] = tm.group(2)
                cls = ' class="task-item"'
            inner = self.blocks(content)
            if not loose:
                inner = re.sub(r"^<p>(.*?)</p>", r"\1", inner, count=1, flags=re.S)
            rendered.append("<li%s>%s%s</li>" % (cls, task, inner))
        tag = "ol" if ordered else "ul"
        start_attr = ' start="%d"' % start if ordered and start != 1 else ""
        return "<%s%s>\n%s\n</%s>" % (tag, start_attr, "\n".join(rendered), tag), i

    def table(self, lines, i):
        def cells(row):
            row = row.strip()
            if row.startswith("|"):
                row = row[1:]
            if row.endswith("|") and not row.endswith("\\|"):
                row = row[:-1]
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]

        head = cells(lines[i])
        aligns = []
        for c in cells(lines[i + 1]):
            left, right = c.startswith(":"), c.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left" if left else "")
        i += 2
        rows = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(cells(lines[i]))
            i += 1

        def cell(tag, text, k):
            a = aligns[k] if k < len(aligns) else ""
            style = ' style="text-align:%s"' % a if a else ""
            return "<%s%s>%s</%s>" % (tag, style, self.inline(text), tag)

        out = ['<div class="table-wrap">', "<table>", "<thead>"]
        out.append("<tr>" + "".join(cell("th", c, k) for k, c in enumerate(head)) + "</tr>")
        out += ["</thead>", "<tbody>"]
        for r in rows:
            out.append("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(r)) + "</tr>")
        out += ["</tbody>", "</table>", "</div>"]
        return "\n".join(out), i

    # ---- inline
    def inline(self, text):
        stash = []
        out = self._inline(text.replace("\x00", ""), stash)
        for _ in range(10):
            if "\x00" not in out:
                break
            out = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)
        return out

    def _inline(self, text, stash):
        def keep(s):
            stash.append(s)
            return "\x00%d\x00" % (len(stash) - 1)

        # code spans
        text = re.sub(r"(`+)(.+?)\1", lambda m: keep("<code>%s</code>" % esc(m.group(2).strip())), text, flags=re.S)
        # backslash escapes
        text = re.sub(r"\\([\\`*_{}\[\]()#+.!|~<>-])", lambda m: keep(esc(m.group(1))), text)

        # wiki links: [[Article]], [[Article|label]], [[Section/Article]], [[Article#heading]]
        if self.resolver is not None:
            def wiki(m):
                target, _, label = m.group(1).partition("|")
                target = target.strip()
                page, _, frag = target.partition("#")
                label = label.strip() or page.strip().split("/")[-1].strip() or target
                url = self.resolver(page) if page.strip() else ""
                if url is None:
                    self.missing.append(page.strip())
                    return keep('<span class="missing-link" title="No article called %s yet">%s</span>'
                                % (attr(page.strip()), esc(label)))
                href = (self.prefix + url if url else "") + ("#" + slugify(frag) if frag.strip() else "")
                return keep('<a class="wiki-link" href="%s">%s</a>' % (attr(href or "#"), esc(label)))

            text = re.sub(r"\[\[([^\[\]\n]+?)\]\]", wiki, text)
        # autolinks
        text = re.sub(
            r"<((?:https?://|mailto:)[^\s<>]+)>",
            lambda m: keep('<a href="%s">%s</a>' % (attr(m.group(1)), esc(m.group(1)))),
            text,
        )

        # inline html (whitelisted tags only)
        def tag(m):
            if m.group(1).lower() in INLINE_TAGS:
                return keep(self.raw(m.group(0)))
            return m.group(0)

        text = re.sub(r"</?([a-zA-Z][\w-]*)(?:\s[^<>]*)?/?>", tag, text)

        # images
        def img(m):
            alt = re.sub(r"\x00\d+\x00", "", m.group(1))
            title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
            return keep('<img src="%s" alt="%s"%s loading="lazy">' % (attr(fix_url(m.group(2), self.prefix)), attr(alt), title))

        text = re.sub(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)', img, text)

        # links
        def link(m):
            title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
            inner = self._inline(m.group(1), stash)
            return keep('<a href="%s"%s>%s</a>' % (attr(fix_url(m.group(2), self.prefix)), title, inner))

        text = re.sub(r'\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)', link, text)

        text = esc_text(text)
        text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", text, flags=re.S)
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"\*(?=[^\s*])(.+?)(?<=[^\s*])\*", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.S)
        return text


def auto_summary(html_):
    m = re.search(r"<p>(.*?)</p>", html_, re.S)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", _unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    return t if len(t) <= 200 else t[:197].rsplit(" ", 1)[0] + "..."


# ----------------------------------------------------------------------------
# Themes and CSS
#
# Every theme from SiteGen, WikiGen and StoryGen is kept. They used three different sets of
# CSS variables, so each is converted to one shared set here: the colors and fonts each theme
# defined are carried over exactly, and variables a theme never had (a sidebar color for a
# blog theme, say) are derived from its own colors.
# ----------------------------------------------------------------------------

UNI_ORDER = [
    "bg", "bg-image", "surface", "surface-opacity", "text", "muted", "accent", "border", "code-bg",
    "header-bg", "header-text", "sidebar-bg", "sidebar-text", "sidebar-active",
    "font-body", "font-heading", "font-ui", "font-mono",
    "base-size", "line-height", "max-width", "measure", "sidebar-width", "radius",
    "heading-weight", "heading-transform", "letter-spacing", "dropcap-size",
]

THEMES = {
    "site-clean": ("Clean light", "Site", {"bg": "#f6f7f9", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#1f2328", "muted": "#59636e", "accent": "#0b5fd6", "border": "#d5dbe3", "code-bg": "#f6f7f9", "header-bg": "#ffffff", "header-text": "#1f2328", "sidebar-bg": "#f6f7f9", "sidebar-text": "#1f2328", "sidebar-active": "#d5dbe3", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.65", "max-width": "46rem", "measure": "46rem", "sidebar-width": "16rem", "radius": "8px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "site-midnight": ("Midnight", "Site", {"bg": "#0e1116", "bg-image": "none", "surface": "#161b22", "surface-opacity": "1", "text": "#e6edf3", "muted": "#9aa4b2", "accent": "#6cb0ff", "border": "#2b3340", "code-bg": "#0e1116", "header-bg": "#161b22", "header-text": "#e6edf3", "sidebar-bg": "#0e1116", "sidebar-text": "#e6edf3", "sidebar-active": "#2b3340", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.65", "max-width": "46rem", "measure": "46rem", "sidebar-width": "16rem", "radius": "8px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "site-terminal": ("Terminal", "Site", {"bg": "#020403", "bg-image": "none", "surface": "#06100a", "surface-opacity": "0.88", "text": "#3dff7a", "muted": "#22b555", "accent": "#b6ffcb", "border": "#146b31", "code-bg": "#020403", "header-bg": "#06100a", "header-text": "#3dff7a", "sidebar-bg": "#020403", "sidebar-text": "#3dff7a", "sidebar-active": "#146b31", "font-body": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-heading": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-ui": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.7", "max-width": "52rem", "measure": "52rem", "sidebar-width": "16rem", "radius": "0px", "heading-weight": "700", "heading-transform": "uppercase", "letter-spacing": "0.05em", "dropcap-size": "3.1em"}),
    "site-paper": ("Paper", "Site", {"bg": "#e9e6dc", "bg-image": "none", "surface": "#f7f4ea", "surface-opacity": "1", "text": "#2b2a26", "muted": "#6d6a5e", "accent": "#1d5c63", "border": "#cfcabb", "code-bg": "#e9e6dc", "header-bg": "#f7f4ea", "header-text": "#2b2a26", "sidebar-bg": "#e9e6dc", "sidebar-text": "#2b2a26", "sidebar-active": "#cfcabb", "font-body": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "19px", "line-height": "1.75", "max-width": "42rem", "measure": "42rem", "sidebar-width": "16rem", "radius": "3px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "site-nord": ("Nord", "Site", {"bg": "#2e3440", "bg-image": "none", "surface": "#3b4252", "surface-opacity": "1", "text": "#eceff4", "muted": "#a7b1c4", "accent": "#88c0d0", "border": "#4c566a", "code-bg": "#2e3440", "header-bg": "#3b4252", "header-text": "#eceff4", "sidebar-bg": "#2e3440", "sidebar-text": "#eceff4", "sidebar-active": "#4c566a", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.65", "max-width": "46rem", "measure": "46rem", "sidebar-width": "16rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "site-neon": ("Neon night", "Site", {"bg": "#0b0016", "bg-image": "none", "surface": "#150029", "surface-opacity": "0.9", "text": "#f3e8ff", "muted": "#b491ff", "accent": "#19f0ff", "border": "#6a1fd0", "code-bg": "#0b0016", "header-bg": "#150029", "header-text": "#f3e8ff", "sidebar-bg": "#0b0016", "sidebar-text": "#f3e8ff", "sidebar-active": "#6a1fd0", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.65", "max-width": "46rem", "measure": "46rem", "sidebar-width": "16rem", "radius": "2px", "heading-weight": "700", "heading-transform": "uppercase", "letter-spacing": "0.06em", "dropcap-size": "3.1em"}),
    "site-solarized": ("Solarized light", "Site", {"bg": "#fdf6e3", "bg-image": "none", "surface": "#fffbee", "surface-opacity": "1", "text": "#4b5f66", "muted": "#7a8b90", "accent": "#1f78b4", "border": "#e6dcbc", "code-bg": "#fdf6e3", "header-bg": "#fffbee", "header-text": "#4b5f66", "sidebar-bg": "#fdf6e3", "sidebar-text": "#4b5f66", "sidebar-active": "#e6dcbc", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.65", "max-width": "46rem", "measure": "46rem", "sidebar-width": "16rem", "radius": "8px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "site-minimal": ("Minimal serif", "Site", {"bg": "#ffffff", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#151515", "muted": "#666666", "accent": "#151515", "border": "#e2e2e2", "code-bg": "#ffffff", "header-bg": "#ffffff", "header-text": "#151515", "sidebar-bg": "#ffffff", "sidebar-text": "#151515", "sidebar-active": "#e2e2e2", "font-body": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "19px", "line-height": "1.75", "max-width": "38rem", "measure": "38rem", "sidebar-width": "16rem", "radius": "0px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-clean": ("Clean light", "Wiki", {"bg": "#f6f7f9", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#1f2328", "muted": "#5b6470", "accent": "#0b5fd6", "border": "#d8dde4", "code-bg": "#f2f4f7", "header-bg": "#ffffff", "header-text": "#1f2328", "sidebar-bg": "#f0f2f5", "sidebar-text": "#1f2328", "sidebar-active": "#e1e7f0", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-classic": ("Classic wiki", "Wiki", {"bg": "#f8f9fa", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#202122", "muted": "#54595d", "accent": "#3366cc", "border": "#c8ccd1", "code-bg": "#f8f9fa", "header-bg": "#ffffff", "header-text": "#202122", "sidebar-bg": "#f8f9fa", "sidebar-text": "#202122", "sidebar-active": "#eaecf0", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "2px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-slate": ("Slate dark", "Wiki", {"bg": "#0f141b", "bg-image": "none", "surface": "#151b24", "surface-opacity": "1", "text": "#dde3ea", "muted": "#95a1b1", "accent": "#7aa7ff", "border": "#263041", "code-bg": "#0b1017", "header-bg": "#111720", "header-text": "#e8edf3", "sidebar-bg": "#111720", "sidebar-text": "#cfd7e2", "sidebar-active": "#1d2635", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-nord": ("Nord", "Wiki", {"bg": "#2e3440", "bg-image": "none", "surface": "#3b4252", "surface-opacity": "1", "text": "#eceff4", "muted": "#b0b8c8", "accent": "#88c0d0", "border": "#4c566a", "code-bg": "#2e3440", "header-bg": "#2e3440", "header-text": "#eceff4", "sidebar-bg": "#353c4a", "sidebar-text": "#e5e9f0", "sidebar-active": "#434c5e", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-gruvbox": ("Gruvbox dark", "Wiki", {"bg": "#1d2021", "bg-image": "none", "surface": "#282828", "surface-opacity": "1", "text": "#ebdbb2", "muted": "#b0a391", "accent": "#fabd2f", "border": "#3c3836", "code-bg": "#1d2021", "header-bg": "#1d2021", "header-text": "#ebdbb2", "sidebar-bg": "#202324", "sidebar-text": "#d5c4a1", "sidebar-active": "#3c3836", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "3px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-dracula": ("Dracula", "Wiki", {"bg": "#21222c", "bg-image": "none", "surface": "#282a36", "surface-opacity": "1", "text": "#f8f8f2", "muted": "#a4abcc", "accent": "#bd93f9", "border": "#3a3d4e", "code-bg": "#1e1f29", "header-bg": "#21222c", "header-text": "#f8f8f2", "sidebar-bg": "#242631", "sidebar-text": "#e6e6f0", "sidebar-active": "#363949", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-solarized": ("Solarized light", "Wiki", {"bg": "#fdf6e3", "bg-image": "none", "surface": "#fffbef", "surface-opacity": "1", "text": "#3c4d54", "muted": "#687b82", "accent": "#1f6fb2", "border": "#e4dcc2", "code-bg": "#f5eed8", "header-bg": "#fdf6e3", "header-text": "#3c4d54", "sidebar-bg": "#f6efd9", "sidebar-text": "#3c4d54", "sidebar-active": "#ebe2c4", "font-body": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-paper": ("Paper", "Wiki", {"bg": "#ebe7dc", "bg-image": "none", "surface": "#f8f5ec", "surface-opacity": "1", "text": "#2b2a26", "muted": "#66635a", "accent": "#1d5c63", "border": "#d3cdbd", "code-bg": "#efeadf", "header-bg": "#f8f5ec", "header-text": "#2b2a26", "sidebar-bg": "#efeadf", "sidebar-text": "#2b2a26", "sidebar-active": "#e1dbc9", "font-body": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "17px", "line-height": "1.7", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "6px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "wiki-terminal": ("Terminal", "Wiki", {"bg": "#050805", "bg-image": "none", "surface": "#0a100a", "surface-opacity": "1", "text": "#3dff7a", "muted": "#2fc862", "accent": "#b6ffcb", "border": "#145c2c", "code-bg": "#030503", "header-bg": "#050805", "header-text": "#3dff7a", "sidebar-bg": "#070b07", "sidebar-text": "#3dff7a", "sidebar-active": "#0f2414", "font-body": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-heading": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-ui": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "15px", "line-height": "1.65", "max-width": "48rem", "measure": "36rem", "sidebar-width": "17rem", "radius": "0px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-paperback": ("Paperback", "Reading", {"bg": "#f3f1ec", "bg-image": "none", "surface": "#fffdf8", "surface-opacity": "1", "text": "#23201b", "muted": "#6b655c", "accent": "#8a5a2b", "border": "#ddd6c8", "code-bg": "#efeadf", "header-bg": "#fffdf8", "header-text": "#23201b", "sidebar-bg": "#f3f1ec", "sidebar-text": "#3a352d", "sidebar-active": "#e6e0d2", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-night": ("Night reading", "Reading", {"bg": "#14161a", "bg-image": "none", "surface": "#1a1d22", "surface-opacity": "1", "text": "#ddd8cf", "muted": "#9a948a", "accent": "#d3a06a", "border": "#2c3139", "code-bg": "#101317", "header-bg": "#14161a", "header-text": "#ddd8cf", "sidebar-bg": "#171a1f", "sidebar-text": "#c9c3ba", "sidebar-active": "#232830", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-parchment": ("Parchment", "Reading", {"bg": "#e7dec7", "bg-image": "none", "surface": "#f6efdc", "surface-opacity": "1", "text": "#2f2a20", "muted": "#6d6552", "accent": "#7a5230", "border": "#d0c4a4", "code-bg": "#e9e0c6", "header-bg": "#f6efdc", "header-text": "#2f2a20", "sidebar-bg": "#ebe2cc", "sidebar-text": "#3a3427", "sidebar-active": "#ded3b6", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "19px", "line-height": "1.8", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-manuscript": ("Manuscript", "Reading", {"bg": "#ffffff", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#111111", "muted": "#555555", "accent": "#222222", "border": "#cccccc", "code-bg": "#f4f4f4", "header-bg": "#ffffff", "header-text": "#111111", "sidebar-bg": "#f4f4f4", "sidebar-text": "#222222", "sidebar-active": "#e6e6e6", "font-body": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-heading": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "16px", "line-height": "2", "max-width": "44rem", "measure": "36rem", "sidebar-width": "16rem", "radius": "0px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "1em"}),
    "book-ink": ("Ink on white", "Reading", {"bg": "#ffffff", "bg-image": "none", "surface": "#ffffff", "surface-opacity": "1", "text": "#16181d", "muted": "#5f646e", "accent": "#1b3f8b", "border": "#e2e4e8", "code-bg": "#f4f5f7", "header-bg": "#ffffff", "header-text": "#16181d", "sidebar-bg": "#f7f8fa", "sidebar-text": "#31353d", "sidebar-active": "#eaecf1", "font-body": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "2px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-midnight": ("Midnight blue", "Reading", {"bg": "#0f1420", "bg-image": "none", "surface": "#161d2b", "surface-opacity": "1", "text": "#e3e8f2", "muted": "#98a2b6", "accent": "#8fb3ff", "border": "#27324a", "code-bg": "#0b101a", "header-bg": "#0f1420", "header-text": "#e3e8f2", "sidebar-bg": "#131a28", "sidebar-text": "#ccd4e3", "sidebar-active": "#1f2839", "font-body": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-sepia": ("Sepia", "Reading", {"bg": "#efe3d0", "bg-image": "none", "surface": "#f9f0e1", "surface-opacity": "1", "text": "#3b2f22", "muted": "#7b6a55", "accent": "#9a4f24", "border": "#dac9ae", "code-bg": "#eadcc4", "header-bg": "#f9f0e1", "header-text": "#3b2f22", "sidebar-bg": "#ecdfca", "sidebar-text": "#453726", "sidebar-active": "#e0cfb2", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "Georgia, \"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Times New Roman\", serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-noir": ("Noir", "Reading", {"bg": "#0c0c0d", "bg-image": "none", "surface": "#141415", "surface-opacity": "1", "text": "#e8e6e3", "muted": "#8e8b86", "accent": "#c3453b", "border": "#27272a", "code-bg": "#0a0a0b", "header-bg": "#0c0c0d", "header-text": "#e8e6e3", "sidebar-bg": "#0f0f10", "sidebar-text": "#cfcdc9", "sidebar-active": "#1d1d20", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "0px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.1em"}),
    "book-pulp": ("Pulp", "Reading", {"bg": "#1b1712", "bg-image": "none", "surface": "#241e17", "surface-opacity": "1", "text": "#f2e6d2", "muted": "#b3a48c", "accent": "#e0a63c", "border": "#3a3126", "code-bg": "#171310", "header-bg": "#1b1712", "header-text": "#f2e6d2", "sidebar-bg": "#1f1a14", "sidebar-text": "#e2d6c0", "sidebar-active": "#312819", "font-body": "\"Iowan Old Style\", \"Palatino Linotype\", Palatino, \"Book Antiqua\", Georgia, serif", "font-heading": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-ui": "system-ui, -apple-system, \"Segoe UI\", Roboto, \"Helvetica Neue\", Arial, sans-serif", "font-mono": "ui-monospace, \"SF Mono\", \"Cascadia Code\", Menlo, Consolas, \"DejaVu Sans Mono\", monospace", "base-size": "18px", "line-height": "1.75", "max-width": "44rem", "measure": "34rem", "sidebar-width": "16rem", "radius": "4px", "heading-weight": "700", "heading-transform": "none", "letter-spacing": "0", "dropcap-size": "3.4em"}),
}
DEFAULT_THEME = {"page": "site-clean", "blog": "site-clean", "wiki": "wiki-classic", "library": "book-paperback"}


def root_block(theme_id):
    label, group, values = THEMES[theme_id]
    lines = [":root {", "  /* Theme: %s (%s). Change any value below. */" % (label, group)]
    for k in UNI_ORDER:
        lines.append("  --%s: %s;" % (k, values[k]))
    lines.append("}")
    return "\n".join(lines)


CSS_HEADER = """/*
  SiteForge stylesheet

  TOP:     theme variables (colors, fonts, sizes). They apply to every page.
  MIDDLE:  base styles: header, pages, blog, wiki, library.
  BOTTOM:  your rules. Every page has body classes, so a rule can target one page, one
           module or one wiki section. See the list at the start of that part.

  SiteForge never overwrites this file. "Apply theme" only replaces the :root block.
*/

/* ============ GLOBAL THEME (every page) ============ */
"""

BASE_CSS = r"""
/* ============ GLOBAL BASE STYLES (every page) ============ */
*, *::before, *::after { box-sizing: border-box; }
html { font-size: var(--base-size); -webkit-text-size-adjust: 100%; }
body {
  margin: 0; color: var(--text); font-family: var(--font-body); line-height: var(--line-height);
  background-color: var(--bg); background-image: var(--bg-image); background-size: cover; background-attachment: fixed;
}
a { color: var(--accent); text-underline-offset: 0.18em; }
a:focus-visible, summary:focus-visible, input:focus-visible, label:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
h1, h2, h3, h4, h5, h6 {
  font-family: var(--font-heading); font-weight: var(--heading-weight); line-height: 1.25;
  text-transform: var(--heading-transform); letter-spacing: var(--letter-spacing);
}
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 1rem; top: 1rem; z-index: 20; padding: 0.5rem 0.75rem; background: var(--surface); color: var(--text); }
.wrap { max-width: var(--max-width); margin: 0 auto; padding: 0 1.25rem; }
.page-main { padding-top: 2rem; padding-bottom: 3rem; }
.empty { color: var(--muted); }

/* header and navigation */
.nav-toggle, .side-toggle { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.site-header { background: var(--header-bg); color: var(--header-text); border-bottom: 1px solid var(--border); }
.site-header .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem 1.5rem; padding: 0.85rem 1.25rem; }
.brand { display: flex; flex-direction: column; }
.site-title { font-family: var(--font-heading); font-weight: var(--heading-weight); font-size: 1.35rem; color: var(--header-text); text-decoration: none; letter-spacing: var(--letter-spacing); }
.tagline { color: var(--muted); font-size: 0.85rem; font-family: var(--font-ui); }
.site-nav { margin-left: auto; font-family: var(--font-ui); font-size: 0.95rem; }
.site-nav ul { display: flex; flex-wrap: wrap; gap: 0.25rem 1.1rem; list-style: none; margin: 0; padding: 0; }
.site-nav a { color: var(--header-text); text-decoration: none; border-bottom: 2px solid transparent; padding: 0.15rem 0; }
.site-nav a:hover { border-bottom-color: var(--border); }
.site-nav a[aria-current="page"] { color: var(--accent); border-bottom-color: var(--accent); }
.nav-button, .side-button {
  display: none; cursor: pointer; user-select: none; font-family: var(--font-ui); font-size: 0.85rem;
  padding: 0.15rem 0.7rem; border: 1px solid var(--border); border-radius: var(--radius);
}
.nav-toggle:focus-visible + .site-header .nav-button { outline: 2px solid var(--accent); }
.site-footer { border-top: 1px solid var(--border); color: var(--muted); font-family: var(--font-ui); font-size: 0.85rem; }
.site-footer .bar { padding: 1.25rem; }
.site-footer p { margin: 0; }

/* content card, used by pages, posts and wiki articles */
.content {
  background: var(--surface);
  background: color-mix(in srgb, var(--surface) calc(var(--surface-opacity) * 100%), transparent);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 1.75rem clamp(1rem, 3vw, 2.25rem);
}
.content > :last-child { margin-bottom: 0; }
.content h1 { font-size: 2rem; margin: 0 0 1rem; }
.content h2 { font-size: 1.45rem; margin: 2rem 0 0.75rem; }
.content h3 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
.content h4, .content h5, .content h6 { font-size: 1rem; margin: 1.25rem 0 0.5rem; }
.content p, .content ul, .content ol, .content dl { margin: 0 0 1rem; }
.content li + li { margin-top: 0.2rem; }
.content img, .content video, .content iframe { max-width: 100%; height: auto; border-radius: calc(var(--radius) / 2); }
.content blockquote { margin: 0 0 1rem; padding: 0.1rem 1rem; border-left: 3px solid var(--border); color: var(--muted); }
.content code, .prose code { font-family: var(--font-mono); font-size: 0.88em; padding: 0.1em 0.35em; background: var(--code-bg); border: 1px solid var(--border); border-radius: 4px; }
.content pre, .prose pre {
  overflow-x: auto; margin: 0 0 1rem; padding: 0.9rem 1rem; line-height: 1.5; font-size: 0.9rem;
  background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius);
}
.content pre code, .prose pre code { padding: 0; border: 0; background: none; font-size: inherit; }
.content kbd { font-family: var(--font-mono); font-size: 0.82em; padding: 0.05em 0.4em; border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 4px; background: var(--code-bg); }
.table-wrap { overflow-x: auto; margin: 0 0 1rem; }
.content table, .prose table { border-collapse: collapse; min-width: 100%; }
.content th, .content td, .prose th, .prose td { border: 1px solid var(--border); padding: 0.4rem 0.7rem; text-align: left; vertical-align: top; }
.content th { background: var(--code-bg); }
.content hr { border: 0; border-top: 1px solid var(--border); margin: 2rem 0; }
.task-item { list-style: none; margin-left: -1.25rem; }
.task-item input { margin-right: 0.5rem; }
.page-title { margin: 0 0 1rem; }
.anchor { margin-left: 0.35rem; font-weight: 400; color: var(--muted); text-decoration: none; opacity: 0; }
.content h2:hover .anchor, .content h3:hover .anchor, .content h4:hover .anchor, .anchor:focus { opacity: 1; }

/* callouts: > [!NOTE], [!TIP], [!IMPORTANT], [!WARNING], [!CAUTION] */
.callout { --c: #2f6fdb; margin: 0 0 1rem; padding: 0.65rem 1rem; border-left: 4px solid var(--c); border-radius: 0 var(--radius) var(--radius) 0; background: var(--code-bg); }
.callout-tip { --c: #1f8a4c; }
.callout-important { --c: #8250df; }
.callout-warning { --c: #c27c0e; }
.callout-caution { --c: #d13b3b; }
.content .callout-title, .prose .callout-title { margin: 0 0 0.3rem; font-weight: 700; color: var(--c); text-indent: 0; }
.callout > :last-child { margin-bottom: 0; }

/* ============ BLOG ============ */
.post-meta { color: var(--muted); font-size: 0.9rem; font-family: var(--font-ui); margin: -0.5rem 0 1rem; }
.post-list { list-style: none; margin: 0; padding: 0; }
.post-item { padding: 1.1rem 0; border-top: 1px solid var(--border); }
.post-item:first-child { border-top: 0; padding-top: 0.25rem; }
.content .post-item h2 { font-size: 1.3rem; margin: 0 0 0.35rem; }
.post-item h2 a { text-decoration: none; }
.post-item .post-meta { margin: 0 0 0.35rem; }
.content .post-summary { margin: 0.35rem 0 0; color: var(--muted); }
.tags { display: flex; flex-wrap: wrap; gap: 0.35rem; list-style: none; margin: 0 0 0.75rem; padding: 0; font-family: var(--font-ui); font-size: 0.78rem; }
.content .tags li + li { margin-top: 0; }
.tags a { display: inline-block; padding: 0.05rem 0.55rem; border: 1px solid var(--border); border-radius: 99px; text-decoration: none; color: var(--muted); }
.tags a:hover { color: var(--accent); border-color: var(--accent); }
.post-back, .tag-back, .feed-links { font-family: var(--font-ui); font-size: 0.9rem; }

/* ============ WIKI and chapter layouts: sidebar on the left ============ */
.with-sidebar { display: grid; grid-template-columns: var(--sidebar-width) minmax(0, 1fr); align-items: start; }
.side {
  position: sticky; top: 0; max-height: 100vh; overflow-y: auto; padding: 1.25rem 0.85rem 2rem;
  min-height: calc(100vh - 4rem); background: var(--sidebar-bg); color: var(--sidebar-text);
  border-right: 1px solid var(--border); font-family: var(--font-ui); font-size: 0.92rem;
}
.side ul, .side ol { list-style: none; margin: 0; padding: 0; }
.side a { display: block; padding: 0.25rem 0.6rem; border-radius: calc(var(--radius) - 1px); color: var(--sidebar-text); text-decoration: none; }
.side a:hover { background: var(--sidebar-active); }
.side a[aria-current="page"] { background: var(--sidebar-active); color: var(--accent); font-weight: 600; }
.with-sidebar > main { min-width: 0; padding: 1.5rem clamp(1rem, 4vw, 3rem) 3rem; }
.with-sidebar .content { max-width: calc(var(--max-width) + 8rem); }
.nav-home { margin: 0 0 0.6rem; }
.nav-empty { color: var(--muted); padding: 0 0.6rem; font-size: 0.85rem; }
.nav-section { margin: 0.1rem 0; }
.nav-section > summary {
  display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.6rem; cursor: pointer;
  list-style: none; font-weight: 600; border-radius: calc(var(--radius) - 1px);
}
.nav-section > summary::-webkit-details-marker { display: none; }
.nav-section > summary::before {
  content: ""; flex: none; width: 0.42em; height: 0.42em; opacity: 0.7;
  border-right: 2px solid currentColor; border-bottom: 2px solid currentColor; transform: rotate(-45deg); transition: transform 0.15s;
}
.nav-section[open] > summary::before { transform: rotate(45deg); }
.nav-section > summary:hover { background: var(--sidebar-active); }
.nav-count { margin-left: auto; font-weight: 400; font-size: 0.78rem; color: var(--muted); }
.nav-section ul { margin: 0.1rem 0 0.5rem 0.95rem; padding-left: 0.4rem; border-left: 1px solid var(--border); }
.search { position: relative; margin: 0 0 1rem; }
.search input {
  width: 100%; padding: 0.45rem 0.65rem; font: inherit; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
}
.search-results { margin: 0.35rem 0 0; padding: 0.25rem; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 6px 18px rgba(0, 0, 0, 0.14); }
.side .search-results a { padding: 0.4rem 0.5rem; }
.search-results a.active { background: var(--sidebar-active); }
.search-results .r-title { display: block; font-weight: 600; color: var(--text); }
.search-results .r-sec, .search-results .r-snip { display: block; font-size: 0.78rem; color: var(--muted); }
.search-results mark { background: rgba(255, 200, 0, 0.35); color: inherit; border-radius: 2px; }
.search-empty { padding: 0.4rem 0.5rem; color: var(--muted); font-size: 0.85rem; }
.breadcrumbs ol { display: flex; flex-wrap: wrap; gap: 0.35rem; list-style: none; margin: 0 0 1rem; padding: 0; font-family: var(--font-ui); font-size: 0.85rem; color: var(--muted); }
.breadcrumbs li + li::before { content: "/"; margin-right: 0.35rem; opacity: 0.6; }
.content .breadcrumbs li + li { margin-top: 0; }
.lead { color: var(--muted); font-size: 1.05rem; margin-top: -0.25rem; }
.wiki-toc { margin: 0 0 1.5rem; padding: 0.6rem 1rem; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); font-family: var(--font-ui); font-size: 0.92rem; }
.wiki-toc summary { cursor: pointer; font-weight: 600; }
.wiki-toc ol { margin: 0.4rem 0 0.2rem; padding-left: 1.3rem; }
.wiki-toc ol ol { margin: 0.1rem 0; }
.missing-link { color: #d33a3a; border-bottom: 1px dashed currentColor; cursor: help; }
.article-meta { margin: 2rem 0 0; padding-top: 0.75rem; border-top: 1px solid var(--border); color: var(--muted); font-family: var(--font-ui); font-size: 0.85rem; }
.pager { display: flex; gap: 1rem; margin-top: 1.5rem; font-family: var(--font-ui); font-size: 0.9rem; }
.pager a { flex: 1 1 0; padding: 0.65rem 0.85rem; border: 1px solid var(--border); border-radius: var(--radius); text-decoration: none; color: var(--text); }
.pager a:hover { border-color: var(--accent); }
.pager .next { text-align: right; margin-left: auto; }
.pager small { display: block; color: var(--muted); font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; }
.wiki-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 1rem; margin: 0 0 1.5rem; }
.wiki-card { padding: 1rem 1.1rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg); }
.content .wiki-card h3 { margin: 0 0 0.35rem; font-size: 1.1rem; }
.wiki-card h3 a { text-decoration: none; }
.content .wiki-card p { margin: 0 0 0.5rem; color: var(--muted); font-size: 0.9rem; }
.content .wiki-card ul { margin: 0; padding-left: 1.1rem; font-size: 0.92rem; }
.wiki-card .more { display: inline-block; margin-top: 0.4rem; font-size: 0.85rem; }
.content .article-list { list-style: none; margin: 0.5rem 0 1rem; padding: 0; }
.article-list li { padding: 0.65rem 0; border-top: 1px solid var(--border); }
.content .article-list li + li { margin-top: 0; }
.article-list li:first-child { border-top: 0; }
.article-list a { font-weight: 600; }
.content .article-list p { margin: 0.15rem 0 0; color: var(--muted); font-size: 0.92rem; }
.article-list .when { display: block; color: var(--muted); font-size: 0.8rem; font-family: var(--font-ui); }

/* ============ LIBRARY: stories and chapters ============ */
.wide { max-width: calc(var(--max-width) + 16rem); margin: 0 auto; padding: 2rem 1.25rem 4rem; }
.sheet { max-width: var(--measure); margin: 0 auto; }
.chapters-nav .in-story { display: block; font-family: var(--font-heading); font-size: 1.05rem; font-weight: 700; padding: 0 0.6rem; margin-bottom: 0.15rem; }
.chapters-nav .by { display: block; color: var(--muted); font-size: 0.8rem; margin: 0 0.6rem 0.9rem; }
.chapters-nav ol { counter-reset: ch; }
.chapters-nav li { counter-increment: ch; }
.chapters-nav li a { display: flex; gap: 0.5rem; }
.chapters-nav li a::before { content: counter(ch); color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.3em; text-align: right; }
.chapters-nav .all-stories { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.82rem; }
.prose { font-size: 1rem; }
.prose p { margin: 0 0 1.15rem; }
body.prose-indented .prose p + p { margin-top: -1.15rem; text-indent: 1.6em; }
body.prose-indented .prose blockquote p + p, body.prose-indented .prose li p + p { margin-top: 0; text-indent: 0; }
.prose h2, .prose h3 { margin: 2.2rem 0 0.9rem; }
.prose h2 { font-size: 1.35rem; }
.prose h3 { font-size: 1.1rem; }
.prose blockquote { margin: 1.5rem; padding: 0; color: var(--muted); font-style: italic; }
.prose hr { border: 0; margin: 2rem 0; text-align: center; }
.prose hr::after { content: "* * *"; letter-spacing: 0.6em; color: var(--muted); font-size: 0.9rem; }
.prose img { max-width: 100%; height: auto; border-radius: var(--radius); }
.prose ul, .prose ol { margin: 0 0 1.15rem; padding-left: 1.4rem; }
body.dropcap .prose.opening > p:first-of-type::first-letter {
  float: left; font-family: var(--font-heading); font-size: var(--dropcap-size); line-height: 0.82;
  padding: 0.08em 0.08em 0 0; color: var(--accent);
}
body.dropcap .prose.opening > p:first-of-type { text-indent: 0; }
.chapter-head { margin-bottom: 2.5rem; text-align: center; }
.chapter-head .in { display: block; font-family: var(--font-ui); font-size: 0.78rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); }
.chapter-head .in a { color: inherit; text-decoration: none; }
.chapter-head h1 { font-size: 1.8rem; margin: 0.6rem 0 0.4rem; }
.chapter-head .meta { font-family: var(--font-ui); font-size: 0.8rem; color: var(--muted); margin: 0; }
.note { margin: 2.5rem 0 0; padding: 1rem 1.15rem; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); font-size: 0.92rem; }
.note h2 { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin: 0 0 0.5rem; }
.note p { margin: 0 0 0.6rem; text-indent: 0 !important; }
.note > :last-child { margin-bottom: 0; }
.the-end { margin: 3rem 0 0; text-align: center; font-family: var(--font-ui); font-size: 0.78rem; letter-spacing: 0.25em; text-transform: uppercase; color: var(--muted); }
.story-head { display: flex; flex-wrap: wrap; gap: 1.5rem; margin-bottom: 2rem; }
.story-head .cover { flex: 0 0 auto; width: 11rem; max-width: 40%; border: 1px solid var(--border); border-radius: var(--radius); }
.story-head .about { flex: 1 1 18rem; min-width: 0; }
.story-head h1 { font-size: 2rem; margin: 0 0 0.2rem; line-height: 1.15; }
.subtitle { font-family: var(--font-heading); font-style: italic; color: var(--muted); margin: 0 0 0.5rem; }
.byline { font-family: var(--font-ui); font-size: 0.9rem; color: var(--muted); margin: 0 0 0.75rem; }
.facts { display: flex; flex-wrap: wrap; gap: 0.4rem 0.75rem; list-style: none; margin: 0 0 1rem; padding: 0; font-family: var(--font-ui); font-size: 0.8rem; color: var(--muted); }
.facts .tag { display: inline-block; padding: 0.05rem 0.6rem; border: 1px solid var(--border); border-radius: 99px; text-decoration: none; color: var(--muted); }
.facts .tag:hover { color: var(--accent); border-color: var(--accent); }
.facts .status { border-color: var(--accent); color: var(--accent); }
.blurb { margin: 0 0 1rem; }
.warnings { font-family: var(--font-ui); font-size: 0.85rem; color: var(--muted); margin: 0 0 1rem; }
.read-button { display: inline-block; padding: 0.55rem 1.4rem; background: var(--accent); color: var(--surface); border-radius: var(--radius); font-family: var(--font-ui); font-size: 0.92rem; text-decoration: none; }
.chapter-list { margin: 2.5rem 0 0; }
.chapter-list h2 { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 0 0 0.75rem; }
.chapter-list ol { list-style: none; margin: 0; padding: 0; counter-reset: ch; }
.chapter-list li { counter-increment: ch; border-top: 1px solid var(--border); }
.chapter-list li:last-child { border-bottom: 1px solid var(--border); }
.chapter-list a { display: flex; align-items: baseline; gap: 0.75rem; padding: 0.65rem 0.25rem; text-decoration: none; color: var(--text); }
.chapter-list a:hover { background: var(--sidebar-active); }
.chapter-list a::before { content: counter(ch); color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.6em; text-align: right; font-family: var(--font-ui); font-size: 0.85rem; }
.chapter-list .ch-title { flex: 1; }
.chapter-list .ch-meta { font-family: var(--font-ui); font-size: 0.78rem; color: var(--muted); white-space: nowrap; }
.shelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr)); gap: 1.75rem; margin: 1.25rem 0 0; }
.book { display: flex; gap: 1rem; }
.book > a { flex: 0 0 auto; text-decoration: none; }
.book .cover { width: 7.5rem; border: 1px solid var(--border); border-radius: var(--radius); }
.book .cover.none { display: flex; align-items: center; justify-content: center; aspect-ratio: 2 / 3; background: var(--code-bg); color: var(--muted); font-family: var(--font-ui); font-size: 0.72rem; text-align: center; padding: 0.5rem; }
.book h2 { font-size: 1.15rem; margin: 0 0 0.15rem; }
.book h2 a { color: var(--text); text-decoration: none; }
.book h2 a:hover { color: var(--accent); }
.book p { margin: 0 0 0.5rem; font-size: 0.92rem; }
.section-title { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 2.5rem 0 0; }
.updates { list-style: none; margin: 1rem 0 0; padding: 0; }
.updates li { padding: 0.55rem 0; border-top: 1px solid var(--border); font-size: 0.95rem; }
.updates .when { display: block; font-family: var(--font-ui); font-size: 0.78rem; color: var(--muted); }

/* narrow screens: menus fold behind buttons (pure CSS, no script) */
@media (max-width: 820px) {
  .nav-button { display: inline-block; margin-left: auto; }
  .site-nav { margin-left: 0; width: 100%; display: none; }
  .nav-toggle:checked ~ .site-header .site-nav { display: block; }
  .with-sidebar { display: block; }
  .side-button { display: inline-block; margin: 0.75rem 1rem 0; }
  .side { display: none; position: static; max-height: none; min-height: 0; border-right: 0; border-bottom: 1px solid var(--border); }
  .side-toggle:checked ~ .with-sidebar .side { display: block; }
  .with-sidebar > main { padding: 1rem 0.75rem 2rem; }
  .story-head .cover { width: 8rem; }
}
@media print {
  .site-header, .site-footer, .side, .pager, .nav-button, .side-button, .anchor { display: none !important; }
  .with-sidebar { display: block; }
  .content { border: 0; padding: 0; background: none; }
}
"""

PAGE_MARK = "/* ============ PAGE SPECIFIC RULES ============ */"
PAGE_HELP = """/*
  Body classes you can target:
    A page:                  body.page-SLUG            (home page: body.page-index)
    The blog:                body.mod-blog             feed: body.blog-feed, post: body.blog-post, tags: body.blog-tag
    The wiki:                body.mod-wiki             one section: body.sec-SLUG, one article: body.art-SECTION-SLUG
    The library:             body.mod-library          story page: body.lib-story, chapter: body.lib-chapter
    One story and chapters:  body.story-SLUG
    Whatever the homepage is: body.is-home
  Example: body.mod-wiki .content h2 { color: var(--accent); }
*/
"""


def css_stub(label, cls):
    return "\n/* --- %s (body.%s) --- */\nbody.%s {\n}\n" % (label.replace("*/", ""), cls, cls)


def default_css(theme="site-clean"):
    return CSS_HEADER + root_block(theme) + "\n" + BASE_CSS + "\n" + PAGE_MARK + "\n" + PAGE_HELP


# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------

def today():
    return datetime.date.today().isoformat()


def up(rel):
    """Relative path from a generated file back to the site root."""
    return "../" * rel.count("/")


def page_rel(slug):
    return "index.html" if slug == "index" else slug + ".html"


def title_from_name(name):
    t = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", str(name))).strip()
    return (t[:1].upper() + t[1:]) if t else "Untitled"


def first_h1(text):
    for line in text.splitlines()[:30]:
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        return m.group(1).strip() if m else ""
    return ""


def strip_title_h1(text, title):
    """Drop a leading '# Title' line that repeats the title, since the page prints its own."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        if m and m.group(1).strip().lower() == title.strip().lower():
            return "\n".join(lines[i + 1:])
        return text
    return text


def file_date(path):
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return today()


def rename_path(src, dst):
    """Rename that also works for case-only changes on case-insensitive file systems."""
    if src == dst:
        return
    tmp = src.with_name(src.name + ".sf-rename")
    os.replace(src, tmp)
    os.replace(tmp, dst)


def plain_text(html_):
    t = re.sub(r'<a class="anchor"[^>]*>#</a>', "", html_)
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _unescape(t)).strip()


def write_if_changed(path, text):
    if path.is_file() and read_text(path) == text:
        return
    write_text(path, text)


def clean_tags(raw, known=None, limit=12):
    """Comma separated tags, de-duplicated without regard to case. An existing spelling wins."""
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    known_by = {slugify(k): k for k in (known or [])}
    out, seen = [], set()
    for t in items:
        t = re.sub(r"\s+", " ", str(t)).strip(" ,")[:40]
        key = slugify(t)
        if not t or not key or key in seen:
            continue
        seen.add(key)
        out.append(known_by.get(key, t))
    return out[:limit]


def seo_fields(d, target):
    """Copy the optional per-item SEO fields from a request onto a page, post or story."""
    if "seo_title" in d:
        target["seo_title"] = str(d.get("seo_title") or "").strip()[:120]
    if "description" in d:
        target["description"] = str(d.get("description") or "").strip()[:320]
    if "image" in d:
        target["image"] = str(d.get("image") or "").strip()[:400]
    if "canonical" in d:
        c = str(d.get("canonical") or "").strip()[:400]
        if c and not re.match(r"^https?://", c, re.I):
            raise ApiError("A canonical URL must start with http:// or https://, or be left blank.")
        target["canonical"] = c
    if "noindex" in d:
        target["noindex"] = bool(d.get("noindex"))
    return target


def reading_time(words, wpm):
    mins = max(1, int(round(words / float(max(60, wpm)))))
    if mins < 60:
        return "%d min read" % mins
    h, m = divmod(mins, 60)
    return "%dh %02dm read" % (h, m)


def nice_words(n):
    if n >= 1000:
        return "{:,} words".format(int(round(n / 100.0) * 100))
    return "1 word" if n == 1 else "%d words" % n


HOME_MD = """# Welcome

This is the home page. Edit it with **Home page** in the editor.
"""

ABOUT_MD = """A few lines about you, or about this site.
"""

PRIVACY_MD = """This site is a set of static pages. It does not use cookies, analytics, or tracking scripts,
and it does not ask for or store personal information.

The server that hosts it keeps standard access logs (IP address, time, page requested and browser
type), as nearly every web server does. Those logs are used only to keep the site running and
are not shared.

If you email me, I use your address only to reply.

*Last updated {date}.*
"""

BLOG_INTRO_MD = ""
WIKI_HOME_MD = """# Wiki

Pick a section from the sidebar, or search for a topic.
"""
LIB_HOME_MD = ""


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

SITE_DEFAULTS = {
    "title": "My site", "tagline": "", "author": "", "footer": "&copy; {year} {title}", "lang": "en", "url": "",
    "description": "", "image": "", "twitter": "", "noindex": False, "sitemap": False, "feeds": False,
    # wiki
    "search": "builtin", "toc": True, "updated": True, "expand": False, "wiki_cards": True, "wiki_recent": True,
    # library
    "prose": "indented", "dropcap": True, "wordcount": True, "reading_time": True, "wpm": 240, "lib_updates": True,
    # blog
    "post_nav": True,
}
SITE_BOOLS = ("noindex", "sitemap", "feeds", "toc", "updated", "expand", "wiki_cards", "wiki_recent", "dropcap",
              "wordcount", "reading_time", "lib_updates", "post_nav")


def default_cfg(title):
    site = dict(SITE_DEFAULTS)
    site["title"] = title or "My site"
    return {
        "version": 1,
        "site": site,
        "home": "page",
        "modules": {m: {"enabled": False, "label": MODULE_NAMES[m], "mount": MOUNT_DEFAULT[m]} for m in MODULES},
        "pages": [{"slug": "index", "title": "Home", "show_title": False}],
        "nav": [{"type": "page", "slug": "index", "label": "", "visible": True}],
        "posts": [],
        "wiki": {"sections": []},
        "library": {"stories": []},
        "deploy": {},
        "generated": [],
    }


# ----------------------------------------------------------------------------
# The site: shared parts. The blog, wiki and library are mixed in below.
# ----------------------------------------------------------------------------

class SiteBase:
    def __init__(self, root):
        self.root = root
        self.meta = root / META_DIR
        self.cfg = None
        self._mds = []
        self._res = (None, None)

    # ---- paths
    def page_md(self, slug):
        return self.meta / "pages" / (slug + ".md")

    def css_path(self):
        return self.root / "style.css"

    # ---- load and save
    def load(self):
        f = self.meta / "config.json"
        try:
            self.cfg = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ApiError("The settings file %s is not valid JSON (%s). Fix or remove it." % (f, e))
        (self.root / "assets").mkdir(parents=True, exist_ok=True)
        if not self.css_path().exists():
            write_text(self.css_path(), default_css())
        self.normalize()
        self.rescan()
        self.save_cfg()
        self.build()

    def create(self, setup):
        """First run: make a new site with the homepage and modules chosen in the setup screen."""
        home = setup.get("home") if setup.get("home") in HOME_MODES else "page"
        self.cfg = default_cfg(str(setup.get("title") or "").strip()[:120] or title_from_name(self.root.name))
        theme = setup.get("theme") if setup.get("theme") in THEMES else None
        write_text(self.page_md("index"), HOME_MD)
        (self.root / "assets").mkdir(parents=True, exist_ok=True)
        if not self.css_path().exists():
            write_text(self.css_path(), default_css(theme or DEFAULT_THEME.get(home, "site-clean")))
        wanted = set(m for m in (setup.get("modules") or []) if m in MODULES)
        if home in MODULES:
            wanted.add(home)
        for m in MODULES:
            if m in wanted:
                self.enable_module(m)
        self.cfg["home"] = home
        if home in ("wiki", "library") and setup.get("root_mount"):
            self.cfg["modules"][home]["mount"] = ""
        self.normalize()
        self.save_cfg()
        self.build()

    def save_cfg(self):
        write_text(self.meta / "config.json", json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n")

    def normalize(self):
        c = self.cfg if isinstance(self.cfg, dict) else {}
        self.cfg = c
        d = default_cfg("")
        if not isinstance(c.get("site"), dict):
            c["site"] = {}
        for k, v in SITE_DEFAULTS.items():
            c["site"].setdefault(k, v)
        if not c["site"].get("title"):
            c["site"]["title"] = title_from_name(self.root.name)
        s = c["site"]
        if s.get("prose") not in ("indented", "spaced"):
            s["prose"] = "indented"
        if s.get("search") not in SEARCH_MODES:
            s["search"] = "builtin"
        try:
            s["wpm"] = max(60, min(1000, int(s.get("wpm") or 240)))
        except (TypeError, ValueError):
            s["wpm"] = 240
        mods = c.get("modules") if isinstance(c.get("modules"), dict) else {}
        for m in MODULES:
            cur = mods.get(m) if isinstance(mods.get(m), dict) else {}
            mods[m] = {
                "enabled": bool(cur.get("enabled")),
                "label": str(cur.get("label") or MODULE_NAMES[m])[:40],
                "mount": cur.get("mount") if cur.get("mount") in ("", MOUNT_DEFAULT[m]) else MOUNT_DEFAULT[m],
            }
        c["modules"] = mods
        if c.get("home") not in HOME_MODES or (c["home"] in MODULES and not mods[c["home"]]["enabled"]):
            c["home"] = "page"
        for m in MODULES:
            if m == "blog" or c["home"] != m:
                mods[m]["mount"] = MOUNT_DEFAULT[m]
        pages, seen = [], set()
        for p in c.get("pages") if isinstance(c.get("pages"), list) else []:
            if not isinstance(p, dict) or not slugify(p.get("slug", "")) or p["slug"] in seen:
                continue
            p["slug"] = "index" if p["slug"] == "index" else slugify(p["slug"])
            seen.add(p["slug"])
            p["title"] = str(p.get("title") or title_from_name(p["slug"]))
            p["show_title"] = bool(p.get("show_title", p["slug"] != "index"))
            pages.append(p)
        if "index" not in seen:
            pages.insert(0, dict(d["pages"][0]))
        c["pages"] = pages
        for k in ("posts", "generated"):
            if not isinstance(c.get(k), list):
                c[k] = []
        for k, sub in (("wiki", "sections"), ("library", "stories")):
            if not isinstance(c.get(k), dict) or not isinstance(c[k].get(sub), list):
                c[k] = {sub: []}
        if not isinstance(c.get("deploy"), dict):
            c["deploy"] = {}
        self.normalize_blog()
        self.normalize_wiki()
        self.normalize_library()
        self.normalize_nav()

    def normalize_nav(self):
        c = self.cfg
        nav, seen = [], set()
        slugs = {p["slug"] for p in c["pages"]}
        for n in c.get("nav") if isinstance(c.get("nav"), list) else []:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            vis = bool(n.get("visible", True))
            label = str(n.get("label") or "")[:60]
            if t == "page" and n.get("slug") in slugs and ("p", n["slug"]) not in seen:
                seen.add(("p", n["slug"]))
                nav.append({"type": "page", "slug": n["slug"], "label": label, "visible": vis})
            elif t == "module" and n.get("id") in MODULES and ("m", n["id"]) not in seen:
                seen.add(("m", n["id"]))
                nav.append({"type": "module", "id": n["id"], "label": label, "visible": vis})
            elif t == "link" and str(n.get("url") or "").strip():
                nav.append({"type": "link", "label": label or "Link", "url": str(n["url"]).strip()[:400], "visible": vis})
        for p in c["pages"]:
            if ("p", p["slug"]) not in seen:
                nav.append({"type": "page", "slug": p["slug"], "label": "", "visible": True})
        for m in MODULES:
            if ("m", m) not in seen and c["modules"][m]["enabled"]:
                nav.append({"type": "module", "id": m, "label": "", "visible": True})
        c["nav"] = nav

    def rescan(self):
        r = {}
        if self.enabled("wiki"):
            r.update(self.wiki_rescan())
        if self.enabled("library"):
            r.update(self.lib_rescan())
        return r

    def api_rescan(self):
        r = self.rescan()
        self.save_cfg()
        self.build()
        return r

    def state(self):
        c = self.cfg
        return {
            "folder": str(self.root),
            "site": c["site"],
            "home": c["home"],
            "modules": c["modules"],
            "pages": c["pages"],
            "nav": c["nav"],
            "posts": c["posts"],
            "tags": sorted({t for p in c["posts"] for t in p.get("tags", [])}, key=str.lower),
            "wiki": c["wiki"],
            "library": c["library"],
            "genres": sorted({g for s in c["library"]["stories"] for g in s.get("genres", [])}, key=str.lower),
            "statuses": [{"id": k, "label": v} for k, v in STATUSES],
            "themes": [{"id": k, "label": v[0], "group": v[1], "block": root_block(k)} for k, v in THEMES.items()],
            "names": MODULE_NAMES,
        }

    # ---- modules
    def enabled(self, mid, cfg=None):
        return bool((cfg or self.cfg)["modules"][mid]["enabled"])

    def mbase(self, mid, cfg=None):
        m = (cfg or self.cfg)["modules"][mid]["mount"]
        return (m + "/") if m else ""

    def landing_rel(self, mid, cfg=None):
        c = cfg or self.cfg
        return "index.html" if c["home"] == mid else self.mbase(mid, c) + "index.html"

    def module_label(self, mid, cfg=None):
        return (cfg or self.cfg)["modules"][mid]["label"] or MODULE_NAMES[mid]

    def enable_module(self, mid):
        c = self.cfg
        c["modules"][mid]["enabled"] = True
        if mid == "blog" and not self.blog_intro_md().exists():
            write_text(self.blog_intro_md(), BLOG_INTRO_MD)
        if mid == "wiki":
            (self.meta / "wiki" / "sections").mkdir(parents=True, exist_ok=True)
            if not self.wiki_home_md().exists():
                write_text(self.wiki_home_md(), WIKI_HOME_MD)
        if mid == "library":
            (self.meta / "library" / "stories").mkdir(parents=True, exist_ok=True)
            if not self.lib_home_md().exists():
                write_text(self.lib_home_md(), LIB_HOME_MD)
        if not any(n.get("type") == "module" and n.get("id") == mid for n in c.get("nav", [])):
            c.setdefault("nav", []).append({"type": "module", "id": mid, "label": "", "visible": True})

    def save_structure(self, d):
        """Homepage choice, which modules are on, their labels and whether the homepage module sits at the root."""
        c = self.cfg
        mods = d.get("modules") if isinstance(d.get("modules"), dict) else {}
        for m in MODULES:
            want = mods.get(m) if isinstance(mods.get(m), dict) else {}
            if "label" in want:
                c["modules"][m]["label"] = str(want.get("label") or "").strip()[:40] or MODULE_NAMES[m]
            if want.get("enabled") is True and not c["modules"][m]["enabled"]:
                self.enable_module(m)
            elif want.get("enabled") is False and c["modules"][m]["enabled"]:
                if (d.get("home") or c["home"]) == m:
                    raise ApiError("%s is the homepage. Choose another homepage before switching it off."
                                   % self.module_label(m))
                c["modules"][m]["enabled"] = False
        home = d.get("home", c["home"])
        if home not in HOME_MODES:
            raise ApiError("Unknown homepage type.")
        if home in MODULES and not c["modules"][home]["enabled"]:
            self.enable_module(home)
        c["home"] = home
        for m in ("wiki", "library"):
            c["modules"][m]["mount"] = "" if (home == m and d.get("root_mount")) else MOUNT_DEFAULT[m]
        self.normalize()
        self.save_cfg()
        self.build()
        return {"ok": True}

    def remove_module_content(self, mid):
        """Move a switched-off module's sources to the trash and forget them."""
        c = self.cfg
        if mid not in MODULES:
            raise ApiError("Unknown module.")
        if c["modules"][mid]["enabled"]:
            raise ApiError("Switch the module off first.")
        folder = self.meta / mid
        if folder.exists():
            self.to_trash(folder)
        if mid == "blog":
            c["posts"] = []
        elif mid == "wiki":
            c["wiki"] = {"sections": []}
        else:
            c["library"] = {"stories": []}
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- lookups
    def find_page(self, slug, cfg=None):
        return next((p for p in (cfg or self.cfg)["pages"] if p["slug"] == slug), None)

    def get_page_rec(self, slug):
        p = self.find_page(slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        return p

    def taken_root_names(self, exclude=None):
        """Names that a page, or a section or story mounted at the root, cannot use."""
        c = self.cfg
        names = set(RESERVED) | {p["slug"] for p in c["pages"]}
        if c["modules"]["wiki"]["mount"] == "":
            names |= {s["slug"] for s in c["wiki"]["sections"]}
        if c["modules"]["library"]["mount"] == "":
            names |= {s["slug"] for s in c["library"]["stories"]}
        names.discard(exclude)
        return names

    def to_trash(self, path):
        if not path.exists():
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.meta / "trash" / ("%s-%s" % (stamp, path.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        while dest.exists():
            dest = dest.with_name("%s-%s-%d" % (stamp, path.name, n))
            n += 1
        shutil.move(str(path), str(dest))

    # ---- rendering helpers
    def wiki_resolver(self, c):
        if not self.enabled("wiki", c):
            return None
        if self._res[0] is c:
            return self._res[1]
        fn = self.make_resolver(c)
        self._res = (c, fn)
        return fn

    def md(self, c, prefix):
        m = Markdown(prefix, self.wiki_resolver(c))
        self._mds.append(m)
        return m

    def nav_html(self, c, prefix, current_rel, current_mod):
        items = []
        for n in c["nav"]:
            if not n.get("visible", True):
                continue
            if n["type"] == "page":
                p = self.find_page(n["slug"], c)
                if not p:
                    continue
                rel = page_rel(p["slug"])
                label = n.get("label") or p["title"]
                cur = rel == current_rel and not current_mod
                href = prefix + rel
            elif n["type"] == "module":
                mid = n["id"]
                if not self.enabled(mid, c):
                    continue
                label = n.get("label") or self.module_label(mid, c)
                cur = current_mod == mid
                href = prefix + self.landing_rel(mid, c)
            else:
                label, cur, href = n.get("label") or "Link", False, fix_url(n["url"], prefix)
            items.append('<li><a href="%s"%s>%s</a></li>' % (attr(href), ' aria-current="page"' if cur else "", esc(label)))
        if not items:
            return ""
        return '<nav class="site-nav" aria-label="Main"><ul>%s</ul></nav>' % "".join(items)

    def head_seo(self, c, seo):
        site = c["site"]
        h = ["<title>%s</title>" % esc(seo["full_title"])]
        if seo["description"]:
            h.append('<meta name="description" content="%s">' % attr(seo["description"]))
        if site.get("author"):
            h.append('<meta name="author" content="%s">' % attr(site["author"]))
        if seo["canonical"]:
            h.append('<link rel="canonical" href="%s">' % attr(seo["canonical"]))
        if seo["noindex"]:
            h.append('<meta name="robots" content="noindex, follow">')
        h.append('<meta property="og:type" content="%s">' % attr(seo["og_type"]))
        h.append('<meta property="og:title" content="%s">' % attr(seo["social_title"]))
        if seo["description"]:
            h.append('<meta property="og:description" content="%s">' % attr(seo["description"]))
        h.append('<meta property="og:site_name" content="%s">' % attr(site["title"]))
        if seo["canonical"]:
            h.append('<meta property="og:url" content="%s">' % attr(seo["canonical"]))
        if site.get("lang"):
            h.append('<meta property="og:locale" content="%s">' % attr(site["lang"].replace("-", "_")))
        if seo["image"]:
            h.append('<meta property="og:image" content="%s">' % attr(seo["image"]))
        if seo["og_type"] == "article":
            if seo["published"]:
                h.append('<meta property="article:published_time" content="%s">' % attr(seo["published"]))
            for t in seo["tags"]:
                h.append('<meta property="article:tag" content="%s">' % attr(t))
        h.append('<meta name="twitter:card" content="%s">' % ("summary_large_image" if seo["image"] else "summary"))
        h.append('<meta name="twitter:title" content="%s">' % attr(seo["social_title"]))
        if seo["description"]:
            h.append('<meta name="twitter:description" content="%s">' % attr(seo["description"]))
        if seo["image"]:
            h.append('<meta name="twitter:image" content="%s">' % attr(seo["image"]))
        tw = str(site.get("twitter") or "").strip()
        if tw:
            h.append('<meta name="twitter:site" content="%s">' % attr(tw if tw.startswith("@") else "@" + tw))
        return h

    def feed_links(self, c, prefix):
        site = c["site"]
        if not (site.get("feeds") and site.get("url")):
            return []
        out = []
        if self.enabled("blog", c) or self.enabled("library", c):
            out.append('<link rel="alternate" type="application/rss+xml" title="%s" href="%srss.xml">'
                       % (attr(site["title"]), prefix))
        if self.enabled("blog", c):
            out.append('<link rel="alternate" type="application/rss+xml" title="%s" href="%s%srss.xml">'
                       % (attr("%s: %s" % (site["title"], self.module_label("blog", c))), prefix, self.mbase("blog", c)))
        if self.enabled("library", c) and self.mbase("library", c):
            out.append('<link rel="alternate" type="application/rss+xml" title="%s" href="%s%srss.xml">'
                       % (attr("%s: %s" % (site["title"], self.module_label("library", c))), prefix,
                          self.mbase("library", c)))
        return out

    def doc(self, c, *, rel, body_class, main, title="", desc="", og_type="website", published="", tags=None,
            image="", seo_title="", canonical="", noindex=False, current_mod=None, css=None, base=None,
            head_extra=None, full=False, canonical_rel=None):
        """Wrap a page body in the shared shell: head with SEO tags, site header, footer."""
        site = c["site"]
        prefix = up(rel)
        st = site["title"]
        is_home = rel == "index.html"
        full_title = seo_title or (st if (is_home or not title or title == st) else "%s | %s" % (title, st))
        override = (canonical or "").strip()
        canon = override if re.match(r"^https?://", override, re.I) else abs_url(site.get("url"), canonical_rel or rel)
        seo = {
            "full_title": full_title, "social_title": st if is_home else (title or st),
            "description": re.sub(r"\s+", " ", desc or "").strip()[:320], "canonical": canon,
            "image": resolve_img(site, image or site.get("image")), "og_type": og_type,
            "noindex": bool(site.get("noindex") or noindex), "published": published or "",
            "tags": [t for t in (tags or []) if slugify(t)],
        }
        head = ['<meta charset="utf-8">']
        if base:
            head.append('<base href="%s">' % attr(base))
        head.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        head.extend(self.head_seo(c, seo))
        head.extend(self.feed_links(c, prefix))
        if css is None:
            head.append('<link rel="stylesheet" href="%sstyle.css">' % prefix)
            head.extend(head_extra or [])
        else:
            head.append("<style>\n%s\n</style>" % css.replace("</", "<\\/"))
        classes = body_class + (" is-home" if is_home else "")
        tagline = '<span class="tagline">%s</span>' % esc(site["tagline"]) if site.get("tagline") else ""
        nav = self.nav_html(c, prefix, rel, current_mod)
        foot = (site.get("footer", "").replace("{year}", str(datetime.date.today().year))
                .replace("{title}", st).replace("{author}", site.get("author") or st))
        footer = ""
        if foot.strip():
            footer = ('\n<footer class="site-footer"><div class="bar"><p>%s</p></div></footer>'
                      % Markdown(prefix).inline(foot))
        body = main if full else '<main id="main" class="wrap page-main">\n%s\n</main>' % main
        return (
            '<!DOCTYPE html>\n<html lang="%s">\n<head>\n%s\n</head>\n<body class="%s">\n'
            '<a class="skip" href="#main">Skip to content</a>\n'
            '<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-label="Show menu">\n'
            '<header class="site-header"><div class="bar">\n<div class="brand"><a class="site-title" href="%sindex.html">%s</a>%s</div>\n'
            '%s%s\n</div></header>\n%s%s\n</body>\n</html>\n'
            % (attr(site.get("lang") or "en"), "\n".join(head), attr(classes), prefix, esc(st), tagline,
               '<label for="nav-toggle" class="nav-button">Menu</label>\n' if nav else "", nav, body, footer)
        )

    def sidebar_layout(self, side_html, main_html, button="Browse"):
        return ('<input type="checkbox" id="side-toggle" class="side-toggle" aria-label="Show %s">\n'
                '<label for="side-toggle" class="side-button">%s</label>\n'
                '<div class="with-sidebar">\n%s\n<main id="main">\n%s\n</main>\n</div>'
                % (attr(button.lower()), esc(button), side_html, main_html))

    # ---- pages
    def render_page(self, c, page, markdown, css=None, base=None):
        rel = page_rel(page["slug"])
        M = self.md(c, up(rel))
        body = M.render(markdown)
        parts = []
        if page.get("show_title", True):
            parts.append('<h1 class="page-title">%s</h1>' % esc(page["title"]))
        if body:
            parts.append(body)
        site = c["site"]
        desc = page.get("description") or (site.get("description") if page["slug"] == "index" else "") \
            or auto_summary(body) or site.get("tagline", "")
        return self.doc(c, rel=rel, body_class="page-" + page["slug"], title=page["title"],
                        main='<article class="content">\n%s\n</article>' % "\n".join(parts), desc=desc,
                        image=page.get("image"), seo_title=page.get("seo_title"), canonical=page.get("canonical"),
                        noindex=page.get("noindex"), css=css, base=base)

    def render_home(self, c, css=None, base=None):
        home = c["home"]
        if home == "blog":
            return self.blog_landing(c, "index.html", css=css, base=base)
        if home == "wiki":
            return self.wiki_landing(c, "index.html", css=css, base=base)
        if home == "library":
            return self.lib_landing(c, "index.html", css=css, base=base)
        return self.render_page(c, self.find_page("index", c), read_text(self.page_md("index")), css=css, base=base)

    def add_page(self, d):
        title = str(d.get("title") or "").strip()[:120]
        if not title:
            raise ApiError("Give the page a title.")
        slug = slugify(title) or "page"
        taken = self.taken_root_names()
        base, n = slug, 2
        while slug in taken:
            slug = "%s-%d" % (base, n)
            n += 1
        template = {"about": ABOUT_MD, "privacy": PRIVACY_MD.replace("{date}", nice_date(today()))}.get(d.get("template"), "")
        self.cfg["pages"].append({"slug": slug, "title": title, "show_title": True})
        write_text(self.page_md(slug), template)
        self.normalize_nav()
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_page(self, slug):
        p = self.get_page_rec(slug)
        return {"page": p, "markdown": read_text(self.page_md(slug))}

    def save_page(self, slug, d):
        p = self.get_page_rec(slug)
        p["title"] = str(d.get("title") or "").strip()[:120] or p["title"]
        if "show_title" in d:
            p["show_title"] = bool(d["show_title"])
        seo_fields(d, p)
        wanted = slugify(str(d.get("new_slug") or "")) or p["slug"]
        if p["slug"] != "index" and wanted != p["slug"]:
            if wanted in self.taken_root_names(exclude=p["slug"]):
                raise ApiError("The address %s is already used or reserved." % wanted)
            rename_path(self.page_md(p["slug"]), self.page_md(wanted))
            for n in self.cfg["nav"]:
                if n.get("type") == "page" and n.get("slug") == p["slug"]:
                    n["slug"] = wanted
            p["slug"] = wanted
        write_text(self.page_md(p["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": p["slug"]}

    def delete_page(self, slug):
        if slug == "index":
            raise ApiError("The home page cannot be deleted.")
        p = self.get_page_rec(slug)
        self.cfg["pages"].remove(p)
        self.to_trash(self.page_md(slug))
        self.normalize_nav()
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- menu
    def save_nav(self, items):
        if not isinstance(items, list):
            raise ApiError("Bad menu.")
        clean = []
        for n in items:
            if not isinstance(n, dict):
                continue
            if n.get("type") == "link":
                url = str(n.get("url") or "").strip()
                if not url:
                    continue
                if _SCHEME.match(url) and not re.match(r"^(https?|mailto|tel):", url, re.I):
                    raise ApiError("Menu links must be http, https, mailto or tel links, or paths on this site.")
                clean.append({"type": "link", "label": str(n.get("label") or "Link").strip()[:60], "url": url[:400],
                              "visible": bool(n.get("visible", True))})
            elif n.get("type") in ("page", "module"):
                key = "slug" if n["type"] == "page" else "id"
                clean.append({"type": n["type"], key: str(n.get(key) or ""), "label": str(n.get("label") or "")[:60],
                              "visible": bool(n.get("visible", True))})
        self.cfg["nav"] = clean
        self.normalize_nav()
        self.save_cfg()
        self.build()
        return {"nav": self.cfg["nav"]}

    # ---- css
    def get_css(self):
        return {"css": read_text(self.css_path())}

    def save_css(self, css):
        write_text(self.css_path(), str(css))
        return {"ok": True}

    # ---- settings
    @staticmethod
    def clean_site(d, current):
        out = dict(current)
        for k, limit in (("title", 120), ("tagline", 200), ("footer", 400), ("author", 120), ("lang", 12),
                         ("description", 320), ("image", 400), ("twitter", 60)):
            if k in d:
                out[k] = str(d[k]).strip()[:limit]
        if "url" in d:
            out["url"] = str(d["url"]).strip().rstrip("/")[:300]
        if d.get("search") in SEARCH_MODES:
            out["search"] = d["search"]
        if d.get("prose") in ("indented", "spaced"):
            out["prose"] = d["prose"]
        if "wpm" in d:
            try:
                out["wpm"] = max(60, min(1000, int(d["wpm"])))
            except (TypeError, ValueError):
                pass
        for k in SITE_BOOLS:
            if k in d:
                out[k] = bool(d[k])
        out["title"] = out.get("title") or current.get("title") or "My site"
        out["lang"] = out.get("lang") or "en"
        return out

    def save_settings(self, d):
        site = self.clean_site(d, self.cfg["site"])
        url = site.get("url", "")
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise ApiError("The site URL must start with http:// or https://, for example https://example.com")
        if site.get("feeds") and not url:
            raise ApiError("Enter the site URL to publish feeds. Feed readers need full web addresses.")
        if site.get("sitemap") and not url:
            raise ApiError("Enter the site URL to generate a sitemap. It needs full web addresses.")
        if site.get("search") == "web" and not url:
            raise ApiError("Web search needs the site URL, so the search engine knows which site to look in.")
        self.cfg["site"] = site
        self.save_cfg()
        self.build()
        return {"ok": True}

    def upload(self, name, data):
        stem, ext = os.path.splitext(os.path.basename(name))
        ext = ext.lower()
        if ext not in IMG_EXT:
            raise ApiError("Only image files can be added (png, jpg, gif, webp, svg, avif).")
        if not data:
            raise ApiError("That file is empty.")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "image"
        folder = self.root / "assets"
        folder.mkdir(exist_ok=True)
        fn, n = stem + ext, 2
        while (folder / fn).exists():
            fn = "%s-%d%s" % (stem, n, ext)
            n += 1
        (folder / fn).write_bytes(data)
        return {"path": "assets/" + fn}

    # ---- build
    def build(self):
        c = self.cfg
        self._mds = []
        self._res = (None, None)
        if self.enabled("library"):
            self.lib_count_words(c)
        files = {"index.html": self.render_home(c)}
        for p in c["pages"]:
            if p["slug"] != "index":
                files[page_rel(p["slug"])] = self.render_page(c, p, read_text(self.page_md(p["slug"])))
        if self.enabled("blog"):
            files.update(self.blog_files(c))
        if self.enabled("wiki"):
            files.update(self.wiki_files(c))
        if self.enabled("library"):
            files.update(self.lib_files(c))
        if c["site"].get("feeds") and c["site"].get("url") and (self.enabled("blog") or self.enabled("library")):
            xml = self.feed_xml(c, self.feed_items(c, blog=True, library=True), c["site"]["title"], "rss.xml")
            files["rss"] = xml
            files["rss.xml"] = xml
        for rel, text in files.items():
            write_if_changed(self.root / rel, text)
        for rel in set(c.get("generated", [])) - set(files):
            self.remove_generated(rel)
        c["generated"] = sorted(files)
        self.write_seo_files()
        self.save_cfg()
        self._mds = []

    def remove_generated(self, rel):
        """Delete a file this app generated earlier and no longer produces. Paths are checked so a
        tampered config can never reach outside the site folder or into the sources."""
        try:
            p = (self.root / str(rel)).resolve()
            parts = p.relative_to(self.root.resolve()).parts
        except (ValueError, OSError):
            return
        if not parts or parts[0].startswith(".") or parts[0] == "assets":
            return
        if p.suffix not in (".html", ".xml", ".js") and p.name != "rss":
            return
        if p.is_file():
            p.unlink()
        parent = p.parent
        root = self.root.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # ---- feeds
    def feed_items(self, c, blog, library):
        """(date, title, rel, html) for recent posts and chapters, newest first."""
        items = []
        if blog and self.enabled("blog", c):
            for p in self.published_posts(c):
                items.append((p["date"], p["title"], self.post_rel(p["slug"], c),
                              Markdown("").render(read_text(self.post_md(p["slug"]))), p.get("tags", [])))
        if library and self.enabled("library", c):
            for s, ch in self.lib_recent(c, 50):
                items.append((ch["date"], "%s: %s" % (s["title"], ch["title"]), self.chapter_rel(s, ch, c),
                              Markdown("").render(read_text(self.chapter_md(s["slug"], ch["slug"]))), [s["title"]]))
        items.sort(key=lambda x: x[0], reverse=True)
        return items[:50]

    def feed_xml(self, c, items, title, self_rel):
        site = c["site"]
        url = site["url"].rstrip("/")
        out = []
        for date, t, rel, body, cats in items:
            link = "%s/%s" % (url, rel)
            when = email.utils.format_datetime(datetime.datetime.combine(
                datetime.date.fromisoformat(clean_date(date)), datetime.time(12, 0), tzinfo=datetime.timezone.utc))
            out.append(
                "<item>\n<title>%s</title>\n<link>%s</link>\n<guid isPermaLink=\"true\">%s</guid>\n<pubDate>%s</pubDate>\n%s"
                "<description>%s</description>\n<content:encoded><![CDATA[%s]]></content:encoded>\n</item>"
                % (_escape(t, quote=False), _escape(link, quote=False), _escape(link, quote=False), when,
                   "".join("<category>%s</category>\n" % _escape(x, quote=False) for x in cats),
                   _escape(auto_summary(body), quote=False), body.replace("]]>", "]]&gt;"))
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
            'xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
            "<title>%s</title>\n<link>%s/index.html</link>\n<description>%s</description>\n<language>%s</language>\n"
            '<generator>SiteForge</generator>\n<atom:link href="%s/%s" rel="self" type="application/rss+xml"/>\n%s\n'
            "</channel>\n</rss>\n"
            % (_escape(title, quote=False), _escape(url, quote=False),
               _escape(site.get("description") or site.get("tagline") or site["title"], quote=False),
               _escape(site.get("lang") or "en", quote=False), _escape(url, quote=False), _escape(self_rel, quote=False),
               "\n".join(out))
        )

    # ---- sitemap and robots
    def sitemap_rows(self, c):
        rows = [("index.html", "")]
        rows += [(page_rel(p["slug"]), "") for p in c["pages"] if p["slug"] != "index" and not p.get("noindex")]
        if self.enabled("blog", c):
            rows += self.blog_sitemap(c)
        if self.enabled("wiki", c):
            rows += self.wiki_sitemap(c)
        if self.enabled("library", c):
            rows += self.lib_sitemap(c)
        return rows

    def write_seo_files(self):
        site = self.cfg["site"]
        url = (site.get("url") or "").rstrip("/")
        sm, rb = self.root / "sitemap.xml", self.root / "robots.txt"

        def ours(path):
            return path.is_file() and "generated by SiteForge" in read_text(path)[:300]

        want = bool(site.get("sitemap")) and bool(url) and not site.get("noindex")
        if want and not sm.is_dir() and (not sm.exists() or ours(sm)):
            seen, rows = set(), []
            for rel, lastmod in self.sitemap_rows(self.cfg):
                if rel in seen:
                    continue
                seen.add(rel)
                lm = "<lastmod>%s</lastmod>" % lastmod if lastmod else ""
                rows.append("<url><loc>%s</loc>%s</url>" % (_escape("%s/%s" % (url, rel), quote=False), lm))
            write_if_changed(sm, '<?xml version="1.0" encoding="UTF-8"?>\n<!-- generated by SiteForge -->\n'
                                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % "\n".join(rows))
        elif not want and ours(sm):
            sm.unlink()
        if url and site.get("noindex"):
            robots = "# generated by SiteForge\nUser-agent: *\nDisallow: /\n"
        elif want:
            robots = "# generated by SiteForge\nUser-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % url
        else:
            robots = None
        if robots and not rb.is_dir() and (not rb.exists() or ours(rb)):
            write_if_changed(rb, robots)
        elif robots is None and ours(rb):
            rb.unlink()

    # ---- preview
    def preview(self, p):
        cfg = copy.deepcopy(self.cfg)
        self._mds = []
        self._res = (None, None)
        if isinstance(p.get("site"), dict):
            cfg["site"] = self.clean_site(p["site"], cfg["site"])
        if isinstance(p.get("nav"), list):
            cfg["nav"] = [n for n in p["nav"] if isinstance(n, dict)]
        css = p.get("css") if isinstance(p.get("css"), str) else read_text(self.css_path())
        view = p.get("view")
        md = str(p.get("markdown") or "")
        base = "/site/"
        html_ = None
        if view == "page":
            page = self.find_page(str(p.get("slug") or ""), cfg)
            if not page:
                raise ApiError("That page does not exist.", 404)
            page["title"] = str(p.get("title") or "").strip() or page["title"]
            page["show_title"] = bool(p.get("show_title", True))
            seo_fields(p, page)
            if page["slug"] == "index" and cfg["home"] != "page":
                cfg["home"] = "page"
            html_ = self.render_page(cfg, page, md, css=css, base=base)
        elif view in ("post", "blogintro", "tag"):
            html_ = self.blog_preview(cfg, p, md, css, base)
        elif view in ("wikihome", "section", "article"):
            html_ = self.wiki_preview(cfg, p, md, css, base)
        elif view in ("libhome", "story", "chapter"):
            html_ = self.lib_preview(cfg, p, md, css, base)
        if html_ is None:
            html_ = self.preview_target(cfg, str(p.get("target") or "home"), css)
        missing = sorted({x for m in self._mds for x in m.missing})
        self._mds = []
        return {"html": html_, "missing": missing}

    def preview_target(self, c, target, css):
        """Existing pages, for the CSS, menu, settings and structure views."""
        kind, _, rest = target.partition(":")
        rel_base = "/site/"
        if kind == "page":
            page = self.find_page(rest, c)
            if page:
                return self.render_page(c, page, read_text(self.page_md(page["slug"])), css=css, base=rel_base)
        if kind == "blog" and self.enabled("blog", c):
            r = self.blog_preview_target(c, rest, css)
            if r:
                return r
        if kind == "wiki" and self.enabled("wiki", c):
            r = self.wiki_preview_target(c, rest, css)
            if r:
                return r
        if kind == "library" and self.enabled("library", c):
            r = self.lib_preview_target(c, rest, css)
            if r:
                return r
        return self.render_home(c, css=css, base=rel_base)

    def preview_targets(self):
        """Choices for the CSS view's preview list."""
        c = self.cfg
        out = [{"id": "home", "label": "Homepage"}]
        out += [{"id": "page:" + p["slug"], "label": "Page: " + p["title"]} for p in c["pages"] if p["slug"] != "index"]
        if self.enabled("blog"):
            out.append({"id": "blog:", "label": "Blog feed"})
            out += [{"id": "blog:" + p["slug"], "label": "Post: " + p["title"]} for p in self.published_posts(c)[:20]]
        if self.enabled("wiki"):
            out.append({"id": "wiki:", "label": "Wiki home"})
            for s in c["wiki"]["sections"]:
                out.append({"id": "wiki:" + s["slug"], "label": "Wiki section: " + s["title"]})
                out += [{"id": "wiki:%s/%s" % (s["slug"], a["slug"]), "label": "   Article: " + a["title"]}
                        for a in s["articles"]]
        if self.enabled("library"):
            out.append({"id": "library:", "label": "Library"})
            for s in c["library"]["stories"]:
                out.append({"id": "library:" + s["slug"], "label": "Story: " + s["title"]})
                out += [{"id": "library:%s/%s" % (s["slug"], ch["slug"]), "label": "   Chapter: " + ch["title"]}
                        for ch in s["chapters"]]
        return {"targets": out}


# ----------------------------------------------------------------------------
# Blog module
# ----------------------------------------------------------------------------

class BlogMixin:
    def blog_intro_md(self):
        return self.meta / "blog" / "_index.md"

    def post_md(self, slug):
        return self.meta / "blog" / "posts" / (slug + ".md")

    def post_rel(self, slug, c=None):
        return self.mbase("blog", c) + "posts/%s.html" % slug

    def tag_rel(self, name, c=None):
        return self.mbase("blog", c) + "tag/%s.html" % slugify(name)

    def normalize_blog(self):
        posts, seen = [], set()
        for p in self.cfg["posts"]:
            if not isinstance(p, dict) or not slugify(p.get("slug", "")) or p["slug"] in seen:
                continue
            p["slug"] = slugify(p["slug"])
            seen.add(p["slug"])
            p["title"] = str(p.get("title") or title_from_name(p["slug"]))
            p["date"] = clean_date(p.get("date"))
            p["summary"] = str(p.get("summary") or "")
            p["tags"] = clean_tags(p.get("tags"))
            p["status"] = "published" if p.get("status") == "published" else "draft"
            for k in ("seo_title", "description", "image", "canonical"):
                p[k] = str(p.get(k) or "")
            p["noindex"] = bool(p.get("noindex"))
            posts.append(p)
        self.cfg["posts"] = posts

    def published_posts(self, c):
        posts = [p for p in c["posts"] if p.get("status") == "published"]
        return sorted(posts, key=lambda p: (p["date"], p["title"].lower()), reverse=True)

    def tag_map(self, c):
        out = {}
        for p in self.published_posts(c):
            for t in p.get("tags", []):
                out.setdefault(slugify(t), {"name": t, "posts": []})["posts"].append(p)
        return dict(sorted(out.items()))

    def tags_html(self, c, tags, prefix):
        if not tags:
            return ""
        return '<ul class="tags">%s</ul>\n' % "".join(
            '<li><a href="%s%s">%s</a></li>' % (prefix, attr(self.tag_rel(t, c)), esc(t)) for t in tags)

    def post_list(self, c, posts, prefix):
        if not posts:
            return '<p class="empty">No posts yet.</p>'
        items = []
        for p in posts:
            summary = p.get("summary", "").strip() or auto_summary(Markdown("").render(read_text(self.post_md(p["slug"]))))
            items.append(
                '<li class="post-item">\n<h2><a href="%s%s">%s</a></h2>\n<p class="post-meta"><time datetime="%s">%s</time></p>\n%s%s</li>'
                % (prefix, attr(self.post_rel(p["slug"], c)), esc(p["title"]), attr(p["date"]), esc(nice_date(p["date"])),
                   self.tags_html(c, p.get("tags", []), prefix),
                   '<p class="post-summary">%s</p>\n' % esc(summary) if summary else ""))
        return '<ul class="post-list">\n%s\n</ul>' % "\n".join(items)

    def blog_landing(self, c, rel, css=None, base=None, intro=None):
        prefix = up(rel)
        body = self.md(c, prefix).render(read_text(self.blog_intro_md()) if intro is None else intro)
        site = c["site"]
        label = self.module_label("blog", c)
        parts = []
        if rel != "index.html" or not body:
            parts.append('<h1 class="page-title">%s</h1>' % esc(label))
        if body:
            parts.append(body)
        if site.get("feeds") and site.get("url"):
            parts.append('<p class="feed-links"><a href="%s%srss.xml">RSS feed</a></p>' % (prefix, self.mbase("blog", c)))
        parts.append(self.post_list(c, self.published_posts(c), prefix))
        home = c["home"] == "blog"
        return self.doc(c, rel=rel, body_class="mod-blog blog-feed", title="" if rel == "index.html" else label,
                        main='<article class="content">\n%s\n</article>' % "\n".join(parts),
                        desc=auto_summary(body) or site.get("description") or site.get("tagline"),
                        current_mod="blog", css=css, base=base,
                        canonical_rel="index.html" if home else None)

    def render_post(self, c, post, markdown, css=None, base=None):
        rel = self.post_rel(post["slug"], c)
        prefix = up(rel)
        body = self.md(c, prefix).render(strip_title_h1(markdown, post["title"]))
        parts = ['<h1 class="page-title">%s</h1>' % esc(post["title"]),
                 '<p class="post-meta"><time datetime="%s">%s</time></p>' % (attr(post["date"]), esc(nice_date(post["date"]))),
                 self.tags_html(c, post.get("tags", []), prefix), body]
        posts = self.published_posts(c)
        slugs = [p["slug"] for p in posts]
        if c["site"].get("post_nav") and post["slug"] in slugs:
            i = slugs.index(post["slug"])
            pager = []
            if i + 1 < len(posts):
                o = posts[i + 1]
                pager.append('<a class="prev" href="%s%s"><small>Older</small>%s</a>' % (prefix, attr(self.post_rel(o["slug"], c)), esc(o["title"])))
            if i > 0:
                n = posts[i - 1]
                pager.append('<a class="next" href="%s%s"><small>Newer</small>%s</a>' % (prefix, attr(self.post_rel(n["slug"], c)), esc(n["title"])))
            if pager:
                parts.append('<nav class="pager" aria-label="More posts">%s</nav>' % "".join(pager))
        parts.append('<p class="post-back"><a href="%s%s">Back to all posts</a></p>' % (prefix, self.landing_rel("blog", c)))
        desc = post.get("description") or post.get("summary") or auto_summary(body)
        return self.doc(c, rel=rel, body_class="mod-blog blog-post post-" + post["slug"], title=post["title"],
                        main='<article class="content">\n%s\n</article>' % "\n".join(x for x in parts if x),
                        desc=desc, og_type="article", published=post["date"], tags=post.get("tags"),
                        image=post.get("image"), seo_title=post.get("seo_title"), canonical=post.get("canonical"),
                        noindex=post.get("noindex"), current_mod="blog", css=css, base=base)

    def render_tag(self, c, name, posts, css=None, base=None):
        rel = self.tag_rel(name, c)
        prefix = up(rel)
        main = ('<article class="content">\n<h1 class="page-title">Posts tagged &ldquo;%s&rdquo;</h1>\n'
                '<p class="tag-back"><a href="%s%s">All posts</a></p>\n%s\n</article>'
                % (esc(name), prefix, self.landing_rel("blog", c), self.post_list(c, posts, prefix)))
        return self.doc(c, rel=rel, body_class="mod-blog blog-tag tag-" + slugify(name), title="Tagged: " + name,
                        main=main, desc="Posts tagged %s." % name, current_mod="blog", css=css, base=base)

    def blog_files(self, c):
        files = {self.mbase("blog", c) + "index.html": self.blog_landing(c, self.mbase("blog", c) + "index.html")}
        for p in self.published_posts(c):
            files[self.post_rel(p["slug"], c)] = self.render_post(c, p, read_text(self.post_md(p["slug"])))
        for slug, t in self.tag_map(c).items():
            files[self.tag_rel(t["name"], c)] = self.render_tag(c, t["name"], t["posts"])
        site = c["site"]
        if site.get("feeds") and site.get("url"):
            rel = self.mbase("blog", c) + "rss.xml"
            files[rel] = self.feed_xml(c, self.feed_items(c, blog=True, library=False),
                                       "%s: %s" % (site["title"], self.module_label("blog", c)), rel)
        return files

    def blog_sitemap(self, c):
        rows = []
        if c["home"] != "blog":
            rows.append((self.landing_rel("blog", c), ""))
        rows += [(self.post_rel(p["slug"], c), p["date"]) for p in self.published_posts(c) if not p.get("noindex")]
        rows += [(self.tag_rel(t["name"], c), "") for t in self.tag_map(c).values()]
        return rows

    # ---- API
    def find_post(self, slug):
        return next((p for p in self.cfg["posts"] if p["slug"] == slug), None)

    def get_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        return {"post": p, "markdown": read_text(self.post_md(slug))}

    def save_post(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the post a title.")
        known = {t for p in self.cfg["posts"] for t in p.get("tags", [])}
        orig = str(d.get("orig_slug") or "")
        post = self.find_post(orig) if orig else None
        if orig and not post:
            raise ApiError("That post does not exist.", 404)
        wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "post"
        taken = {p["slug"] for p in self.cfg["posts"] if p is not post} | {"index"}
        slug, n = wanted, 2
        while slug in taken:
            slug = "%s-%d" % (wanted, n)
            n += 1
        md = str(d.get("markdown") or "")
        if post is None:
            post = {"slug": slug, "status": "draft"}
            self.cfg["posts"].append(post)
        elif post["slug"] != slug:
            old = self.post_md(post["slug"])
            if old.exists():
                old.unlink()
            post["slug"] = slug
        post.update(title=title, date=clean_date(d.get("date")), summary=str(d.get("summary") or "").strip()[:400],
                    tags=clean_tags(d.get("tags"), known))
        seo_fields(d, post)
        if d.get("publish"):
            post["status"] = "published"
        write_text(self.post_md(slug), md)
        self.normalize_blog()
        self.save_cfg()
        self.build()
        return {"slug": slug, "post": self.find_post(slug)}

    def unpublish_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        p["status"] = "draft"
        self.save_cfg()
        self.build()
        return {"post": p}

    def delete_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        self.cfg["posts"].remove(p)
        self.to_trash(self.post_md(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def get_blog_intro(self):
        return {"markdown": read_text(self.blog_intro_md())}

    def save_blog_intro(self, d):
        write_text(self.blog_intro_md(), str(d.get("markdown") or ""))
        self.build()
        return {"ok": True}

    def blog_preview(self, c, p, md, css, base):
        if p.get("view") == "blogintro":
            rel = "index.html" if c["home"] == "blog" else self.mbase("blog", c) + "index.html"
            return self.blog_landing(c, rel, css=css, base=base + (up(rel) and self.mbase("blog", c)), intro=md)
        post = next((x for x in c["posts"] if x["slug"] == str(p.get("orig_slug") or "")), None)
        if post is None:
            post = {"slug": slugify(str(p.get("new_slug") or p.get("title") or "")) or "post", "tags": []}
            c["posts"].append(post)
        post.update(title=str(p.get("title") or "").strip() or "Untitled post", date=clean_date(p.get("date")),
                    summary=str(p.get("summary") or ""), tags=clean_tags(p.get("tags")), status="published")
        seo_fields(p, post)
        return self.render_post(c, post, md, css=css, base=base + self.mbase("blog", c) + "posts/")

    def blog_preview_target(self, c, rest, css):
        if not rest:
            rel = self.mbase("blog", c) + "index.html"
            return self.blog_landing(c, rel, css=css, base="/site/" + self.mbase("blog", c))
        post = next((x for x in c["posts"] if x["slug"] == rest), None)
        if post:
            return self.render_post(c, post, read_text(self.post_md(rest)), css=css,
                                    base="/site/" + self.mbase("blog", c) + "posts/")
        return None


# ----------------------------------------------------------------------------
# Wiki module
# ----------------------------------------------------------------------------

def preview_base(rel):
    return "/site/" + (rel.rsplit("/", 1)[0] + "/" if "/" in rel else "")


def wiki_published(sec):
    return [a for a in sec.get("articles", []) if not a.get("draft")]


class WikiMixin:
    def wiki_home_md(self):
        return self.meta / "wiki" / "home.md"

    def wsec_dir(self, s):
        return self.meta / "wiki" / "sections" / s

    def wsec_md(self, s):
        return self.wsec_dir(s) / "_index.md"

    def wart_md(self, s, a):
        return self.wsec_dir(s) / (a + ".md")

    def wsec_rel(self, s, c=None):
        return self.mbase("wiki", c) + "%s/index.html" % s

    def wart_rel(self, s, a, c=None):
        return self.mbase("wiki", c) + "%s/%s.html" % (s, a)

    def normalize_wiki(self):
        secs, seen = [], set()
        for s in self.cfg["wiki"]["sections"]:
            if not isinstance(s, dict) or not slugify(s.get("slug", "")) or s["slug"] in seen:
                continue
            s["slug"] = slugify(s["slug"])
            seen.add(s["slug"])
            s["title"] = str(s.get("title") or title_from_name(s["slug"]))
            s["description"] = str(s.get("description") or "")
            arts, aseen = [], set()
            for a in s.get("articles") if isinstance(s.get("articles"), list) else []:
                if not isinstance(a, dict) or not slugify(a.get("slug", "")) or a["slug"] in aseen:
                    continue
                aseen.add(a["slug"])
                a["title"] = str(a.get("title") or title_from_name(a["slug"]))
                a["description"] = str(a.get("description") or "")
                a["draft"] = bool(a.get("draft"))
                a["updated"] = clean_date(a.get("updated"))
                arts.append(a)
            s["articles"] = arts
            secs.append(s)
        self.cfg["wiki"]["sections"] = secs

    def find_section(self, slug, c=None):
        return next((s for s in (c or self.cfg)["wiki"]["sections"] if s["slug"] == slug), None)

    def get_section_rec(self, slug):
        s = self.find_section(slug)
        if not s:
            raise ApiError("That wiki section does not exist.", 404)
        return s

    def get_article_rec(self, sslug, aslug):
        s = self.get_section_rec(sslug)
        a = next((x for x in s["articles"] if x["slug"] == aslug), None)
        if not a:
            raise ApiError("That article does not exist.", 404)
        return s, a

    def unique_section_slug(self, base, exclude=None):
        base = slugify(base) or "section"
        taken = {s["slug"] for s in self.cfg["wiki"]["sections"] if s["slug"] != exclude} | RESERVED
        if self.cfg["modules"]["wiki"]["mount"] == "":
            taken |= self.taken_root_names(exclude=exclude)
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.wsec_dir(slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def unique_article_slug(self, sec, base, exclude=None):
        base = slugify(base) or "article"
        taken = {a["slug"] for a in sec["articles"] if a["slug"] != exclude} | {"index", "_index"}
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.wart_md(sec["slug"], slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    # ---- rescan: pick up .md files written outside the editor
    def wiki_rescan(self):
        base = self.meta / "wiki" / "sections"
        base.mkdir(parents=True, exist_ok=True)
        r = {"wiki_sections_added": 0, "wiki_articles_added": 0, "wiki_articles_removed": 0}
        known = {s["slug"] for s in self.cfg["wiki"]["sections"]}
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith(".") or d.name in known:
                continue
            slug = self.unique_section_slug(d.name, exclude=d.name if slugify(d.name) == d.name else None)
            if slug != d.name:
                if (base / slug).exists() and not (base / slug).samefile(d):
                    continue
                rename_path(d, base / slug)
            self.cfg["wiki"]["sections"].append({"slug": slug, "title": title_from_name(d.name), "description": "", "articles": []})
            known.add(slug)
            r["wiki_sections_added"] += 1
        for sec in self.cfg["wiki"]["sections"]:
            d = self.wsec_dir(sec["slug"])
            d.mkdir(parents=True, exist_ok=True)
            arts = {a["slug"]: a for a in sec["articles"]}
            for f in sorted(d.glob("*.md"), key=lambda p: p.name.lower()):
                if f.name == "_index.md" or f.name.startswith("."):
                    continue
                if f.stem in arts:
                    mdate = file_date(f)
                    if mdate > arts[f.stem].get("updated", ""):
                        arts[f.stem]["updated"] = mdate
                    continue
                orig = f.stem
                slug = orig if slugify(orig) == orig and orig not in ("index", "_index") else None
                if slug is None:
                    slug = self.unique_article_slug(sec, orig)
                    target = self.wart_md(sec["slug"], slug)
                    if target.exists() and not target.samefile(f):
                        continue
                    rename_path(f, target)
                    f = target
                art = {"slug": slug, "title": first_h1(read_text(f)) or title_from_name(orig), "description": "",
                       "draft": False, "updated": file_date(f)}
                sec["articles"].append(art)
                arts[slug] = art
                r["wiki_articles_added"] += 1
            keep = [a for a in sec["articles"] if self.wart_md(sec["slug"], a["slug"]).is_file()]
            r["wiki_articles_removed"] += len(sec["articles"]) - len(keep)
            sec["articles"] = keep
        return r

    # ---- rendering
    def make_resolver(self, c):
        """[[wiki links]] to site-root-relative URLs. Section/Article paths win, then article titles
        and slugs, then section titles and slugs, then page, post and story titles. Drafts are
        not linkable."""
        idx = {}

        def norm(s):
            return re.sub(r"\s+", " ", str(s).strip().lower())

        arts = [(s, a) for s in c["wiki"]["sections"] for a in wiki_published(s)]
        for s, a in arts:
            u = self.wart_rel(s["slug"], a["slug"], c)
            for key in (s["slug"] + "/" + a["slug"], norm(s["title"]) + "/" + norm(a["title"])):
                idx.setdefault(key, u)
        for s, a in arts:
            idx.setdefault(norm(a["title"]), self.wart_rel(s["slug"], a["slug"], c))
        for s, a in arts:
            idx.setdefault(a["slug"], self.wart_rel(s["slug"], a["slug"], c))
        for s in c["wiki"]["sections"]:
            idx.setdefault(norm(s["title"]), self.wsec_rel(s["slug"], c))
            idx.setdefault(s["slug"], self.wsec_rel(s["slug"], c))
        # then the rest of the site, so [[Title]] also reaches pages, posts and stories
        for p in c["pages"]:
            idx.setdefault(norm(p["title"]), page_rel(p["slug"]))
        if self.enabled("blog", c):
            for p in self.published_posts(c):
                idx.setdefault(norm(p["title"]), self.post_rel(p["slug"], c))
        if self.enabled("library", c):
            for st in lib_visible(c):
                idx.setdefault(norm(st["title"]), self.story_rel(st["slug"], c))
        idx.setdefault("home", "index.html")

        def resolve(target):
            t = norm(target)
            if t in idx:
                return idx[t]
            if "/" in t:
                sec, _, art = t.partition("/")
                key = slugify(sec) + "/" + slugify(art)
                if key in idx:
                    return idx[key]
            return idx.get(slugify(t))

        return resolve

    def wiki_search_box(self, c, prefix):
        site = c["site"]
        if site.get("search") == "builtin":
            return ('<div class="search" data-root="%s">\n<input type="search" id="wiki-search" placeholder="Search the wiki" '
                    'aria-label="Search the wiki" autocomplete="off" spellcheck="false">\n'
                    '<ul class="search-results" id="wiki-results" hidden></ul>\n</div>' % attr(prefix))
        if site.get("search") == "web" and site.get("url"):
            return ('<form class="search" role="search" action="https://duckduckgo.com/" method="get">\n'
                    '<input type="hidden" name="sites" value="%s">\n'
                    '<input type="search" name="q" placeholder="Search the wiki" aria-label="Search the wiki">\n</form>'
                    % attr(urlsplit(site["url"]).netloc))
        return ""

    def wiki_sidebar(self, c, prefix, cur_sec=None, cur_art=None, home=False):
        out = ['<nav class="side wiki-side" aria-label="Wiki">', self.wiki_search_box(c, prefix)]
        out.append('<ul class="nav-home"><li><a href="%s%s"%s>%s home</a></li></ul>'
                   % (prefix, self.landing_rel("wiki", c), ' aria-current="page"' if home else "",
                      esc(self.module_label("wiki", c))))
        if not c["wiki"]["sections"]:
            out.append('<p class="nav-empty">No sections yet.</p>')
        for s in c["wiki"]["sections"]:
            arts = wiki_published(s)
            is_cur = s["slug"] == cur_sec
            items = ['<li><a href="%s%s"%s>Overview</a></li>' % (prefix, self.wsec_rel(s["slug"], c),
                                                               ' aria-current="page"' if is_cur and cur_art is None else "")]
            for a in arts:
                cur = ' aria-current="page"' if is_cur and a["slug"] == cur_art else ""
                items.append('<li><a href="%s%s"%s>%s</a></li>' % (prefix, self.wart_rel(s["slug"], a["slug"], c), cur, esc(a["title"])))
            out.append('<details class="nav-section"%s>\n<summary>%s<span class="nav-count">%d</span></summary>\n<ul>\n%s\n</ul>\n</details>'
                       % (" open" if (c["site"].get("expand") or is_cur) else "", esc(s["title"]), len(arts), "\n".join(items)))
        out.append("</nav>")
        return "\n".join(x for x in out if x)

    def wiki_head(self, c, prefix):
        if c["site"].get("search") == "builtin":
            return ['<script src="%s%ssearch.js" defer></script>' % (prefix, self.mbase("wiki", c)),
                    "<noscript><style>.search { display: none; }</style></noscript>"]
        return []

    @staticmethod
    def breadcrumbs(trail):
        items = []
        for label, href in trail:
            items.append('<li><a href="%s">%s</a></li>' % (attr(href), esc(label)) if href
                         else '<li aria-current="page">%s</li>' % esc(label))
        return '<nav class="breadcrumbs" aria-label="Breadcrumbs"><ol>%s</ol></nav>' % "".join(items)

    @staticmethod
    def toc_html(headings):
        if len(headings) < 3:
            return ""
        groups = []
        for level, hid, text in headings:
            if level == 2 or not groups:
                groups.append([(hid, text), []])
            else:
                groups[-1][1].append((hid, text))
        out = ['<nav class="wiki-toc" aria-label="Contents"><details open><summary>Contents</summary><ol>']
        for (hid, text), subs in groups:
            sub = "<ol>%s</ol>" % "".join('<li><a href="#%s">%s</a></li>' % (h, t) for h, t in subs) if subs else ""
            out.append('<li><a href="#%s">%s</a>%s</li>' % (hid, text, sub))
        out.append("</ol></details></nav>")
        return "".join(out)

    def wiki_page(self, c, rel, side, parts, **kw):
        prefix = up(rel)
        main = self.sidebar_layout(side, '<article class="content">\n%s\n</article>' % "\n".join(x for x in parts if x),
                                   button="Browse the " + self.module_label("wiki", c).lower())
        return self.doc(c, rel=rel, main=main, full=True, current_mod="wiki", head_extra=self.wiki_head(c, prefix), **kw)

    def wiki_landing(self, c, rel, css=None, base=None, intro=None):
        site = c["site"]
        prefix = up(rel)
        body = self.md(c, prefix).render(read_text(self.wiki_home_md()) if intro is None else intro)
        parts = [body]
        secs = [s for s in c["wiki"]["sections"] if wiki_published(s)]
        if site.get("wiki_cards") and secs:
            cards = []
            for s in secs:
                arts = wiki_published(s)
                lis = "".join('<li><a href="%s%s">%s</a></li>' % (prefix, self.wart_rel(s["slug"], a["slug"], c), esc(a["title"]))
                              for a in arts[:6])
                more = ('<a class="more" href="%s%s">%d more</a>' % (prefix, self.wsec_rel(s["slug"], c), len(arts) - 6)
                        if len(arts) > 6 else "")
                desc = "<p>%s</p>" % esc(s["description"]) if s.get("description") else ""
                cards.append('<div class="wiki-card"><h3><a href="%s%s">%s</a></h3>%s<ul>%s</ul>%s</div>'
                             % (prefix, self.wsec_rel(s["slug"], c), esc(s["title"]), desc, lis, more))
            parts.append('<h2>Sections</h2>\n<div class="wiki-cards">\n%s\n</div>' % "\n".join(cards))
        if site.get("wiki_recent"):
            recent = [(a, s) for s in c["wiki"]["sections"] for a in wiki_published(s)]
            recent.sort(key=lambda x: (x[0].get("updated", ""), x[0]["title"].lower()), reverse=True)
            if recent:
                lis = ['<li><a href="%s%s">%s</a><span class="when">%s, %s</span></li>'
                       % (prefix, self.wart_rel(s["slug"], a["slug"], c), esc(a["title"]), esc(s["title"]), esc(nice_date(a["updated"])))
                       for a, s in recent[:6]]
                parts.append('<h2>Recently updated</h2>\n<ul class="article-list">\n%s\n</ul>' % "\n".join(lis))
        label = self.module_label("wiki", c)
        return self.wiki_page(c, rel, self.wiki_sidebar(c, prefix, home=True), parts,
                              body_class="mod-wiki wiki-home", title="" if rel == "index.html" else label,
                              desc=auto_summary(body) or site.get("description"), css=css, base=base,
                              canonical_rel="index.html" if c["home"] == "wiki" else None)

    def render_section(self, c, sec, markdown, css=None, base=None):
        rel = self.wsec_rel(sec["slug"], c)
        prefix = up(rel)
        intro = self.md(c, prefix).render(markdown)
        arts = wiki_published(sec)
        parts = [self.breadcrumbs([(self.module_label("wiki", c), prefix + self.landing_rel("wiki", c)), (sec["title"], "")]),
                 "<h1>%s</h1>" % esc(sec["title"])]
        if sec.get("description"):
            parts.append('<p class="lead">%s</p>' % esc(sec["description"]))
        parts.append(intro)
        if arts:
            lis = ['<li><a href="%s.html">%s</a>%s</li>' % (attr(a["slug"]), esc(a["title"]),
                                                          "<p>%s</p>" % esc(a["description"]) if a.get("description") else "")
                   for a in arts]
            parts.append('<h2>Articles</h2>\n<ul class="article-list">\n%s\n</ul>' % "\n".join(lis))
        else:
            parts.append('<p class="empty">No articles in this section yet.</p>')
        return self.wiki_page(c, rel, self.wiki_sidebar(c, prefix, sec["slug"]), parts,
                              body_class="mod-wiki wiki-section sec-" + sec["slug"], title=sec["title"],
                              desc=sec.get("description") or auto_summary(intro) or "Articles in %s." % sec["title"],
                              css=css, base=base)

    def render_article(self, c, sec, art, markdown, css=None, base=None):
        site = c["site"]
        rel = self.wart_rel(sec["slug"], art["slug"], c)
        prefix = up(rel)
        M = self.md(c, prefix)
        body = M.render(strip_title_h1(markdown, art["title"]))
        parts = [self.breadcrumbs([(self.module_label("wiki", c), prefix + self.landing_rel("wiki", c)),
                                   (sec["title"], "index.html"), (art["title"], "")]),
                 "<h1>%s</h1>" % esc(art["title"])]
        if site.get("toc"):
            parts.append(self.toc_html(M.headings))
        parts.append(body)
        if site.get("updated"):
            parts.append('<p class="article-meta">Last updated <time datetime="%s">%s</time> in <a href="index.html">%s</a></p>'
                         % (attr(art["updated"]), esc(nice_date(art["updated"])), esc(sec["title"])))
        arts = wiki_published(sec)
        slugs = [a["slug"] for a in arts]
        if art["slug"] in slugs:
            i = slugs.index(art["slug"])
            pager = []
            if i > 0:
                pager.append('<a class="prev" href="%s.html"><small>Previous</small>%s</a>' % (attr(arts[i - 1]["slug"]), esc(arts[i - 1]["title"])))
            if i + 1 < len(arts):
                pager.append('<a class="next" href="%s.html"><small>Next</small>%s</a>' % (attr(arts[i + 1]["slug"]), esc(arts[i + 1]["title"])))
            if pager:
                parts.append('<nav class="pager" aria-label="More in this section">%s</nav>' % "".join(pager))
        self._last_body = body
        return self.wiki_page(c, rel, self.wiki_sidebar(c, prefix, sec["slug"], art["slug"]), parts,
                              body_class="mod-wiki wiki-article sec-%s art-%s-%s" % (sec["slug"], sec["slug"], art["slug"]),
                              title=art["title"], desc=art.get("description") or auto_summary(body), og_type="article",
                              published=art.get("updated", ""), css=css, base=base)

    def wiki_files(self, c):
        files = {}
        if self.mbase("wiki", c):
            files[self.mbase("wiki", c) + "index.html"] = self.wiki_landing(c, self.mbase("wiki", c) + "index.html")
        entries = []
        for s in c["wiki"]["sections"]:
            intro_md = read_text(self.wsec_md(s["slug"]))
            files[self.wsec_rel(s["slug"], c)] = self.render_section(c, s, intro_md)
            entries.append({"t": s["title"], "s": "Section", "u": self.wsec_rel(s["slug"], c), "d": s.get("description", ""),
                            "x": plain_text(Markdown("").render(intro_md))[:2000]})
            for a in wiki_published(s):
                files[self.wart_rel(s["slug"], a["slug"], c)] = self.render_article(c, s, a, read_text(self.wart_md(s["slug"], a["slug"])))
                entries.append({"t": a["title"], "s": s["title"], "u": self.wart_rel(s["slug"], a["slug"], c),
                                "d": a.get("description", ""), "x": plain_text(self._last_body)[:SEARCH_TEXT_LIMIT]})
        if c["site"].get("search") == "builtin":
            data = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
            files[self.mbase("wiki", c) + "search.js"] = ("/* generated by SiteForge: search index and script */\n"
                                                         + SEARCH_JS.replace("__INDEX__", data))
        return files

    def wiki_sitemap(self, c):
        rows = []
        if c["home"] != "wiki":
            rows.append((self.landing_rel("wiki", c), ""))
        for s in c["wiki"]["sections"]:
            rows.append((self.wsec_rel(s["slug"], c), ""))
            rows += [(self.wart_rel(s["slug"], a["slug"], c), a["updated"]) for a in wiki_published(s)]
        return rows

    # ---- API
    def get_wiki_home(self):
        return {"markdown": read_text(self.wiki_home_md())}

    def save_wiki_home(self, d):
        write_text(self.wiki_home_md(), str(d.get("markdown") or ""))
        self.build()
        return {"ok": True}

    def add_section(self, title):
        title = str(title or "").strip()[:120]
        if not title:
            raise ApiError("Give the section a name.")
        slug = self.unique_section_slug(title)
        self.cfg["wiki"]["sections"].append({"slug": slug, "title": title, "description": "", "articles": []})
        write_text(self.wsec_md(slug), "")
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_section(self, slug):
        s = self.get_section_rec(slug)
        return {"section": s, "markdown": read_text(self.wsec_md(slug))}

    def save_section(self, slug, d):
        s = self.get_section_rec(slug)
        s["title"] = str(d.get("title") or "").strip()[:120] or s["title"]
        s["description"] = str(d.get("description") or "").strip()[:320]
        wanted = slugify(str(d.get("new_slug") or "")) or s["slug"]
        if wanted != s["slug"]:
            if wanted != self.unique_section_slug(wanted, exclude=s["slug"]):
                raise ApiError("The address %s is already used or reserved." % wanted)
            rename_path(self.wsec_dir(s["slug"]), self.wsec_dir(wanted))
            s["slug"] = wanted
        write_text(self.wsec_md(s["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": s["slug"]}

    def delete_section(self, slug):
        s = self.get_section_rec(slug)
        self.cfg["wiki"]["sections"].remove(s)
        self.to_trash(self.wsec_dir(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def move_section(self, slug, direction):
        secs = self.cfg["wiki"]["sections"]
        s = self.get_section_rec(slug)
        i = secs.index(s)
        secs.insert(max(0, min(len(secs) - 1, i + (1 if int(direction) > 0 else -1))), secs.pop(i))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def order_articles(self, slug, order):
        s = self.get_section_rec(slug)
        by = {a["slug"]: a for a in s["articles"]}
        s["articles"] = [by.pop(x) for x in (order if isinstance(order, list) else []) if x in by] + list(by.values())
        self.save_cfg()
        self.build()
        return {"ok": True}

    def get_article(self, sslug, aslug):
        s, a = self.get_article_rec(sslug, aslug)
        return {"section": s["slug"], "article": a, "markdown": read_text(self.wart_md(s["slug"], a["slug"]))}

    def save_article(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the article a title.")
        target = self.find_section(str(d.get("section") or ""))
        if not target:
            raise ApiError("Pick a section for this article.")
        md = str(d.get("markdown") or "")
        wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "article"
        orig_sec, orig_slug = str(d.get("orig_section") or ""), str(d.get("orig_slug") or "")
        if orig_slug:
            osec, art = self.get_article_rec(orig_sec, orig_slug)
            old_path = self.wart_md(osec["slug"], art["slug"])
            changed = read_text(old_path) != md or art["title"] != title
            same = osec is target
            if not same or wanted != art["slug"]:
                new_slug = self.unique_article_slug(target, wanted, exclude=art["slug"] if same else None)
                if not same:
                    osec["articles"].remove(art)
                    target["articles"].append(art)
                art["slug"] = new_slug
            new_path = self.wart_md(target["slug"], art["slug"])
            write_text(new_path, md)
            if old_path != new_path and old_path.exists():
                old_path.unlink()
        else:
            art = {"slug": self.unique_article_slug(target, wanted), "draft": False}
            target["articles"].append(art)
            changed = True
            write_text(self.wart_md(target["slug"], art["slug"]), md)
        art.update(title=title, description=str(d.get("description") or "").strip()[:320], draft=bool(d.get("draft")))
        if changed or not art.get("updated"):
            art["updated"] = today()
        self.save_cfg()
        self.build()
        return {"section": target["slug"], "slug": art["slug"], "article": art}

    def delete_article(self, sslug, aslug):
        s, a = self.get_article_rec(sslug, aslug)
        s["articles"].remove(a)
        self.to_trash(self.wart_md(s["slug"], a["slug"]))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def wiki_preview(self, c, p, md, css, base):
        view = p.get("view")
        if view == "wikihome":
            rel = self.landing_rel("wiki", c)
            return self.wiki_landing(c, rel, css=css, base=preview_base(rel), intro=md)
        if view == "section":
            sec = self.find_section(str(p.get("slug") or ""), c)
            if not sec:
                raise ApiError("That section does not exist.", 404)
            sec["title"] = str(p.get("title") or "").strip() or sec["title"]
            sec["description"] = str(p.get("description") or "").strip()
            return self.render_section(c, sec, md, css=css, base=preview_base(self.wsec_rel(sec["slug"], c)))
        target = self.find_section(str(p.get("section") or ""), c)
        if not target:
            raise ApiError("Create a wiki section first. Articles live inside sections.", 409)
        osec = self.find_section(str(p.get("orig_section") or ""), c)
        orig, pos = None, None
        if osec:
            orig = next((a for a in osec["articles"] if a["slug"] == str(p.get("orig_slug") or "")), None)
            if orig:
                pos = osec["articles"].index(orig) if osec is target else None
                osec["articles"].remove(orig)
        art = dict(orig or {})
        art.update(slug=slugify(str(p.get("new_slug") or "")) or slugify(str(p.get("title") or "")) or "article",
                   title=str(p.get("title") or "").strip() or "Untitled article",
                   description=str(p.get("description") or "").strip(), draft=False, updated=art.get("updated") or today())
        if pos is None:
            target["articles"].append(art)
        else:
            target["articles"].insert(pos, art)
        self._res = (None, None)
        return self.render_article(c, target, art, md, css=css, base=preview_base(self.wart_rel(target["slug"], art["slug"], c)))

    def wiki_preview_target(self, c, rest, css):
        if not rest:
            rel = self.landing_rel("wiki", c)
            return self.wiki_landing(c, rel, css=css, base=preview_base(rel))
        sslug, _, aslug = rest.partition("/")
        sec = self.find_section(sslug, c)
        if not sec:
            return None
        if not aslug:
            return self.render_section(c, sec, read_text(self.wsec_md(sslug)), css=css, base=preview_base(self.wsec_rel(sslug, c)))
        art = next((a for a in sec["articles"] if a["slug"] == aslug), None)
        if art:
            return self.render_article(c, sec, art, read_text(self.wart_md(sslug, aslug)), css=css,
                                       base=preview_base(self.wart_rel(sslug, aslug, c)))
        return None


# ----------------------------------------------------------------------------
# Built-in search (the only script in a generated wiki; Settings can turn it off)
# ----------------------------------------------------------------------------

SEARCH_JS = r"""(function () {
  "use strict";
  var INDEX = __INDEX__;
  var box = document.querySelector(".search[data-root]");
  var input = document.getElementById("wiki-search");
  var list = document.getElementById("wiki-results");
  if (!box || !input || !list) return;
  var root = box.getAttribute("data-root") || "";
  var active = -1, links = [];

  function low(s) { return (s || "").toLowerCase(); }
  INDEX.forEach(function (e) { e._t = low(e.t); e._s = low(e.s); e._d = low(e.d); e._x = low(e.x); });

  function hits(hay, term) {
    var n = 0, i = hay.indexOf(term);
    while (i !== -1 && n < 25) { n++; i = hay.indexOf(term, i + term.length); }
    return n;
  }

  function score(e, terms) {
    var total = 0;
    for (var i = 0; i < terms.length; i++) {
      var t = terms[i];
      var a = hits(e._t, t), b = hits(e._s, t), c = hits(e._d, t), d = hits(e._x, t);
      if (!a && !b && !c && !d) return 0;
      total += a * 25 + (e._t.indexOf(t) === 0 ? 15 : 0) + b * 5 + c * 8 + d;
    }
    return total;
  }

  function snippet(e, terms) {
    var at = -1;
    terms.forEach(function (t) { var k = e._x.indexOf(t); if (k !== -1 && (at === -1 || k < at)) at = k; });
    if (at === -1) return e.d || e.x.slice(0, 110);
    var start = Math.max(0, at - 35);
    var s = e.x.slice(start, start + 110);
    return (start > 0 ? "..." : "") + s + (start + 110 < e.x.length ? "..." : "");
  }

  function marked(el, text, terms) {
    var l = low(text), pos = 0;
    while (pos < text.length) {
      var best = -1, len = 0;
      terms.forEach(function (t) {
        var k = l.indexOf(t, pos);
        if (k !== -1 && (best === -1 || k < best || (k === best && t.length > len))) { best = k; len = t.length; }
      });
      if (best === -1) { el.appendChild(document.createTextNode(text.slice(pos))); break; }
      if (best > pos) el.appendChild(document.createTextNode(text.slice(pos, best)));
      var m = document.createElement("mark");
      m.textContent = text.substr(best, len);
      el.appendChild(m);
      pos = best + len;
    }
  }

  function span(cls, text, terms) {
    var s = document.createElement("span");
    s.className = cls;
    if (terms) marked(s, text, terms); else s.textContent = text;
    return s;
  }

  function render(q) {
    var terms = low(q).split(/\s+/).filter(Boolean);
    list.innerHTML = "";
    active = -1;
    links = [];
    if (!terms.length) { list.hidden = true; return; }
    var found = [];
    INDEX.forEach(function (e) { var s = score(e, terms); if (s) found.push([s, e]); });
    found.sort(function (a, b) { return b[0] - a[0] || a[1].t.localeCompare(b[1].t); });
    if (!found.length) {
      var li = document.createElement("li");
      li.className = "search-empty";
      li.textContent = "No matches";
      list.appendChild(li);
    }
    found.slice(0, 12).forEach(function (r) {
      var e = r[1];
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = root + e.u;
      a.appendChild(span("r-title", e.t, terms));
      a.appendChild(span("r-sec", e.s));
      a.appendChild(span("r-snip", snippet(e, terms), terms));
      li.appendChild(a);
      list.appendChild(li);
      links.push(a);
    });
    list.hidden = false;
  }

  function setActive(i) {
    links.forEach(function (a, k) { a.className = k === i ? "active" : ""; });
    active = i;
    if (links[i]) links[i].scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("input", function () { render(input.value); });
  input.addEventListener("focus", function () { if (input.value) render(input.value); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown") { ev.preventDefault(); if (links.length) setActive(Math.min(active + 1, links.length - 1)); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); if (links.length) setActive(Math.max(active - 1, 0)); }
    else if (ev.key === "Enter") { if (links.length) { ev.preventDefault(); window.location.href = links[active < 0 ? 0 : active].href; } }
    else if (ev.key === "Escape") { input.value = ""; render(""); }
  });
  document.addEventListener("keydown", function (ev) {
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    if (ev.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) { ev.preventDefault(); input.focus(); }
  });
  document.addEventListener("click", function (ev) { if (!box.contains(ev.target)) list.hidden = true; });
})();
"""


# ----------------------------------------------------------------------------
# Library module: short stories and serials
# ----------------------------------------------------------------------------

def count_words(markdown):
    return len(re.findall(r"[^\s]+", plain_text(Markdown("").render(str(markdown or "")))))


def lib_visible(c):
    return [s for s in c["library"]["stories"] if not s.get("draft")]


def ch_visible(story):
    return [ch for ch in story.get("chapters", []) if not ch.get("draft")]


class LibraryMixin:
    def lib_home_md(self):
        return self.meta / "library" / "home.md"

    def story_dir(self, s):
        return self.meta / "library" / "stories" / s

    def story_md(self, s):
        return self.story_dir(s) / "_index.md"

    def chapter_md(self, s, ch):
        return self.story_dir(s) / (ch + ".md")

    def story_rel(self, slug, c=None):
        return self.mbase("library", c) + "%s/index.html" % slug

    def chapter_rel(self, story, ch, c=None):
        return self.mbase("library", c) + "%s/%s.html" % (story["slug"], ch["slug"])

    def genre_rel(self, name, c=None):
        return self.mbase("library", c) + "genre/%s.html" % slugify(name)

    def normalize_library(self):
        stories, seen = [], set()
        for s in self.cfg["library"]["stories"]:
            if not isinstance(s, dict) or not slugify(s.get("slug", "")) or s["slug"] in seen:
                continue
            s["slug"] = slugify(s["slug"])
            seen.add(s["slug"])
            s["title"] = str(s.get("title") or title_from_name(s["slug"]))
            for k in ("subtitle", "author", "blurb", "cover", "warnings", "series", "series_no", "description"):
                s[k] = str(s.get(k) or "")
            s["status"] = s.get("status") if s.get("status") in STATUS_LABELS else "ongoing"
            s["kind"] = "single" if s.get("kind") == "single" else "chaptered"
            s["genres"] = clean_tags(s.get("genres"), limit=8)
            s["draft"] = bool(s.get("draft"))
            s["started"] = clean_date(s.get("started"))
            s["completed"] = str(s.get("completed") or "")
            chs, cseen = [], set()
            for ch in s.get("chapters") if isinstance(s.get("chapters"), list) else []:
                if not isinstance(ch, dict) or not slugify(ch.get("slug", "")) or ch["slug"] in cseen:
                    continue
                cseen.add(ch["slug"])
                ch["title"] = str(ch.get("title") or title_from_name(ch["slug"]))
                ch["note"] = str(ch.get("note") or "")
                ch["draft"] = bool(ch.get("draft"))
                ch["date"] = clean_date(ch.get("date"))
                ch["words"] = int(ch.get("words") or 0)
                chs.append(ch)
            s["chapters"] = chs
            stories.append(s)
        self.cfg["library"]["stories"] = stories

    def find_story(self, slug, c=None):
        return next((s for s in (c or self.cfg)["library"]["stories"] if s["slug"] == slug), None)

    def get_story_rec(self, slug):
        s = self.find_story(slug)
        if not s:
            raise ApiError("That story does not exist.", 404)
        return s

    def get_chapter_rec(self, sslug, cslug):
        s = self.get_story_rec(sslug)
        ch = next((x for x in s["chapters"] if x["slug"] == cslug), None)
        if not ch:
            raise ApiError("That chapter does not exist.", 404)
        return s, ch

    def unique_story_slug(self, base, exclude=None):
        base = slugify(base) or "story"
        taken = {s["slug"] for s in self.cfg["library"]["stories"] if s["slug"] != exclude} | RESERVED
        if self.cfg["modules"]["library"]["mount"] == "":
            taken |= self.taken_root_names(exclude=exclude)
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.story_dir(slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def unique_chapter_slug(self, story, base, exclude=None):
        base = slugify(base) or "chapter"
        taken = {ch["slug"] for ch in story["chapters"] if ch["slug"] != exclude} | {"index", "_index"}
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.chapter_md(story["slug"], slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def lib_count_words(self, c):
        for s in c["library"]["stories"]:
            if s["kind"] == "single":
                s["words"] = count_words(read_text(self.story_md(s["slug"])))
            else:
                s.pop("words", None)
                for ch in s["chapters"]:
                    ch["words"] = count_words(read_text(self.chapter_md(s["slug"], ch["slug"])))

    def story_words(self, s):
        return s.get("words", 0) if s["kind"] == "single" else sum(ch.get("words", 0) for ch in ch_visible(s))

    def lib_recent(self, c, limit):
        rows = [(s, ch) for s in lib_visible(c) for ch in ch_visible(s)]
        rows.sort(key=lambda x: (x[1]["date"], x[1]["title"]), reverse=True)
        return rows[:limit]

    def genre_map(self, c):
        out = {}
        for s in lib_visible(c):
            for g in s.get("genres", []):
                out.setdefault(slugify(g), {"name": g, "stories": []})["stories"].append(s)
        return dict(sorted(out.items()))

    # ---- rescan
    def lib_rescan(self):
        base = self.meta / "library" / "stories"
        base.mkdir(parents=True, exist_ok=True)
        r = {"stories_added": 0, "chapters_added": 0, "chapters_removed": 0}
        known = {s["slug"] for s in self.cfg["library"]["stories"]}
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith(".") or d.name in known:
                continue
            slug = self.unique_story_slug(d.name, exclude=d.name if slugify(d.name) == d.name else None)
            if slug != d.name:
                if (base / slug).exists() and not (base / slug).samefile(d):
                    continue
                rename_path(d, base / slug)
            self.cfg["library"]["stories"].append({
                "slug": slug, "title": title_from_name(d.name), "subtitle": "", "author": "", "status": "ongoing",
                "kind": "chaptered", "blurb": "", "cover": "", "genres": [], "warnings": "", "series": "", "series_no": "",
                "started": today(), "completed": "", "draft": False, "description": "", "chapters": []})
            known.add(slug)
            r["stories_added"] += 1
        for st in self.cfg["library"]["stories"]:
            d = self.story_dir(st["slug"])
            d.mkdir(parents=True, exist_ok=True)
            chs = {ch["slug"]: ch for ch in st["chapters"]}
            for f in sorted(d.glob("*.md"), key=lambda p: p.name.lower()):
                if f.name == "_index.md" or f.name.startswith(".") or f.stem in chs:
                    continue
                orig = f.stem
                slug = orig if slugify(orig) == orig and orig not in ("index", "_index") else None
                if slug is None:
                    slug = self.unique_chapter_slug(st, orig)
                    target = self.chapter_md(st["slug"], slug)
                    if target.exists() and not target.samefile(f):
                        continue
                    rename_path(f, target)
                    f = target
                ch = {"slug": slug, "title": first_h1(read_text(f)) or title_from_name(orig), "date": file_date(f),
                      "draft": False, "note": "", "words": 0}
                st["chapters"].append(ch)
                chs[slug] = ch
                r["chapters_added"] += 1
            keep = [ch for ch in st["chapters"] if self.chapter_md(st["slug"], ch["slug"]).is_file()]
            r["chapters_removed"] += len(st["chapters"]) - len(keep)
            st["chapters"] = keep
            if st["chapters"] and st["kind"] == "single":
                st["kind"] = "chaptered"
        return r

    # ---- rendering
    def lib_classes(self, c, extra):
        site = c["site"]
        cls = ["mod-library", "prose-" + site.get("prose", "indented")] + extra
        if site.get("dropcap"):
            cls.append("dropcap")
        return " ".join(cls)

    @staticmethod
    def cover_src(story, prefix):
        url = fix_url(story.get("cover", ""), prefix) if story.get("cover") else ""
        return "" if url in ("", "#") else url

    def story_facts(self, c, story, prefix, words=None, chapters=None):
        site = c["site"]
        out = ['<li><span class="tag status">%s</span></li>' % esc(STATUS_LABELS[story["status"]])]
        for g in story.get("genres", []):
            out.append('<li><a class="tag" href="%s%s">%s</a></li>' % (prefix, attr(self.genre_rel(g, c)), esc(g)))
        if chapters is not None and story["kind"] == "chaptered":
            out.append("<li>%d chapter%s</li>" % (chapters, "" if chapters == 1 else "s"))
        if words and site.get("wordcount"):
            out.append("<li>%s</li>" % esc(nice_words(words)))
        if words and site.get("reading_time"):
            out.append("<li>%s</li>" % esc(reading_time(words, site.get("wpm", 240))))
        if story.get("series"):
            out.append("<li>%s</li>" % esc(story["series"] + (" #%s" % story["series_no"] if story.get("series_no") else "")))
        return '<ul class="facts">%s</ul>' % "".join(out)

    def book_card(self, c, s, prefix):
        src = self.cover_src(s, prefix)
        cover = ('<img class="cover" src="%s" alt="Cover of %s" loading="lazy">' % (attr(src), attr(s["title"]))
                 if src else '<span class="cover none">%s</span>' % esc(s["title"]))
        href = prefix + self.story_rel(s["slug"], c)
        return ('<article class="book">\n<a href="%s" tabindex="-1" aria-hidden="true">%s</a>\n<div>\n<h2><a href="%s">%s</a></h2>\n%s%s%s</div>\n</article>'
                % (attr(href), cover, attr(href), esc(s["title"]),
                   '<p class="subtitle">%s</p>' % esc(s["subtitle"]) if s.get("subtitle") else "",
                   "<p>%s</p>" % esc(s["blurb"]) if s.get("blurb") else "",
                   self.story_facts(c, s, prefix, self.story_words(s), len(ch_visible(s)))))

    def chapters_nav(self, c, story, prefix, current=None):
        author = story.get("author") or c["site"].get("author") or ""
        items = ['<li><a href="%s.html"%s>%s</a></li>' % (attr(ch["slug"]), ' aria-current="page"' if ch["slug"] == current else "",
                                                        esc(ch["title"])) for ch in ch_visible(story)]
        return ('<nav class="side chapters-nav" aria-label="Chapters">\n<a class="in-story" href="index.html">%s</a>%s\n<ol>\n%s\n</ol>\n'
                '<a class="all-stories" href="%s%s">All stories</a>\n</nav>'
                % (esc(story["title"]), '<span class="by">%s</span>' % esc(author) if author else "",
                   "\n".join(items) or '<li class="nav-empty">No chapters yet.</li>', prefix, self.landing_rel("library", c)))

    def lib_landing(self, c, rel, css=None, base=None, intro=None):
        site = c["site"]
        prefix = up(rel)
        body = self.md(c, prefix).render(read_text(self.lib_home_md()) if intro is None else intro)
        label = self.module_label("library", c)
        parts = ['<main id="main" class="wide">']
        if rel != "index.html":
            parts.append('<h1 class="page-title">%s</h1>' % esc(label))
        if body:
            parts.append('<div class="prose">%s</div>' % body)
        cards = [self.book_card(c, s, prefix) for s in lib_visible(c)]
        parts.append('<h2 class="section-title">Stories</h2>\n<div class="shelf">\n%s\n</div>' % "\n".join(cards)
                     if cards else '<p class="empty">No stories yet.</p>')
        if site.get("lib_updates"):
            recent = self.lib_recent(c, 6)
            if recent:
                lis = ['<li><a href="%s%s">%s</a><span class="when">%s, %s</span></li>'
                       % (prefix, self.chapter_rel(s, ch, c), esc(ch["title"]), esc(s["title"]), esc(nice_date(ch["date"])))
                       for s, ch in recent]
                parts.append('<h2 class="section-title">Latest chapters</h2>\n<ul class="updates">\n%s\n</ul>' % "\n".join(lis))
        parts.append("</main>")
        return self.doc(c, rel=rel, body_class=self.lib_classes(c, ["lib-home"]), title="" if rel == "index.html" else label,
                        main="\n".join(parts), full=True, desc=auto_summary(body) or site.get("description") or site.get("tagline"),
                        current_mod="library", css=css, base=base,
                        canonical_rel="index.html" if c["home"] == "library" else None)

    def render_story(self, c, story, markdown, css=None, base=None):
        site = c["site"]
        rel = self.story_rel(story["slug"], c)
        prefix = up(rel)
        body = self.md(c, prefix).render(markdown)
        chs = ch_visible(story)
        words = self.story_words(story)
        author = story.get("author") or site.get("author") or ""
        head = ['<div class="story-head">']
        src = self.cover_src(story, prefix)
        if src:
            head.append('<img class="cover" src="%s" alt="Cover of %s">' % (attr(src), attr(story["title"])))
        head.append('<div class="about">\n<h1>%s</h1>' % esc(story["title"]))
        if story.get("subtitle"):
            head.append('<p class="subtitle">%s</p>' % esc(story["subtitle"]))
        if author:
            head.append('<p class="byline">by %s</p>' % esc(author))
        head.append(self.story_facts(c, story, prefix, words, len(chs)))
        if story.get("blurb"):
            head.append('<p class="blurb">%s</p>' % esc(story["blurb"]))
        if story.get("warnings"):
            head.append('<p class="warnings"><strong>Content notes:</strong> %s</p>' % esc(story["warnings"]))
        if story["kind"] == "chaptered" and chs:
            head.append('<p><a class="read-button" href="%s.html">Start reading</a></p>' % attr(chs[0]["slug"]))
        head.append("</div>\n</div>")
        parts = list(head)
        if story["kind"] == "single":
            if body:
                parts.append('<article class="prose opening">\n%s\n</article>' % body)
                if story["status"] == "complete":
                    parts.append('<p class="the-end">The end</p>')
        else:
            if body:
                parts.append('<div class="prose foreword">%s</div>' % body)
            if chs:
                lis = []
                for ch in chs:
                    meta = ([nice_words(ch["words"])] if site.get("wordcount") and ch.get("words") else []) + [nice_date(ch["date"])]
                    lis.append('<li><a href="%s.html"><span class="ch-title">%s</span><span class="ch-meta">%s</span></a></li>'
                               % (attr(ch["slug"]), esc(ch["title"]), esc(" \u00b7 ".join(meta))))
                parts.append('<section class="chapter-list">\n<h2>Chapters</h2>\n<ol>\n%s\n</ol>\n</section>' % "\n".join(lis))
            else:
                parts.append('<p class="empty">No chapters published yet.</p>')
        inner = '<div class="sheet">\n%s\n</div>' % "\n".join(parts)
        if story["kind"] == "chaptered":
            main = self.sidebar_layout(self.chapters_nav(c, story, prefix), inner, button="Chapters")
        else:
            main = '<main id="main" class="wrap page-main">\n%s\n</main>' % inner
        return self.doc(c, rel=rel, body_class=self.lib_classes(c, ["lib-story", "story-" + story["slug"]]),
                        title=story["title"], desc=story.get("description") or story.get("blurb") or auto_summary(body),
                        main=main, full=True, og_type="article", image=story.get("cover"), current_mod="library",
                        css=css, base=base)

    def render_chapter(self, c, story, ch, markdown, css=None, base=None):
        site = c["site"]
        rel = self.chapter_rel(story, ch, c)
        prefix = up(rel)
        body = self.md(c, prefix).render(strip_title_h1(markdown, ch["title"]))
        chs = ch_visible(story)
        slugs = [x["slug"] for x in chs]
        n = slugs.index(ch["slug"]) + 1 if ch["slug"] in slugs else 0
        meta = ["Chapter %d of %d" % (n, len(chs))] if n else []
        if site.get("wordcount") and ch.get("words"):
            meta.append(nice_words(ch["words"]))
        if site.get("reading_time") and ch.get("words"):
            meta.append(reading_time(ch["words"], site.get("wpm", 240)))
        meta.append(nice_date(ch["date"]))
        parts = ['<header class="chapter-head">\n<span class="in"><a href="index.html">%s</a></span>\n<h1>%s</h1>\n<p class="meta">%s</p>\n</header>'
                 % (esc(story["title"]), esc(ch["title"]), esc(" \u00b7 ".join(meta))),
                 '<article class="prose opening">\n%s\n</article>' % body]
        if ch.get("note"):
            parts.append('<aside class="note prose">\n<h2>Author\u2019s note</h2>\n%s\n</aside>' % Markdown(prefix).render(ch["note"]))
        if n and n == len(chs) and story["status"] == "complete":
            parts.append('<p class="the-end">The end</p>')
        pager = []
        if n > 1:
            pager.append('<a class="prev" href="%s.html"><small>Previous</small>%s</a>' % (attr(chs[n - 2]["slug"]), esc(chs[n - 2]["title"])))
        if n and n < len(chs):
            pager.append('<a class="next" href="%s.html"><small>Next</small>%s</a>' % (attr(chs[n]["slug"]), esc(chs[n]["title"])))
        if pager:
            parts.append('<nav class="pager" aria-label="Chapters">%s</nav>' % "".join(pager))
        main = self.sidebar_layout(self.chapters_nav(c, story, prefix, ch["slug"]),
                                   '<div class="sheet">\n%s\n</div>' % "\n".join(parts), button="Chapters")
        return self.doc(c, rel=rel, body_class=self.lib_classes(c, ["lib-chapter", "story-" + story["slug"], "chapter-" + ch["slug"]]),
                        title="%s | %s" % (ch["title"], story["title"]), desc=auto_summary(body) or story.get("blurb", ""),
                        main=main, full=True, og_type="article", published=ch["date"], image=story.get("cover"),
                        current_mod="library", css=css, base=base)

    def render_genre(self, c, name, stories, css=None, base=None):
        rel = self.genre_rel(name, c)
        prefix = up(rel)
        main = ('<main id="main" class="wide">\n<h1 class="page-title">%s</h1>\n<div class="shelf">\n%s\n</div>\n'
                '<p><a href="%s%s">All stories</a></p>\n</main>'
                % (esc(name), "\n".join(self.book_card(c, s, prefix) for s in stories), prefix, self.landing_rel("library", c)))
        return self.doc(c, rel=rel, body_class=self.lib_classes(c, ["lib-genre", "genre-" + slugify(name)]), title=name,
                        desc="Stories tagged %s." % name, main=main, full=True, current_mod="library", css=css, base=base)

    def lib_files(self, c):
        files = {}
        mb = self.mbase("library", c)
        if mb:
            files[mb + "index.html"] = self.lib_landing(c, mb + "index.html")
        for s in lib_visible(c):
            files[self.story_rel(s["slug"], c)] = self.render_story(c, s, read_text(self.story_md(s["slug"])))
            for ch in ch_visible(s):
                files[self.chapter_rel(s, ch, c)] = self.render_chapter(c, s, ch, read_text(self.chapter_md(s["slug"], ch["slug"])))
        for g in self.genre_map(c).values():
            files[self.genre_rel(g["name"], c)] = self.render_genre(c, g["name"], g["stories"])
        site = c["site"]
        if mb and site.get("feeds") and site.get("url"):
            files[mb + "rss.xml"] = self.feed_xml(c, self.feed_items(c, blog=False, library=True),
                                                  "%s: %s" % (site["title"], self.module_label("library", c)), mb + "rss.xml")
        return files

    def lib_sitemap(self, c):
        rows = [] if c["home"] == "library" else [(self.landing_rel("library", c), "")]
        for s in lib_visible(c):
            rows.append((self.story_rel(s["slug"], c), ""))
            rows += [(self.chapter_rel(s, ch, c), ch["date"]) for ch in ch_visible(s)]
        rows += [(self.genre_rel(g["name"], c), "") for g in self.genre_map(c).values()]
        return rows

    # ---- API
    def get_lib_home(self):
        return {"markdown": read_text(self.lib_home_md())}

    def save_lib_home(self, d):
        write_text(self.lib_home_md(), str(d.get("markdown") or ""))
        self.build()
        return {"ok": True}

    def add_story(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the story a title.")
        slug = self.unique_story_slug(title)
        self.cfg["library"]["stories"].append({
            "slug": slug, "title": title, "subtitle": "", "author": "", "status": "ongoing",
            "kind": "single" if d.get("kind") == "single" else "chaptered", "blurb": "", "cover": "", "genres": [],
            "warnings": "", "series": "", "series_no": "", "started": today(), "completed": "", "draft": False,
            "description": "", "chapters": []})
        write_text(self.story_md(slug), "")
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_story(self, slug):
        s = self.get_story_rec(slug)
        return {"story": s, "markdown": read_text(self.story_md(slug))}

    def save_story(self, slug, d):
        s = self.get_story_rec(slug)
        s["title"] = str(d.get("title") or "").strip()[:160] or s["title"]
        for k, limit in (("subtitle", 200), ("author", 120), ("blurb", 600), ("cover", 400), ("warnings", 400),
                         ("series", 120), ("series_no", 12), ("description", 320)):
            if k in d:
                s[k] = str(d.get(k) or "").strip()[:limit]
        if d.get("status") in STATUS_LABELS:
            s["status"] = d["status"]
        if d.get("kind") in ("single", "chaptered"):
            if d["kind"] == "single" and s["chapters"]:
                raise ApiError("This story has chapters, so it cannot become a single piece. Delete the chapters first.")
            s["kind"] = d["kind"]
        if "genres" in d:
            known = {g for st in self.cfg["library"]["stories"] for g in st.get("genres", [])}
            s["genres"] = clean_tags(d.get("genres"), known, limit=8)
        if "draft" in d:
            s["draft"] = bool(d["draft"])
        if "started" in d:
            s["started"] = clean_date(d.get("started"))
        if "completed" in d:
            done = str(d.get("completed") or "").strip()
            s["completed"] = clean_date(done) if done else ""
        wanted = slugify(str(d.get("new_slug") or "")) or s["slug"]
        if wanted != s["slug"]:
            if wanted != self.unique_story_slug(wanted, exclude=s["slug"]):
                raise ApiError("The address %s is already used or reserved." % wanted)
            rename_path(self.story_dir(s["slug"]), self.story_dir(wanted))
            s["slug"] = wanted
        write_text(self.story_md(s["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": s["slug"]}

    def delete_story(self, slug):
        s = self.get_story_rec(slug)
        self.cfg["library"]["stories"].remove(s)
        self.to_trash(self.story_dir(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def move_story(self, slug, direction):
        items = self.cfg["library"]["stories"]
        s = self.get_story_rec(slug)
        i = items.index(s)
        items.insert(max(0, min(len(items) - 1, i + (1 if int(direction) > 0 else -1))), items.pop(i))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def order_chapters(self, slug, order):
        s = self.get_story_rec(slug)
        by = {ch["slug"]: ch for ch in s["chapters"]}
        s["chapters"] = [by.pop(x) for x in (order if isinstance(order, list) else []) if x in by] + list(by.values())
        self.save_cfg()
        self.build()
        return {"ok": True}

    def get_chapter(self, sslug, cslug):
        s, ch = self.get_chapter_rec(sslug, cslug)
        return {"story": s["slug"], "chapter": ch, "markdown": read_text(self.chapter_md(s["slug"], ch["slug"]))}

    def save_chapter(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the chapter a title.")
        target = self.find_story(str(d.get("story") or ""))
        if not target:
            raise ApiError("Pick a story for this chapter.")
        if target["kind"] == "single":
            raise ApiError("%s is a single piece. Change it to chaptered in the story editor first." % target["title"])
        md = str(d.get("markdown") or "")
        wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "chapter"
        orig_story, orig_slug = str(d.get("orig_story") or ""), str(d.get("orig_slug") or "")
        if orig_slug:
            ostory, ch = self.get_chapter_rec(orig_story, orig_slug)
            old_path = self.chapter_md(ostory["slug"], ch["slug"])
            same = ostory is target
            if not same or wanted != ch["slug"]:
                new_slug = self.unique_chapter_slug(target, wanted, exclude=ch["slug"] if same else None)
                if not same:
                    ostory["chapters"].remove(ch)
                    target["chapters"].append(ch)
                ch["slug"] = new_slug
            new_path = self.chapter_md(target["slug"], ch["slug"])
            write_text(new_path, md)
            if old_path != new_path and old_path.exists():
                old_path.unlink()
        else:
            ch = {"slug": self.unique_chapter_slug(target, wanted), "date": today(), "draft": False, "note": "", "words": 0}
            target["chapters"].append(ch)
            write_text(self.chapter_md(target["slug"], ch["slug"]), md)
        ch["title"] = title
        ch["note"] = str(d.get("note") or "")[:4000]
        ch["draft"] = bool(d.get("draft"))
        if d.get("date"):
            ch["date"] = clean_date(d.get("date"))
        ch["words"] = count_words(md)
        self.save_cfg()
        self.build()
        return {"story": target["slug"], "slug": ch["slug"], "chapter": ch}

    def delete_chapter(self, sslug, cslug):
        s, ch = self.get_chapter_rec(sslug, cslug)
        s["chapters"].remove(ch)
        self.to_trash(self.chapter_md(s["slug"], ch["slug"]))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def lib_preview(self, c, p, md, css, base):
        view = p.get("view")
        if view == "libhome":
            rel = self.landing_rel("library", c)
            return self.lib_landing(c, rel, css=css, base=preview_base(rel), intro=md)
        if view == "story":
            s = self.find_story(str(p.get("slug") or ""), c)
            if not s:
                raise ApiError("That story does not exist.", 404)
            for k in ("title", "subtitle", "author", "blurb", "cover", "warnings", "series", "series_no"):
                if k in p:
                    s[k] = str(p.get(k) or "").strip()
            if p.get("status") in STATUS_LABELS:
                s["status"] = p["status"]
            if p.get("kind") in ("single", "chaptered") and not (p["kind"] == "single" and s["chapters"]):
                s["kind"] = p["kind"]
            if "genres" in p:
                s["genres"] = clean_tags(p.get("genres"), limit=8)
            s["draft"] = False
            if s["kind"] == "single":
                s["words"] = count_words(md)
            return self.render_story(c, s, md, css=css, base=preview_base(self.story_rel(s["slug"], c)))
        target = self.find_story(str(p.get("story") or ""), c)
        if not target:
            raise ApiError("Create a story first. Chapters live inside stories.", 409)
        ostory = self.find_story(str(p.get("orig_story") or ""), c)
        orig, pos = None, None
        if ostory:
            orig = next((x for x in ostory["chapters"] if x["slug"] == str(p.get("orig_slug") or "")), None)
            if orig:
                pos = ostory["chapters"].index(orig) if ostory is target else None
                ostory["chapters"].remove(orig)
        ch = dict(orig or {})
        ch.update(slug=slugify(str(p.get("new_slug") or "")) or slugify(str(p.get("title") or "")) or "chapter",
                  title=str(p.get("title") or "").strip() or "Untitled chapter", note=str(p.get("note") or ""), draft=False,
                  date=clean_date(p.get("date") or ch.get("date")), words=count_words(md))
        if pos is None:
            target["chapters"].append(ch)
        else:
            target["chapters"].insert(pos, ch)
        target["kind"] = "chaptered"
        return self.render_chapter(c, target, ch, md, css=css, base=preview_base(self.chapter_rel(target, ch, c)))

    def lib_preview_target(self, c, rest, css):
        if not rest:
            rel = self.landing_rel("library", c)
            return self.lib_landing(c, rel, css=css, base=preview_base(rel))
        sslug, _, cslug = rest.partition("/")
        s = self.find_story(sslug, c)
        if not s:
            return None
        self.lib_count_words(c)
        if not cslug:
            return self.render_story(c, s, read_text(self.story_md(sslug)), css=css, base=preview_base(self.story_rel(sslug, c)))
        ch = next((x for x in s["chapters"] if x["slug"] == cslug), None)
        if ch:
            return self.render_chapter(c, s, ch, read_text(self.chapter_md(sslug, cslug)), css=css,
                                       base=preview_base(self.chapter_rel(s, ch, c)))
        return None


# ----------------------------------------------------------------------------
# Importing sites made with SiteGen, WikiGen or StoryGen
#
# The source folder is only read. Markdown is copied as text (never as symlinks), slugs are
# re-checked, and anything that clashes with existing content gets a new address, which the
# summary reports. Generated .html in the source folder is ignored; everything is rebuilt.
# ----------------------------------------------------------------------------

IMPORT_KINDS = {".sitegen": "sitegen", ".wikigen": "wikigen", ".storygen": "storygen"}


def detect_import(path):
    root = Path(str(path or "")).expanduser()
    for d, kind in IMPORT_KINDS.items():
        f = root / d / "config.json"
        if f.is_file():
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ApiError("%s exists but is not valid JSON." % f)
            if not isinstance(cfg, dict):
                raise ApiError("%s is not a site settings file." % f)
            return kind, root, cfg
    raise ApiError("No SiteGen, WikiGen or StoryGen site found in %s. Pick the folder that contains the hidden "
                   ".sitegen, .wikigen or .storygen folder." % root)


def src_text(path):
    """Read a markdown file from the source, refusing symlinks and anything that is not a file."""
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        return ""
    return read_text(p)


class ImportMixin:
    def import_site(self, d):
        kind, src, scfg = detect_import(d.get("path"))
        if src.resolve() == self.root.resolve():
            raise ApiError("That is this site's own folder.")
        report = {"kind": kind, "notes": []}
        if kind == "sitegen":
            self._import_sitegen(src, scfg, report, bool(d.get("settings")))
        elif kind == "wikigen":
            self._import_wikigen(src, scfg, report, bool(d.get("settings")))
        else:
            self._import_storygen(src, scfg, report, bool(d.get("settings")))
        copied = self._import_assets(src)
        if copied:
            report["notes"].append("Copied %d file(s) from assets/." % copied)
        old_css = src / "style.css"
        if old_css.is_file() and not old_css.is_symlink():
            dest = self.meta / "imported" / ("%s-style.css" % kind)
            write_text(dest, read_text(old_css))
            report["notes"].append("Your old stylesheet was saved to %s for reference; it is not applied." % dest)
        home = d.get("home")
        if home in MODULES and self.enabled(home):
            self.cfg["home"] = home
            if home in ("wiki", "library") and d.get("root_mount"):
                self.cfg["modules"][home]["mount"] = ""
        self.normalize()
        self.save_cfg()
        self.build()
        report["state"] = self.state()
        return report

    def _copy_site_settings(self, s, keys):
        for k in keys:
            if k in s and s[k] not in (None, ""):
                self.cfg["site"][k] = s[k]

    def _import_assets(self, src):
        a = src / "assets"
        if not a.is_dir() or a.is_symlink():
            return 0
        n = 0
        dest = self.root / "assets"
        dest.mkdir(exist_ok=True)
        for f in sorted(a.iterdir()):
            if f.is_file() and not f.is_symlink() and not f.name.startswith(".") and not (dest / f.name).exists():
                shutil.copy2(f, dest / f.name, follow_symlinks=False)
                n += 1
        return n

    def _new_page_slug(self, wanted):
        taken = self.taken_root_names()
        slug, n = wanted, 2
        while slug in taken:
            slug = "%s-%d" % (wanted, n)
            n += 1
        return slug

    def _import_sitegen(self, src, scfg, report, settings):
        meta = src / ".sitegen"
        s = scfg.get("site") if isinstance(scfg.get("site"), dict) else {}
        if settings:
            self._copy_site_settings(s, ("title", "tagline", "author", "footer", "lang", "url", "description", "image",
                                         "twitter", "noindex", "sitemap"))
            if s.get("rss"):
                self.cfg["site"]["feeds"] = True
        pages = posts = 0
        for p in scfg.get("pages") if isinstance(scfg.get("pages"), list) else []:
            if not isinstance(p, dict) or not slugify(str(p.get("slug", ""))):
                continue
            slug = "index" if p.get("slug") == "index" else slugify(p["slug"])
            text = src_text(meta / "pages" / (slug + ".md"))
            seo = {k: p.get(k) for k in ("seo_title", "description", "image", "canonical", "noindex") if k in p}
            if p.get("kind") == "home" or slug == "index":
                write_text(self.page_md("index"), text)
                seo_fields(seo, self.find_page("index"))
            elif p.get("kind") == "blog":
                self.enable_module("blog")
                self.cfg["modules"]["blog"]["label"] = str(p.get("title") or "Blog")[:40]
                write_text(self.blog_intro_md(), text)
            else:
                new = self._new_page_slug(slug)
                if new != slug:
                    report["notes"].append("Page %s was renamed to %s because the name was taken." % (slug, new))
                rec = {"slug": new, "title": str(p.get("title") or title_from_name(new)), "show_title": bool(p.get("show_title", True))}
                seo_fields(seo, rec)
                self.cfg["pages"].append(rec)
                write_text(self.page_md(new), text)
                pages += 1
        existing = {p["slug"] for p in self.cfg["posts"]}
        for p in scfg.get("posts") if isinstance(scfg.get("posts"), list) else []:
            if not isinstance(p, dict) or not slugify(str(p.get("slug", ""))):
                continue
            self.enable_module("blog")
            slug = base = slugify(p["slug"])
            n = 2
            while slug in existing:
                slug = "%s-%d" % (base, n)
                n += 1
            existing.add(slug)
            published = p.get("status") == "published"
            text = src_text(meta / ("posts" if published else "drafts") / (p["slug"] + ".md")) or \
                src_text(meta / ("drafts" if published else "posts") / (p["slug"] + ".md"))
            rec = {"slug": slug, "title": str(p.get("title") or title_from_name(slug)), "date": clean_date(p.get("date")),
                   "summary": str(p.get("summary") or ""), "tags": clean_tags(p.get("tags")),
                   "status": "published" if published else "draft"}
            seo_fields({k: p.get(k) for k in ("seo_title", "description", "image", "canonical", "noindex") if k in p}, rec)
            self.cfg["posts"].append(rec)
            write_text(self.post_md(slug), text)
            posts += 1
        links = 0
        for n_ in scfg.get("nav") if isinstance(scfg.get("nav"), list) else []:
            if isinstance(n_, dict) and n_.get("type") == "link" and str(n_.get("url") or "").strip():
                self.cfg["nav"].append({"type": "link", "label": str(n_.get("label") or "Link")[:60],
                                        "url": str(n_["url"])[:400], "visible": bool(n_.get("visible", True))})
                links += 1
        report["notes"].insert(0, "Imported %d page(s), %d post(s) and %d menu link(s) from SiteGen." % (pages, posts, links))

    def _copy_tree_md(self, src_dir, dest_dir):
        n = 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not src_dir.is_dir() or src_dir.is_symlink():
            return 0
        for f in sorted(src_dir.glob("*.md")):
            if f.is_file() and not f.is_symlink() and not f.name.startswith("."):
                write_text(dest_dir / f.name, read_text(f))
                n += 1
        return n

    def _import_wikigen(self, src, scfg, report, settings):
        meta = src / ".wikigen"
        self.enable_module("wiki")
        s = scfg.get("site") if isinstance(scfg.get("site"), dict) else {}
        if settings:
            self._copy_site_settings(s, ("title", "tagline", "author", "footer", "lang", "url", "description", "noindex", "sitemap"))
        for k, dest in (("search", "search"), ("toc", "toc"), ("updated", "updated"), ("expand", "expand"),
                        ("home_sections", "wiki_cards"), ("home_recent", "wiki_recent")):
            if k in s:
                self.cfg["site"][dest] = s[k]
        text = src_text(meta / "home.md")
        if text:
            write_text(self.wiki_home_md(), text)
        secs = arts = 0
        for sec in scfg.get("sections") if isinstance(scfg.get("sections"), list) else []:
            if not isinstance(sec, dict) or not slugify(str(sec.get("slug", ""))):
                continue
            old = slugify(sec["slug"])
            new = self.unique_section_slug(old)
            if new != old:
                report["notes"].append("Wiki section %s was renamed to %s because the name was taken." % (old, new))
            self._copy_tree_md(meta / "sections" / sec["slug"], self.wsec_dir(new))
            rec = {"slug": new, "title": str(sec.get("title") or title_from_name(new)), "description": str(sec.get("description") or ""),
                   "articles": []}
            for a in sec.get("articles") if isinstance(sec.get("articles"), list) else []:
                if isinstance(a, dict) and slugify(str(a.get("slug", ""))) and self.wart_md(new, slugify(a["slug"])).is_file():
                    rec["articles"].append({"slug": slugify(a["slug"]), "title": str(a.get("title") or ""),
                                            "description": str(a.get("description") or ""), "draft": bool(a.get("draft")),
                                            "updated": clean_date(a.get("updated"))})
                    arts += 1
            self.cfg["wiki"]["sections"].append(rec)
            secs += 1
        self.normalize_wiki()
        extra = self.wiki_rescan()
        report["notes"].insert(0, "Imported %d wiki section(s) and %d article(s) from WikiGen%s." % (
            secs, arts + extra.get("wiki_articles_added", 0),
            "" if not extra.get("wiki_articles_added") else ", including %d file(s) found on disk" % extra["wiki_articles_added"]))

    def _import_storygen(self, src, scfg, report, settings):
        meta = src / ".storygen"
        self.enable_module("library")
        s = scfg.get("site") if isinstance(scfg.get("site"), dict) else {}
        if settings:
            self._copy_site_settings(s, ("title", "tagline", "author", "footer", "lang", "url", "description", "noindex", "sitemap"))
            if s.get("rss"):
                self.cfg["site"]["feeds"] = True
        for k, dest in (("prose", "prose"), ("dropcap", "dropcap"), ("wordcount", "wordcount"),
                        ("reading_time", "reading_time"), ("wpm", "wpm"), ("updates", "lib_updates")):
            if k in s:
                self.cfg["site"][dest] = s[k]
        text = src_text(meta / "home.md")
        if text:
            write_text(self.lib_home_md(), text)
        pages = 0
        for p in scfg.get("pages") if isinstance(scfg.get("pages"), list) else []:
            if isinstance(p, dict) and slugify(str(p.get("slug", ""))):
                new = self._new_page_slug(slugify(p["slug"]))
                self.cfg["pages"].append({"slug": new, "title": str(p.get("title") or title_from_name(new)), "show_title": True,
                                          "description": str(p.get("description") or "")})
                write_text(self.page_md(new), src_text(meta / "pages" / (p["slug"] + ".md")))
                pages += 1
        stories = chapters = 0
        for st in scfg.get("stories") if isinstance(scfg.get("stories"), list) else []:
            if not isinstance(st, dict) or not slugify(str(st.get("slug", ""))):
                continue
            old = slugify(st["slug"])
            new = self.unique_story_slug(old)
            if new != old:
                report["notes"].append("Story %s was renamed to %s because the name was taken." % (old, new))
            self._copy_tree_md(meta / "stories" / st["slug"], self.story_dir(new))
            rec = {k: st.get(k) for k in ("title", "subtitle", "author", "status", "kind", "blurb", "cover", "genres", "warnings",
                                          "series", "series_no", "started", "completed", "draft", "description")}
            rec["slug"] = new
            rec["chapters"] = []
            for ch in st.get("chapters") if isinstance(st.get("chapters"), list) else []:
                if isinstance(ch, dict) and slugify(str(ch.get("slug", ""))) and self.chapter_md(new, slugify(ch["slug"])).is_file():
                    rec["chapters"].append({"slug": slugify(ch["slug"]), "title": str(ch.get("title") or ""),
                                            "date": clean_date(ch.get("date")), "draft": bool(ch.get("draft")),
                                            "note": str(ch.get("note") or "")})
                    chapters += 1
            self.cfg["library"]["stories"].append(rec)
            stories += 1
        self.normalize_library()
        self.lib_rescan()
        report["notes"].insert(0, "Imported %d stor%s, %d chapter(s) and %d page(s) from StoryGen." % (
            stories, "y" if stories == 1 else "ies", chapters, pages))


class Site(ImportMixin, BlogMixin, WikiMixin, LibraryMixin, SiteBase):
    pass


# ----------------------------------------------------------------------------
# Publishing to a server
#
# Transfers use tools already on your machine (rsync, ssh, sftp, git) or Python's own
# ftplib. Nothing here implements SSH, so password logins for rsync and sftp need
# sshpass; key logins need nothing extra and are the better option.
#
# Security rules this code keeps:
#   * Settings are validated before any program runs. They live in config.json, which can
#     arrive with a site folder someone else made, so they are treated as untrusted.
#   * Secrets never appear on a command line or in the log. sshpass and git read them
#     from the environment, which only this user can read.
#   * Only regular files are published. Symlinks are skipped, so a link inside the site
#     folder cannot leak the file it points at.
#   * Files are copied to a private staging folder first, so saving in the editor during
#     a publish cannot change what is being sent halfway through.
#   * Deletions on the server only touch plain relative paths inside the target, and only
#     files an earlier publish sent there (rsync mirror mode excepted, which asks first).
# ----------------------------------------------------------------------------

DEPLOY_METHODS = ("rsync", "sftp", "ftp", "ftps", "git", "folder")
PASSWORD_ENV = APP.upper() + "_PASSWORD"
PUBLISH_DOTFILES = (".htaccess", ".well-known")
JOB = {"running": False, "ok": None, "what": "", "lines": [], "started": "", "proc": None, "cancel": False}
JOB_LOCK = threading.Lock()

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*\.?|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+)$")
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_FTP_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@+-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,99}$")
_SHELL_META = set("$`;&|<>()*?[]{}'\"\\!#")
_DANGEROUS_ROOTS = {"", "/", "~", ".", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc",
                    "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/www", "/var/lib"}


def default_deploy():
    return {
        "method": "rsync",
        "host": "",
        "port": "",
        "user": "",
        "path": "",
        "key": "",
        "remote": "",
        "branch": "main",
        "mirror": False,
        "include_sources": False,
        "save_password": False,
        "passive": True,
        "insecure_tls": False,
    }


def clean_deploy(d, current):
    """Merge submitted settings over the stored ones. Shape only; validate_deploy checks meaning."""
    out = dict(default_deploy())
    if isinstance(current, dict):
        out.update({k: v for k, v in current.items() if k in out})
    if d.get("method") in DEPLOY_METHODS:
        out["method"] = d["method"]
    if out.get("method") not in DEPLOY_METHODS:
        out["method"] = "rsync"
    for k, limit in (("host", 253), ("user", 128), ("path", 400), ("key", 400), ("remote", 400), ("branch", 100)):
        if k in d:
            out[k] = str(d.get(k) or "").strip()[:limit]
        out[k] = str(out.get(k) or "")
    if "port" in d:
        out["port"] = str(d.get("port") or "").strip()[:5]
    out["port"] = str(out.get("port") or "")
    for k in ("mirror", "include_sources", "save_password", "passive", "insecure_tls"):
        if k in d:
            out[k] = bool(d[k])
        out[k] = bool(out.get(k))
    return out


def validate_deploy(dep, require=True):
    """Refuse anything that could be read as a command line option, a shell fragment, or a
    path outside the target. Raises ApiError with a message the user can act on.
    require=False allows blank fields, for saving settings that are not finished yet."""
    m = dep["method"]
    for k in ("host", "user", "path", "key", "remote", "branch", "port"):
        if _CTRL.search(dep.get(k) or ""):
            raise ApiError("The %s contains a control character, which is not allowed." % k)
    if dep.get("port"):
        if not dep["port"].isdigit() or not 1 <= int(dep["port"]) <= 65535:
            raise ApiError("The port must be a number from 1 to 65535, or blank for the default.")
    if m in ("rsync", "sftp", "ftp", "ftps"):
        if not dep.get("host") and require:
            raise ApiError("Fill in the Host.")
        if dep.get("host") and not _HOST_RE.match(dep["host"]):
            raise ApiError("The host must be a plain hostname or IP address, such as vps.example.com.")
        user = dep.get("user") or ""
        if m in ("ftp", "ftps"):
            if not user and require:
                raise ApiError("FTP needs a User.")
            if user and not _FTP_USER_RE.match(user):
                raise ApiError("The user name contains characters that are not allowed.")
        elif user and not _USER_RE.match(user):
            raise ApiError("The user name may only contain letters, digits, dot, dash and underscore.")
    if m in ("rsync", "sftp", "ftp", "ftps", "folder"):
        path = dep.get("path") or ""
        if not path and require:
            raise ApiError("Fill in the Path.")
        if path.startswith("-"):
            raise ApiError("The path cannot start with a dash.")
        if ".." in path.replace("\\", "/").split("/"):
            raise ApiError("The path cannot contain '..'. Use the full path instead.")
        if m != "folder" and (_SHELL_META & set(path) or " " in path):
            raise ApiError("The server path may only contain letters, digits, and . _ - / ~ @ + characters.")
    if m in ("rsync", "sftp", "git") and dep.get("key"):
        if dep["key"].startswith("-") or not Path(dep["key"]).expanduser().is_file():
            raise ApiError("The SSH key file %s does not exist." % dep["key"])
    if m == "git":
        remote = dep.get("remote") or ""
        if not remote and require:
            raise ApiError("Fill in the Git remote URL.")
        if remote.startswith("-"):
            raise ApiError("The git remote cannot start with a dash.")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", remote):
            raise ApiError("Git remote helpers of the form transport::address are not allowed here.")
        bits = urlsplit(remote)
        if bits.scheme and len(bits.scheme) > 1:
            if bits.scheme not in ("https", "http", "ssh", "git", "file"):
                raise ApiError("Use an https://, ssh:// or git@host:path remote.")
            if bits.password:
                raise ApiError("Do not put a password or token in the URL. Use the Password box instead.")
            if bits.hostname and bits.hostname.startswith("-"):
                raise ApiError("The host in the git remote cannot start with a dash.")
        branch = dep.get("branch") or "main"
        if not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
            raise ApiError("The branch name is not valid.")
    return dep


def mirror_path_is_dangerous(path):
    p = path.strip().rstrip("/") or "/"
    if p in _DANGEROUS_ROOTS:
        return True
    return bool(re.match(r"^(/home/[^/]+|/Users/[^/]+|~[^/]*)$", p))


# ---- the job log

def job_say(line):
    with JOB_LOCK:
        JOB["lines"].append(str(line).rstrip("\r\n"))
        del JOB["lines"][:-800]


def job_status(since=0):
    try:
        since = max(0, int(since))
    except (TypeError, ValueError):
        since = 0
    with JOB_LOCK:
        return {"running": JOB["running"], "ok": JOB["ok"], "what": JOB["what"],
                "total": len(JOB["lines"]), "lines": JOB["lines"][since:]}


def job_cancelled():
    with JOB_LOCK:
        return JOB["cancel"]


def cancel_publish():
    with JOB_LOCK:
        if not JOB["running"]:
            return {"ok": False}
        JOB["cancel"] = True
        proc = JOB["proc"]
    job_say("Stopping at your request...")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return {"ok": True}


def run_stream(cmd, env=None, cwd=None):
    """Run a command with no terminal attached, sending its output to the job log."""
    job_say("$ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=env, cwd=cwd, text=True, bufsize=1, errors="replace")
    except FileNotFoundError:
        raise ApiError("%s is not installed on this machine." % cmd[0])
    with JOB_LOCK:
        JOB["proc"] = p
    try:
        for line in p.stdout:
            job_say(line)
        return p.wait()
    finally:
        with JOB_LOCK:
            JOB["proc"] = None
        if job_cancelled():
            raise ApiError("Stopped before it finished. The server may hold a partial upload; publish again to complete it.")


# ---- ssh helpers

def ssh_opts(dep, password):
    opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    opts += ["-o", "BatchMode=no"] if password else ["-o", "BatchMode=yes"]
    if dep.get("key"):
        opts += ["-i", str(Path(dep["key"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    if dep.get("port"):
        opts += ["-o", "Port=" + dep["port"]]
    return opts


def with_sshpass(cmd, password):
    """Put sshpass in front of a command. It reads the password from SSHPASS in the
    environment, which other users cannot read, instead of from the command line."""
    env = dict(os.environ)
    env.pop("SSHPASS", None)
    if not password:
        return cmd, env
    if not shutil.which("sshpass"):
        raise ApiError(
            "Password logins for this method need the sshpass program, which is not installed. "
            "Install it (openSUSE: sudo zypper install sshpass), or leave the password blank and use "
            "an SSH key, which is the safer option anyway."
        )
    env["SSHPASS"] = password
    return ["sshpass", "-e"] + cmd, env


def remote_target(dep):
    user = dep.get("user") or ""
    return ("%s@%s" % (user, dep["host"])) if user else dep["host"]


def server_path(dep):
    """Path on the server. For sftp and ftp a leading ~/ means the login folder, which is
    where relative paths start anyway."""
    p = dep["path"].strip()
    if dep["method"] in ("sftp", "ftp", "ftps"):
        if p in ("~", "~/"):
            return "."
        if p.startswith("~/"):
            return p[2:].rstrip("/") or "."
    return p.rstrip("/") or "/"


# ---- what gets published

def local_files(root):
    """Regular files to publish, relative to the site folder. Hidden files and folders are
    skipped (that covers the markdown sources and .git), except .htaccess and .well-known.
    Symlinks are skipped and reported, so a link cannot publish the file it points at."""
    out, skipped = [], []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            full = here / d
            if full.is_symlink():
                skipped.append(full.relative_to(root).as_posix() + "/")
            elif not d.startswith(".") or d in PUBLISH_DOTFILES:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = here / f
            rel = full.relative_to(root).as_posix()
            if f.startswith(".") and f not in PUBLISH_DOTFILES:
                continue
            if full.is_symlink() or not full.is_file():
                skipped.append(rel)
                continue
            out.append(rel)
    return out, skipped


def safe_rel(rel):
    """True for a plain relative path with no way to climb out of the folder it is joined to."""
    if not isinstance(rel, str) or not rel or _CTRL.search(rel) or "\\" in rel:
        return False
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def stage(site, files):
    """Copy the files into a private temporary folder and publish from there."""
    tmp = Path(tempfile.mkdtemp(prefix="%s-publish-" % APP.lower()))
    for rel in files:
        dst = tmp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(site.root / rel, dst, follow_symlinks=False)
    return tmp


def target_key(dep):
    return "|".join([dep["method"], dep.get("host", ""), dep.get("port", ""), dep.get("user", ""),
                     dep.get("path", ""), dep.get("remote", "")])


def load_published(site, dep):
    """Files the last publish to this exact target sent, or None if there was none. Entries
    that are not plain relative paths are dropped, since the record lives in an editable file."""
    try:
        d = json.loads((site.meta / "published.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("target") != target_key(dep):
        return None
    return [f for f in (d.get("files") or []) if safe_rel(f)]


def save_published(site, dep, files):
    write_text(site.meta / "published.json",
               json.dumps({"target": target_key(dep), "files": sorted(files),
                           "when": datetime.date.today().isoformat()}, indent=1))


# ---- saved password

def secret_path(site):
    return site.meta / "secret.json"


def secret_key(dep):
    """A saved password is tied to the method, host, port and user it was saved for, so
    pointing config.json somewhere else does not send it to a different server."""
    return "|".join([dep["method"], dep.get("host", ""), dep.get("port", ""), dep.get("user", ""),
                     dep.get("remote", "")])


def load_password(site, dep):
    try:
        d = json.loads(secret_path(site).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict) or d.get("for") != secret_key(dep):
        return ""
    return str(d.get("password") or "")


def store_password(site, dep, password, keep):
    """Save or forget the password. A blank password never overwrites a saved one. The file
    is created with owner-only permissions, so it is never readable by others, even briefly."""
    p = secret_path(site)
    if not keep:
        if p.exists():
            p.unlink()
        return
    if not password:
        return
    data = json.dumps({"for": secret_key(dep), "password": password}).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, p)


def resolve_password(site, dep, given):
    """Password from the form, then the one saved for this target, then the environment."""
    if given:
        return given
    return load_password(site, dep) or os.environ.get(PASSWORD_ENV, "")


# ---- the methods

def deploy_rsync(dep, password, staged, dry):
    if not shutil.which("rsync"):
        raise ApiError("rsync is not installed on this machine. Install it, or choose another method.")
    ssh = ["ssh"] + ssh_opts(dep, password)
    # -r -t -z without -l or -p: no symlinks, no local permission bits; --chmod gives sane web
    # permissions. The protect filter keeps server-side dotfiles (.htaccess, .well-known) safe
    # from --delete.
    cmd = ["rsync", "-rtz", "--human-readable", "--itemize-changes", "--timeout=120",
           "--chmod=D755,F644", "--filter=P .*"]
    if dry:
        cmd.append("--dry-run")
    if dep.get("mirror"):
        cmd.append("--delete")
    cmd += ["-e", " ".join(shlex.quote(x) for x in ssh), "--",
            str(staged) + "/", "%s:%s/" % (remote_target(dep), server_path(dep).rstrip("/"))]
    cmd, env = with_sshpass(cmd, password)
    code = run_stream(cmd, env=env)
    if code != 0:
        raise ApiError("rsync exited with code %d. The log above says why." % code)


def sftp_quote(s):
    """Quote an argument for an sftp batch file. Control characters are refused, so a name
    can never start a new batch command."""
    if _CTRL.search(s):
        raise ApiError("A file name contains a control character: %r" % s)
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def deploy_sftp(site, dep, password, files, staged, dry):
    if not shutil.which("sftp"):
        raise ApiError("The sftp program (part of OpenSSH) is not installed. Install it, or choose another method.")
    odd = [f for f in files if set(f) & set("*?[]\\\"")]
    if odd:
        raise ApiError("sftp treats * ? [ ] as wildcards, so these names cannot be uploaded safely with it: %s. "
                       "Rename them or use rsync." % ", ".join(odd[:5]))
    base = server_path(dep)
    previous = load_published(site, dep) or []
    stale = [f for f in previous if f not in files] if dep.get("mirror") else []
    lines, made = [], set()
    prefix = "/" if base.startswith("/") else ""
    walked = []
    for part in [p for p in base.split("/") if p and p != "."]:
        walked.append(part)
        lines.append("-mkdir " + sftp_quote(prefix + "/".join(walked)))
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(len(parts)):
            sub = "/".join(parts[: i + 1])
            if sub not in made:
                made.add(sub)
                lines.append("-mkdir " + sftp_quote(base + "/" + sub))
    for f in files:
        lines.append("put %s %s" % (sftp_quote(str(staged / f)), sftp_quote(base + "/" + f)))
    for f in stale:
        lines.append("-rm " + sftp_quote(base + "/" + f))
    if dry:
        job_say("Dry run. %d file(s) would be uploaded and %d removed." % (len(files), len(stale)))
        for line in lines[:80]:
            job_say("  " + line)
        if len(lines) > 80:
            job_say("  ... and %d more" % (len(lines) - 80))
        return
    fd, batch = tempfile.mkstemp(prefix="%s-sftp-" % APP.lower(), suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        cmd = ["sftp"] + ssh_opts(dep, password) + ["-b", batch, "--", remote_target(dep)]
        cmd, env = with_sshpass(cmd, password)
        code = run_stream(cmd, env=env)
    finally:
        os.unlink(batch)
    if code != 0:
        raise ApiError("sftp exited with code %d. The log above says why." % code)


def ftp_close(ftp):
    try:
        ftp.quit()
    except (OSError, EOFError, ftplib.Error):
        ftp.close()


def ftp_connect(dep, password):
    """Log in over FTP, or FTPS with the certificate checked unless that was switched off.
    FTP_TLS.login secures the control channel before the password is sent."""
    tls = dep["method"] == "ftps"
    if tls:
        ctx = ssl.create_default_context()
        if dep.get("insecure_tls"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            job_say("The server certificate is not being checked, because you asked for that.")
        ftp = ftplib.FTP_TLS(context=ctx, timeout=30)
    else:
        job_say("WARNING: plain FTP sends your password and files unencrypted. Use FTPS or SFTP if you can.")
        ftp = ftplib.FTP(timeout=30)
    port = int(dep.get("port") or 21)
    job_say("Connecting to %s:%d" % (dep["host"], port))
    try:
        ftp.connect(dep["host"], port)
        ftp.login(dep.get("user") or "", password)
        if tls:
            ftp.prot_p()
    except ssl.SSLError as e:
        ftp.close()
        raise ApiError("The server's TLS certificate was refused (%s). Fix the certificate, tick "
                       "\"trust a self-signed certificate\", or use SFTP." % e)
    except ftplib.error_perm as e:
        ftp.close()
        raise ApiError("The server refused the login: %s" % e)
    except (OSError, EOFError) as e:
        ftp.close()
        raise ApiError("Could not connect: %s" % e)
    ftp.set_pasv(bool(dep.get("passive", True)))
    job_say("Logged in as %s" % dep.get("user"))
    return ftp


def ftp_enter(ftp, path, create):
    """Change into path one folder at a time, creating folders when asked. Returns the full path."""
    if path.startswith("/"):
        ftp.cwd("/")
    for part in [p for p in path.split("/") if p and p != "."]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            if not create:
                raise
            ftp.mkd(part)
            ftp.cwd(part)
    return ftp.pwd()


def deploy_ftp(site, dep, password, files, staged, dry):
    if not password:
        raise ApiError("FTP needs a password.")
    previous = load_published(site, dep) or []
    stale = [f for f in previous if f not in files] if dep.get("mirror") else []
    if dry:
        job_say("Dry run. %d file(s) would be uploaded and %d removed." % (len(files), len(stale)))
        return
    ftp = ftp_connect(dep, password)
    try:
        home = ftp_enter(ftp, server_path(dep), create=True).rstrip("/")
        made = set()
        for i, f in enumerate(files, 1):
            if job_cancelled():
                raise ApiError("Stopped before it finished. Publish again to complete the upload.")
            parts = f.split("/")[:-1]
            for j in range(len(parts)):
                sub = "/".join(parts[: j + 1])
                if sub not in made:
                    made.add(sub)
                    try:
                        ftp.mkd(home + "/" + sub)
                    except ftplib.error_perm:
                        pass
            with open(staged / f, "rb") as fh:
                ftp.storbinary("STOR " + home + "/" + f, fh)
            if i % 10 == 0 or i == len(files):
                job_say("Uploaded %d/%d" % (i, len(files)))
        for f in stale:
            try:
                ftp.delete(home + "/" + f)
                job_say("Removed " + f)
            except ftplib.error_perm as e:
                job_say("Could not remove %s (%s)" % (f, e))
    finally:
        ftp_close(ftp)


def git_env(dep, password):
    """Environment for git: no prompts, ordinary transports only, and credentials supplied by
    a helper that reads them from the environment instead of the URL or the command line."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:http:ssh:git:file"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    if dep.get("key"):
        ssh += ["-i", str(Path(dep["key"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(x) for x in ssh)
    env["PUBLISH_GIT_USER"] = dep.get("user") or "git"
    env["PUBLISH_GIT_PASS"] = password or ""
    return env


def git_cmd(*args):
    """git with this app's settings. The first empty credential.helper switches off any helper
    in your git config, so the token is neither read from nor saved into a credential store."""
    helper = ('!f() { test "$1" = get || return 0; echo "username=$PUBLISH_GIT_USER"; '
              'echo "password=$PUBLISH_GIT_PASS"; }; f')
    return ["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper,
            "-c", "protocol.ext.allow=never", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"] + list(args)


def deploy_git(site, dep, password, dry):
    if not shutil.which("git"):
        raise ApiError("git is not installed on this machine.")
    branch = dep.get("branch") or "main"
    root = str(site.root)
    env = git_env(dep, password)
    who = (site.cfg.get("site") or {}).get("author") or APP
    for k, v in (("GIT_AUTHOR_NAME", who), ("GIT_COMMITTER_NAME", who),
                 ("GIT_AUTHOR_EMAIL", APP.lower() + "@localhost"), ("GIT_COMMITTER_EMAIL", APP.lower() + "@localhost")):
        env.setdefault(k, v)
    if not (site.root / ".git").exists():
        if run_stream(git_cmd("init", "-q", "-b", branch), cwd=root, env=env) != 0:
            raise ApiError("git init failed.")
    ignore = site.root / ".gitignore"
    current = read_text(ignore)
    if dep.get("include_sources"):
        wanted = [META_DIR + "/secret.json", META_DIR + "/secret.json.tmp", META_DIR + "/published.json"]
    else:
        wanted = [META_DIR + "/"]
    add = [w for w in wanted if w not in current.split("\n")]
    if add:
        write_text(ignore, (current.rstrip("\n") + "\n" if current.strip() else "") + "\n".join(add) + "\n")
    run_stream(git_cmd("rm", "-r", "-q", "--cached", "--ignore-unmatch", META_DIR + "/secret.json"), cwd=root, env=env)
    if run_stream(git_cmd("add", "-A"), cwd=root, env=env) != 0:
        raise ApiError("git add failed.")
    if dry:
        run_stream(git_cmd("status", "--short"), cwd=root, env=env)
        job_say("Dry run. Nothing was committed or pushed.")
        return
    if run_stream(git_cmd("diff", "--cached", "--quiet"), cwd=root, env=env) != 0:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        if run_stream(git_cmd("commit", "-q", "-m", "Publish " + stamp), cwd=root, env=env) != 0:
            raise ApiError("git could not commit. The log above says why.")
    else:
        job_say("No changes to commit. Pushing the current commit anyway.")
    code = run_stream(git_cmd("push", "--", dep["remote"], "HEAD:refs/heads/" + branch), cwd=root, env=env)
    if code != 0:
        raise ApiError("git push exited with code %d. The log above says why." % code)


def folder_dest(site, dep):
    dest = Path(dep["path"]).expanduser().resolve()
    root = site.root.resolve()
    if dest == root or root in dest.parents or dest in root.parents:
        raise ApiError("The destination cannot be the site folder, inside it, or a folder that contains it.")
    return dest


def deploy_folder(site, dep, files, staged, dry):
    dest = folder_dest(site, dep)
    previous = load_published(site, dep) or []
    stale = [f for f in previous if f not in files] if dep.get("mirror") else []
    if dry:
        job_say("Dry run. %d file(s) would be copied to %s and %d removed." % (len(files), dest, len(stale)))
        return
    dest.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files, 1):
        if job_cancelled():
            raise ApiError("Stopped before it finished.")
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        shutil.copy2(staged / f, target, follow_symlinks=False)
        if i % 25 == 0 or i == len(files):
            job_say("Copied %d/%d" % (i, len(files)))
    for f in stale:
        p = dest / f
        try:
            inside = dest in p.resolve().parents
        except OSError:
            inside = False
        if inside and (p.is_file() or p.is_symlink()):
            p.unlink()
            job_say("Removed " + f)


def run_deploy(site, dep, password, dry):
    method = dep["method"]
    if method == "git":
        deploy_git(site, dep, password, dry)
        return
    files, skipped = local_files(site.root)
    for s in skipped:
        job_say("Skipped %s (a symlink or special file, which is never published)" % s)
    job_say("%d file(s) to publish from %s" % (len(files), site.root))
    staged = stage(site, files)
    try:
        if method == "rsync":
            deploy_rsync(dep, password, staged, dry)
        elif method == "sftp":
            deploy_sftp(site, dep, password, files, staged, dry)
        elif method in ("ftp", "ftps"):
            deploy_ftp(site, dep, password, files, staged, dry)
        elif method == "folder":
            deploy_folder(site, dep, files, staged, dry)
        else:
            raise ApiError("Unknown publish method.")
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    if not dry:
        save_published(site, dep, files)


def test_deploy(site, dep, password):
    """A reachability check that changes nothing on the server."""
    method = dep["method"]
    if method in ("rsync", "sftp"):
        if not shutil.which("ssh"):
            raise ApiError("The ssh program is not installed, so the connection cannot be tested here.")
        path = server_path(dep)
        cmd = ["ssh"] + ssh_opts(dep, password) + [
            "--", remote_target(dep), "test -d %s && echo FOUND || echo MISSING" % shlex.quote(path)]
        cmd, env = with_sshpass(cmd, password)
        code = run_stream(cmd, env=env)
        if code != 0:
            raise ApiError("Could not log in (ssh exited %d). The log above says why." % code)
        job_say("Logged in. MISSING above means the path does not exist yet; the first publish creates it.")
    elif method in ("ftp", "ftps"):
        if not password:
            raise ApiError("FTP needs a password.")
        ftp = ftp_connect(dep, password)
        try:
            ftp_enter(ftp, server_path(dep), create=False)
            job_say("Path %s exists." % dep["path"])
        except ftplib.error_perm:
            job_say("Path %s does not exist yet. The first publish creates it." % dep["path"])
        finally:
            ftp_close(ftp)
    elif method == "git":
        if run_stream(git_cmd("ls-remote", "--heads", "--", dep["remote"]), env=git_env(dep, password)) != 0:
            raise ApiError("git could not reach that remote. The log above says why.")
        job_say("Remote reachable.")
    elif method == "folder":
        dest = Path(dep["path"]).expanduser()
        job_say("%s: %s" % (dest, "exists" if dest.is_dir() else "does not exist yet, it will be created"))
        parent = dest if dest.is_dir() else dest.parent
        if not os.access(str(parent), os.W_OK):
            raise ApiError("No permission to write to %s" % parent)
        job_say("Writable.")
    else:
        raise ApiError("Unknown publish method.")


def start_publish(site, d, dry=False, test=False):
    """Validate, build, then transfer on a background thread so the editor stays responsive."""
    d = d if isinstance(d, dict) else {}
    with JOB_LOCK:
        if JOB["running"]:
            raise ApiError("A publish is already running.")
    dep = validate_deploy(clean_deploy(d, site.cfg.get("deploy")))
    if dep["method"] == "folder":
        folder_dest(site, dep)
    if dep["method"] == "rsync" and dep.get("mirror") and not dry and not test:
        if mirror_path_is_dangerous(server_path(dep)):
            raise ApiError("Refusing to mirror into %s: removing files there could wipe far more than the "
                           "site. Point Path at the site's own folder." % dep["path"])
        if load_published(site, dep) is None and not d.get("confirm_mirror"):
            return {"needs_confirm": True, "message": (
                "This is the first publish to %s:%s with \"remove files\" turned on.\n\n"
                "Anything already in that folder on the server that is not part of this site will be "
                "deleted. Hidden files such as .htaccess and .well-known are kept.\n\n"
                "Run a dry run first if you are not sure. Continue?" % (dep["host"], dep["path"]))}
    site.cfg["deploy"] = dep
    site.save_cfg()
    given = str(d.get("password") or "")
    password = resolve_password(site, dep, given)
    if "save_password" in d:
        store_password(site, dep, given, dep.get("save_password"))
    if not test:
        site.build()
    what = "Testing the connection" if test else ("Dry run" if dry else "Publishing")
    with JOB_LOCK:
        JOB.update(running=True, ok=None, what=what, lines=[], proc=None, cancel=False,
                   started=datetime.datetime.now().isoformat(" ", "seconds"))
    job_say("%s (%s) at %s" % (what, dep["method"], JOB["started"]))

    def work():
        ok = False
        try:
            if test:
                test_deploy(site, dep, password)
            else:
                run_deploy(site, dep, password, dry)
            job_say("Done.")
            ok = True
        except ApiError as e:
            job_say("Stopped: %s" % e)
        except Exception as e:  # noqa: BLE001  (report anything unexpected instead of dying silently)
            job_say("Stopped: %s: %s" % (type(e).__name__, e))
            traceback.print_exc()
        with JOB_LOCK:
            JOB.update(running=False, ok=ok, proc=None)

    threading.Thread(target=work, daemon=True, name="publish").start()
    return {"started": True}


def deploy_info(site):
    dep = clean_deploy({}, site.cfg.get("deploy"))
    with JOB_LOCK:
        running = JOB["running"]
    return {
        "deploy": dep,
        "has_password": bool(load_password(site, dep)),
        "env_var": PASSWORD_ENV,
        "secret_path": str(secret_path(site)),
        "running": running,
        "tools": {t: bool(shutil.which(t)) for t in ("rsync", "ssh", "sftp", "sshpass", "git")},
    }


def save_deploy(site, d):
    d = d if isinstance(d, dict) else {}
    dep = validate_deploy(clean_deploy(d, site.cfg.get("deploy")), require=False)
    site.cfg["deploy"] = dep
    site.save_cfg()
    if "save_password" in d:
        store_password(site, dep, str(d.get("password") or ""), dep.get("save_password"))
    return {"ok": True, "has_password": bool(load_password(site, dep))}


# ----------------------------------------------------------------------------
# Opening sites, remembering the last one
# ----------------------------------------------------------------------------

PENDING = {"folder": None}


def remember_folder(path):
    try:
        write_text(CONFIG_HOME / "last.json", json.dumps({"folder": str(path)}))
    except OSError:
        pass


def last_folder():
    try:
        return json.loads((CONFIG_HOME / "last.json").read_text(encoding="utf-8")).get("folder")
    except (OSError, ValueError, AttributeError):
        return None


def open_site(path, confirm=False, setup=None):
    """Open a site. A folder without one asks for confirmation if it holds other files, then for
    the first-run setup (homepage type, modules, optional import) before anything is written."""
    path = str(path or "").strip()
    if not path:
        raise ApiError("Enter a folder path.")
    root = Path(path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ApiError("That path is a file, not a folder.")
    if (root / META_DIR / "config.json").exists():
        site = Site(root)
        site.load()
    else:
        if root.exists() and not confirm and any(not n.name.startswith(".") for n in root.iterdir()):
            return {"needs_confirm": True, "message": (
                "This folder already has files that SiteForge did not create.\n\nSiteForge will add its own files here "
                "and overwrite index.html and any page it generates with the same name. Other files are left alone.\n\n"
                "Use this folder anyway?")}
        if not isinstance(setup, dict):
            PENDING["folder"] = str(root)
            return {"needs_setup": True, "folder": str(root)}
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ApiError("Could not create that folder: %s" % e)
        site = Site(root)
        site.create(setup)
        if str(setup.get("import") or "").strip():
            site.import_site({"path": setup["import"], "settings": True, "home": setup.get("home"),
                              "root_mount": setup.get("root_mount")})
    CURRENT["site"] = site
    PENDING["folder"] = None
    remember_folder(root)
    return {"state": site.state()}


def app_state():
    site = CURRENT["site"]
    if site is None:
        return {"folder": None, "pending": PENDING["folder"], "suggest": str(Path.home() / "my-site"),
                "last": last_folder(), "themes": [{"id": k, "label": v[0], "group": v[1]} for k, v in THEMES.items()]}
    return site.state()


def pick_folder():
    site = CURRENT["site"]
    start = str(site.root.parent) if site else str(Path.home())
    cmds = []
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", start])
    if shutil.which("zenity"):
        cmds.append(["zenity", "--file-selection", "--directory", "--filename=" + start.rstrip("/") + "/"])
    if sys.platform == "darwin":
        cmds.append(["osascript", "-e", "POSIX path of (choose folder)"])
    cmds.append([sys.executable, "-c",
                 "import tkinter as t, tkinter.filedialog as f; r=t.Tk(); r.withdraw(); print(f.askdirectory())"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return {"path": r.stdout.strip() or None, "available": True}
        if r.returncode == 1 and cmd[0] != sys.executable:
            return {"path": None, "available": True}
    return {"path": None, "available": False}


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

def need(site, mid):
    if not site.enabled(mid):
        raise ApiError("The %s is switched off. Turn it on in Site structure first." % site.module_label(mid), 409)


def api(method, route, query, h):
    q = parse_qs(query)
    parts = [unquote(x) for x in route.strip("/").split("/")]
    n = len(parts)

    if parts == ["state"] and method == "GET":
        return app_state()
    if parts == ["open"] and method == "POST":
        d = h.read_json()
        return open_site(d.get("path", ""), bool(d.get("confirm")), d.get("setup"))
    if parts == ["detect-import"] and method == "POST":
        kind, src, cfg = detect_import(h.read_json().get("path"))
        title = (cfg.get("site") or {}).get("title") if isinstance(cfg.get("site"), dict) else ""
        return {"kind": kind, "title": title or src.name, "home": {"sitegen": "page", "wikigen": "wiki", "storygen": "library"}[kind]}

    w = CURRENT["site"]
    if w is None:
        raise ApiError("Open a site folder first.", 409)

    # site structure
    if parts == ["structure"] and method == "PUT":
        return w.save_structure(h.read_json())
    if n == 3 and parts[0] == "modules" and parts[2] == "remove" and method == "POST":
        return w.remove_module_content(parts[1])
    if parts == ["import"] and method == "POST":
        return w.import_site(h.read_json())

    # pages and menu
    if parts == ["pages"] and method == "POST":
        return w.add_page(h.read_json())
    if n == 2 and parts[0] == "pages":
        if method == "GET":
            return w.get_page(parts[1])
        if method == "PUT":
            return w.save_page(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_page(parts[1])
    if parts == ["nav"] and method == "PUT":
        return w.save_nav(h.read_json().get("nav"))

    # blog
    if parts[0] in ("posts", "blog-intro"):
        need(w, "blog")
    if parts == ["blog-intro"]:
        if method == "GET":
            return w.get_blog_intro()
        if method == "PUT":
            return w.save_blog_intro(h.read_json())
    if parts == ["posts"] and method == "POST":
        return w.save_post(h.read_json())
    if n == 2 and parts[0] == "posts":
        if method == "GET":
            return w.get_post(parts[1])
        if method == "DELETE":
            return w.delete_post(parts[1])
    if n == 3 and parts[0] == "posts" and parts[2] == "unpublish" and method == "POST":
        return w.unpublish_post(parts[1])

    # wiki
    if parts[0] in ("wiki-home", "sections", "articles"):
        need(w, "wiki")
    if parts == ["wiki-home"]:
        if method == "GET":
            return w.get_wiki_home()
        if method == "PUT":
            return w.save_wiki_home(h.read_json())
    if parts == ["sections"] and method == "POST":
        return w.add_section(h.read_json().get("title", ""))
    if n == 2 and parts[0] == "sections":
        if method == "GET":
            return w.get_section(parts[1])
        if method == "PUT":
            return w.save_section(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_section(parts[1])
    if n == 3 and parts[0] == "sections" and method == "POST":
        if parts[2] == "move":
            return w.move_section(parts[1], h.read_json().get("dir", 1))
        if parts[2] == "order":
            return w.order_articles(parts[1], h.read_json().get("articles", []))
    if parts == ["articles"] and method == "POST":
        return w.save_article(h.read_json())
    if n == 3 and parts[0] == "articles":
        if method == "GET":
            return w.get_article(parts[1], parts[2])
        if method == "DELETE":
            return w.delete_article(parts[1], parts[2])

    # library
    if parts[0] in ("lib-home", "stories", "chapters"):
        need(w, "library")
    if parts == ["lib-home"]:
        if method == "GET":
            return w.get_lib_home()
        if method == "PUT":
            return w.save_lib_home(h.read_json())
    if parts == ["stories"] and method == "POST":
        return w.add_story(h.read_json())
    if n == 2 and parts[0] == "stories":
        if method == "GET":
            return w.get_story(parts[1])
        if method == "PUT":
            return w.save_story(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_story(parts[1])
    if n == 3 and parts[0] == "stories" and method == "POST":
        if parts[2] == "move":
            return w.move_story(parts[1], h.read_json().get("dir", 1))
        if parts[2] == "order":
            return w.order_chapters(parts[1], h.read_json().get("chapters", []))
    if parts == ["chapters"] and method == "POST":
        return w.save_chapter(h.read_json())
    if n == 3 and parts[0] == "chapters":
        if method == "GET":
            return w.get_chapter(parts[1], parts[2])
        if method == "DELETE":
            return w.delete_chapter(parts[1], parts[2])

    # everything else
    if parts == ["css"]:
        if method == "GET":
            return w.get_css()
        if method == "PUT":
            return w.save_css(h.read_json().get("css", ""))
    if parts == ["settings"] and method == "PUT":
        return w.save_settings(h.read_json())
    if parts == ["preview"] and method == "POST":
        return w.preview(h.read_json())
    if parts == ["preview-targets"] and method == "GET":
        return w.preview_targets()
    if parts == ["upload"] and method == "POST":
        return w.upload(q.get("name", ["image"])[0], h.read_body(MAX_UPLOAD))
    if parts == ["rescan"] and method == "POST":
        return w.api_rescan()
    if parts == ["build"] and method == "POST":
        w.build()
        return {"ok": True}
    if parts == ["deploy"]:
        if method == "GET":
            return deploy_info(w)
        if method == "PUT":
            return save_deploy(w, h.read_json())
    if parts == ["publish"] and method == "POST":
        d = h.read_json()
        return start_publish(w, d, dry=bool(d.get("dry")), test=bool(d.get("test")))
    if parts == ["publish", "status"] and method == "GET":
        return job_status(q.get("since", ["0"])[0])
    if parts == ["publish", "cancel"] and method == "POST":
        return cancel_publish()
    raise ApiError("Unknown request.", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = APP

    def log_message(self, *args):
        pass

    # ---- helpers
    def read_body(self, limit):
        try:
            n = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            raise ApiError("Bad Content-Length header.")
        if n > limit:
            raise ApiError("That file is too large.", 413)
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body(8 * 1024 * 1024)
        try:
            d = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ApiError("Bad request body.")
        if not isinstance(d, dict):
            raise ApiError("Bad request body.")
        return d

    def send_bytes(self, status, ctype, data, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def send_text(self, status, text):
        self.send_bytes(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def host_ok(self):
        port = SERVER["port"]
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def send_site(self, rel):
        w = CURRENT["site"]
        if w is None:
            return self.send_text(404, "No site is open.")
        rel = unquote(rel)
        if "\x00" in rel:
            return self.send_text(404, "Not found")
        target = (w.root / rel).resolve()
        try:
            parts = target.relative_to(w.root).parts
        except ValueError:
            return self.send_text(403, "Forbidden")
        if any(p.startswith(".") for p in parts):
            return self.send_text(404, "Not found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self.send_text(404, "Not found. Save the page first so it gets generated.")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("xml") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        # Built pages share an origin with the editor. Only same-origin script files may run (that is
        # search.js); inline script, such as one inside an uploaded SVG, is refused.
        self.send_bytes(200, ctype, target.read_bytes(), {
            "Content-Security-Policy": "script-src 'self'; object-src 'none'; base-uri 'self'",
            "X-Content-Type-Options": "nosniff",
        })

    # ---- routing
    def dispatch(self, method):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if not self.host_ok():
                return self.send_text(403, "Forbidden")
            if method == "GET" and path == "/":
                return self.send_bytes(200, "text/html; charset=utf-8", UI_HTML.replace("__TOKEN__", TOKEN).encode("utf-8"))
            if method == "GET" and path.startswith("/site/"):
                return self.send_site(path[len("/site/"):])
            if path.startswith("/api/"):
                if self.headers.get("X-Token") != TOKEN:
                    return self.send_json({"error": "Not authorized."}, 401)
                route = path[len("/api/"):]
                if route == "pick-folder" and method == "POST":
                    return self.send_json(pick_folder())
                if route == "quit" and method == "POST":
                    self.send_json({"ok": True})
                    threading.Thread(target=SERVER["httpd"].shutdown, daemon=True).start()
                    return
                with LOCK:
                    result = api(method, route, parts.query, self)
                return self.send_json(result)
            return self.send_text(404, "Not found")
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": "Unexpected error: %s" % e}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


# ----------------------------------------------------------------------------
# Editor UI (served to your browser; the editor uses JavaScript)
# ----------------------------------------------------------------------------

UI_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SiteForge</title>
<style>
:root {
  color-scheme: light;
  --ink: #18202e; --ink2: #242e42; --paper: #eef1f6; --panel: #ffffff; --panel2: #f7f9fc; --field: #ffffff;
  --line: #d3d9e3; --text: #1c2432; --muted: #5b6678; --accent: #2c5fe6;
  --primary: #2c5fe6; --primary-d: #2249b8;
  --btn-bg: #ffffff; --btn-hover: #f7f9fc; --btn-line-hover: #aab3c2;
  --side: #e5e9f0; --side-hover: #d9dfe9; --side-active: #ffffff;
  --right: #dde2ea; --right-bar: #eef1f6;
  --danger: #b3261e; --amber: #8a5a00; --amber-bg: #fff8e6; --amber-line: #e2c98f; --ok: #1c7a45;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #0b0f19; --ink2: #1a2233; --paper: #0f1420; --panel: #161c2a; --panel2: #1b2233; --field: #0f1522;
  --line: #2c3548; --text: #e2e8f4; --muted: #98a3b8; --accent: #7ea2ff;
  --primary: #3d68e6; --primary-d: #5079ee;
  --btn-bg: #1f2740; --btn-hover: #263050; --btn-line-hover: #4a5878;
  --side: #121826; --side-hover: #1d2536; --side-active: #1f2940;
  --right: #0c111b; --right-bar: #131a28;
  --danger: #ff8a80; --amber: #f0c060; --amber-bg: #2b2412; --amber-line: #5a4a1e; --ok: #5fd69a;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; margin: 0; }
body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text); background: var(--paper); }
button, input, select, textarea { font: inherit; color: inherit; }
button { border: 1px solid var(--line); background: var(--btn-bg); padding: 5px 11px; border-radius: 6px; cursor: pointer; }
button:hover { border-color: var(--btn-line-hover); background: var(--btn-hover); }
button:disabled { opacity: 0.45; cursor: default; }
button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:hover { background: var(--primary-d); border-color: var(--primary-d); }
button.danger { color: var(--danger); }
input[type=text], input[type=date], input[type=number], input[type=password], input:not([type]), select, textarea.small {
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); min-width: 0;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.grow { flex: 1; }
.hint { color: var(--muted); font-size: 12px; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }

#workspace { display: flex; flex-direction: column; height: 100vh; }
#top { background: var(--ink); border-bottom: 1px solid var(--ink2); color: #fff; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 12px; }
#top button { padding: 4px 9px; background: transparent; border-color: #3b465e; color: #e8ecf5; }
#top button:hover { background: var(--ink2); border-color: #5a6785; }
#top .brand { font-weight: 700; margin-right: 6px; }
#top .sep { width: 1px; height: 22px; background: #3b465e; margin: 0 4px; }
#folder { color: #9aa6bf; font: 12px ui-monospace, Consolas, monospace; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#dirty { color: #ffd479; font-size: 12px; }

#main { flex: 1; min-height: 0; display: grid; grid-template-columns: 250px minmax(0, 1fr) minmax(0, 1fr); }
#side { background: var(--side); border-right: 1px solid var(--line); overflow: auto; padding: 8px; }
#side h3 { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); margin: 16px 6px 4px; display: flex; justify-content: space-between; align-items: center; }
#side h3 button { font-size: 11px; padding: 0 7px; text-transform: none; letter-spacing: 0; font-weight: 500; }
.item { display: flex; justify-content: space-between; align-items: center; gap: 6px; width: 100%; text-align: left; border: 0; background: transparent; padding: 5px 8px; border-radius: 6px; }
.item:hover { background: var(--side-hover); }
.item.active { background: var(--side-active); box-shadow: inset 3px 0 0 var(--accent); }
.item .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item.group { font-weight: 600; margin-top: 4px; }
.item.child { padding-left: 22px; }
.item.add { padding-left: 22px; color: var(--muted); font-size: 12px; }
.chip { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); white-space: nowrap; color: var(--muted); }
.chip.draft { color: var(--amber); border-color: var(--amber-line); background: var(--amber-bg); }
.chip.home { color: var(--accent); border-color: var(--accent); }
.empty { color: var(--muted); padding: 4px 8px; font-size: 13px; }

#left { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); border-right: 1px solid var(--line); overflow: auto; }
#left .fill { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.bar.sub { background: var(--panel2); }
.bar.warn { background: var(--amber-bg); color: var(--amber); border-color: var(--amber-line); font-size: 13px; }
.bar label { display: inline-flex; align-items: center; gap: 6px; }
.bar label.wide { flex: 1; min-width: 220px; }
.bar label.wide input { flex: 1; }
.title-input { flex: 1; min-width: 140px; font-size: 16px; font-weight: 600; border: 1px solid transparent !important; padding: 5px 8px; }
.title-input:hover, .title-input:focus { border-color: var(--line) !important; }
.md-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--line); }
.md-tools button { padding: 2px 8px; font-size: 13px; }
.code { flex: 1; width: 100%; min-height: 240px; border: 0; resize: none; padding: 14px; background: var(--panel); color: var(--text);
  font: 14px/20px ui-monospace, "SF Mono", Consolas, "DejaVu Sans Mono", monospace; tab-size: 2; }
.slug-wrap { display: inline-flex; align-items: center; gap: 2px; color: var(--muted); }
.slug-wrap input { width: 160px; }
.pad { padding: 14px; display: flex; flex-direction: column; gap: 12px; max-width: 640px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field input, .field select, .field textarea { width: 100%; }
.field textarea, textarea.small { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; background: var(--field); resize: vertical; font: inherit; }
.two { display: flex; gap: 10px; flex-wrap: wrap; }
.two > * { flex: 1 1 12rem; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
legend { font-weight: 600; padding: 0 4px; }
details.box { border-bottom: 1px solid var(--line); }
details.box > summary { cursor: pointer; padding: 8px 10px; font-weight: 600; background: var(--panel2); list-style: none; }
details.box > summary::-webkit-details-marker { display: none; }
details.box > summary::before { content: "\25B8 "; color: var(--muted); }
details.box[open] > summary::before { content: "\25BE "; }
details.box .pad { max-width: none; }
.row { display: flex; align-items: center; gap: 6px; padding: 5px 10px; border-top: 1px solid var(--line); }
.row .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .n { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.5em; text-align: right; }
.row button { padding: 1px 8px; }
.row input[type=text], .row input:not([type]) { flex: 1; }
.words { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.tagpick { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.tagpick button { padding: 1px 9px; font-size: 12px; border-radius: 99px; }
.tagpick button.on { background: var(--primary); border-color: var(--primary); color: #fff; }
.mod-card { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
.mod-card h4 { margin: 0; display: flex; align-items: center; gap: 8px; }
.log { flex: 1; min-height: 160px; margin: 0; padding: 10px 12px; overflow: auto; white-space: pre-wrap;
  background: var(--field); border-top: 1px solid var(--line); font: 12px/1.5 ui-monospace, Consolas, monospace; }

#right { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--right); }
#right .bar { background: var(--right-bar); }
#right a { color: var(--accent); }
#pv-wrap { flex: 1; position: relative; overflow: hidden; }
#preview { position: absolute; top: 0; left: 0; border: 0; background: #fff; transform-origin: 0 0; }
#pv-size button { padding: 2px 8px; font-size: 12px; }
#pv-size button.on { background: var(--primary); border-color: var(--primary); color: #fff; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: #fff; padding: 8px 16px; border-radius: 8px; opacity: 0; pointer-events: none; transition: opacity 0.15s; max-width: 80vw; white-space: pre-line; z-index: 50; }
#toast.show { opacity: 1; }
#toast.bad { background: #b3261e; }

.card-screen { max-width: 640px; margin: 8vh auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 26px 28px; }
.card-screen h1 { margin: 0 0 6px; font-size: 22px; }
.card-screen p { color: var(--muted); margin: 0 0 16px; }
.card-screen .row2 { display: flex; gap: 8px; margin-bottom: 12px; }
.card-screen .row2 input { flex: 1; }
.choices { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 6px 0 14px; }
.choice { display: flex; gap: 8px; align-items: flex-start; border: 1px solid var(--line); border-radius: 8px; padding: 10px; cursor: pointer; }
.choice:has(input:checked) { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.choice b { display: block; }
.choice span { color: var(--muted); font-size: 12px; }
</style>
<script>
(function () {
  var t = null;
  try { t = localStorage.getItem("siteforge-theme"); } catch (e) { t = null; }
  if (t !== "dark" && t !== "light") t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
</head>
<body>

<div id="welcome" class="card-screen" hidden>
  <h1>SiteForge</h1>
  <p>Choose the folder for your site. A new or empty folder starts a new site. A folder that already holds a SiteForge site opens as you left it.</p>
  <div class="row2">
    <input id="w-path" aria-label="Site folder path" placeholder="/home/you/my-site">
    <button id="w-browse">Browse</button>
  </div>
  <button id="w-open" class="primary">Open</button>
</div>

<div id="setup" class="card-screen" hidden>
  <h1>New site</h1>
  <p id="u-folder" class="mono"></p>
  <label class="field">Site title <input id="u-title"></label>
  <p style="margin:12px 0 4px;color:var(--text)"><b>What should the homepage be?</b></p>
  <div class="choices">
    <label class="choice"><input type="radio" name="u-home" value="page" checked><div><b>A page</b><span>A normal page you write, like a landing or about page.</span></div></label>
    <label class="choice"><input type="radio" name="u-home" value="blog"><div><b>A blog feed</b><span>Your newest posts, with tags and an RSS feed.</span></div></label>
    <label class="choice"><input type="radio" name="u-home" value="wiki"><div><b>A wiki</b><span>Sections of articles with a sidebar and search.</span></div></label>
    <label class="choice"><input type="radio" name="u-home" value="library"><div><b>A book list</b><span>Short stories and serials, with chapters.</span></div></label>
  </div>
  <p style="margin:0 0 4px;color:var(--text)"><b>Also switch on</b> <span class="hint">(you can change all of this later in Site structure)</span></p>
  <div style="display:flex;gap:14px;margin-bottom:12px">
    <label><input type="checkbox" class="u-mod" value="blog"> Blog</label>
    <label><input type="checkbox" class="u-mod" value="wiki"> Wiki</label>
    <label><input type="checkbox" class="u-mod" value="library"> Library</label>
  </div>
  <label class="field">Theme <select id="u-theme"></select></label>
  <details style="margin:12px 0"><summary>Import an existing SiteGen, WikiGen or StoryGen site</summary>
    <div class="pad" style="padding:10px 0 0">
      <div class="row2" style="margin:0"><input id="u-import" placeholder="/opt/trstswiki (the folder with .sitegen, .wikigen or .storygen in it)"><button id="u-import-browse">Browse</button><button id="u-detect">Check</button></div>
      <span class="hint" id="u-detected"></span>
      <label id="u-rootrow" hidden><input type="checkbox" id="u-root" checked> Serve it from the site root, so its existing URLs keep working</label>
    </div>
  </details>
  <div style="display:flex;gap:8px"><button id="u-create" class="primary">Create site</button><button id="u-cancel">Back</button></div>
</div>

<div id="workspace" hidden>
  <div id="top">
    <span class="brand">SiteForge</span>
    <button data-act="new-page">Page</button>
    <button data-act="new-post" data-mod="blog">Post</button>
    <button data-act="new-article" data-mod="wiki">Article</button>
    <button data-act="new-story" data-mod="library">Story</button>
    <button data-act="new-chapter" data-mod="library">Chapter</button>
    <span class="sep"></span>
    <button data-act="structure">Site structure</button>
    <button data-act="menu">Menu</button>
    <button data-act="css">Edit CSS</button>
    <button data-act="settings">Settings</button>
    <button data-act="rescan" id="b-rescan" title="Pick up .md files added outside the editor">Rescan files</button>
    <button data-act="publish">Publish</button>
    <span class="grow"></span>
    <span id="dirty" hidden>Unsaved changes</span>
    <span id="folder" title=""></span>
    <button data-act="theme" id="b-theme">Dark mode</button>
    <button data-act="switch">Switch folder</button>
    <button data-act="quit">Quit</button>
  </div>
  <div id="main">
    <aside id="side" aria-label="Site contents"></aside>
    <section id="left" aria-label="Editor"></section>
    <section id="right" aria-label="Preview">
      <div class="bar"><span class="hint">Live preview</span><span id="pv-size" role="group" aria-label="Preview width"><button data-pv="desktop">Desktop</button> <button data-pv="mobile">Mobile</button> <button data-pv="fit">Fit</button></span><span class="grow"></span><a id="open-built" href="/site/index.html" target="_blank" rel="noopener">Open built site</a></div>
      <div id="pv-wrap"><iframe id="preview" title="Site preview" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe></div>
    </section>
  </div>
</div>
<div id="toast" role="status"></div>
<script>
const TOKEN = "__TOKEN__";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const enc = encodeURIComponent;

let S = null;
let view = { type: "none" };
let dirty = false;
let timer = null;
let slugTouched = false;

/* ---------- basics ---------- */
async function api(method, path, body) {
  const opt = { method, headers: { "X-Token": TOKEN } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api/" + path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { data = { error: "Unexpected response from the server." }; }
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show " + (kind || "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { t.className = ""; }, kind === "bad" ? 5000 : 3200);
}

function setDirty(v) { dirty = v; $("#dirty").hidden = !v; }
function markDirty() { setDirty(true); countWords(); schedulePreview(); }
async function guard() { return !dirty || window.confirm("You have unsaved changes. Discard them?"); }
function slugify(s) {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
function fmt(n) { return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function on(mid) { return !!(S && S.modules[mid] && S.modules[mid].enabled); }
function label(mid) { return (S.modules[mid] && S.modules[mid].label) || S.names[mid]; }
function el(tag, props, kids) {
  const e = document.createElement(tag);
  Object.entries(props || {}).forEach(([k, v]) => { if (k === "text") e.textContent = v; else if (k === "on") Object.entries(v).forEach(([ev, f]) => e.addEventListener(ev, f)); else e.setAttribute(k, v); });
  (kids || []).forEach((k) => e.appendChild(k));
  return e;
}
function opts(select, items, value) {
  select.innerHTML = "";
  items.forEach(([v, t]) => select.appendChild(el("option", { value: v, text: t })));
  if (value !== undefined) select.value = value;
}
function story(slug) { return S.library.stories.find((s) => s.slug === slug); }
function section(slug) { return S.wiki.sections.find((s) => s.slug === slug); }
function post(slug) { return S.posts.find((p) => p.slug === slug); }
function page(slug) { return S.pages.find((p) => p.slug === slug); }

/* ---------- markdown toolbar ---------- */
const MD_TOOLS = `
<div class="md-tools">
  <button data-md="bold" title="Bold"><b>B</b></button>
  <button data-md="italic" title="Italic"><i>I</i></button>
  <button data-md="heading">Heading</button>
  <button data-md="link">Link</button>
  <button data-md="wiki" data-need="wiki" title="Link to a wiki article by its title">Wiki link</button>
  <button data-md="image">Image</button>
  <button data-md="code">Code</button>
  <button data-md="block">Code block</button>
  <button data-md="list">List</button>
  <button data-md="steps">Steps</button>
  <button data-md="quote">Quote</button>
  <button data-md="callout">Callout</button>
  <button data-md="break" title="Scene break">Scene break</button>
  <button data-md="table">Table</button>
  <span class="grow"></span>
  <span class="words" id="wc"></span>
  <input type="file" id="img-file" accept="image/*" hidden>
</div>`;

const SEO_BLOCK = `
<details class="box" id="seo">
  <summary>SEO and social sharing</summary>
  <div class="pad">
    <label class="field">Title tag override <input id="seo-title" maxlength="120" placeholder="Defaults to the title, followed by the site name"></label>
    <label class="field">Meta description <textarea id="seo-desc" rows="2" maxlength="320" placeholder="Shown in search results and social cards"></textarea><span class="hint" id="seo-count"></span></label>
    <label class="field">Social image <input id="seo-image" placeholder="assets/cover.jpg or a full URL. Defaults to the site image."></label>
    <label class="field">Canonical URL <input id="seo-canonical" placeholder="Leave blank unless this is also published at another URL"></label>
    <label><input type="checkbox" id="seo-noindex"> Ask search engines not to index this</label>
  </div>
</details>`;

function setSeo(rec) {
  $("#seo-title").value = rec.seo_title || "";
  $("#seo-desc").value = rec.description || "";
  $("#seo-image").value = rec.image || "";
  $("#seo-canonical").value = rec.canonical || "";
  $("#seo-noindex").checked = !!rec.noindex;
  const count = () => { const n = $("#seo-desc").value.trim().length; $("#seo-count").textContent = n ? n + " characters" + (n > 160 ? ", may be shortened in search results" : "") : ""; };
  ["#seo-title", "#seo-desc", "#seo-image", "#seo-canonical"].forEach((id) => $(id).addEventListener("input", () => { count(); markDirty(); }));
  $("#seo-noindex").addEventListener("change", markDirty);
  count();
}
function seoVals() {
  return { seo_title: $("#seo-title").value, description: $("#seo-desc").value, image: $("#seo-image").value,
    canonical: $("#seo-canonical").value, noindex: $("#seo-noindex").checked };
}

function countWords() {
  const w = $("#wc"), ta = $("#md");
  if (!w || !ta) return;
  const n = (ta.value.replace(/[#*_>`\[\]()~|-]/g, " ").match(/\S+/g) || []).length;
  w.textContent = fmt(n) + " words";
}

function applyMd(ta, kind) {
  const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
  const wrap = (a, b, ph) => { const t = sel || ph; ta.setRangeText(a + t + b, s, e, "end"); ta.setSelectionRange(s + a.length, s + a.length + t.length); };
  const prefix = (p) => { const ls = ta.value.lastIndexOf("\n", s - 1) + 1; ta.setRangeText(p, ls, ls, "preserve"); ta.setSelectionRange(s + p.length, e + p.length); };
  const block = (text) => {
    const before = ta.value.slice(0, s);
    const lead = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    ta.setRangeText(lead + text + "\n", s, e, "end");
  };
  if (kind === "bold") wrap("**", "**", "bold text");
  else if (kind === "italic") wrap("*", "*", "italic text");
  else if (kind === "code") wrap("`", "`", "code");
  else if (kind === "link") wrap("[", "](https://)", "link text");
  else if (kind === "wiki") wrap("[[", "]]", "Article title");
  else if (kind === "heading") prefix("## ");
  else if (kind === "list") prefix("- ");
  else if (kind === "quote") prefix("> ");
  else if (kind === "steps") block(sel ? sel.split("\n").map((l, i) => (i + 1) + ". " + l).join("\n") : "1. First step\n2. Second step\n3. Third step");
  else if (kind === "block") block("```bash\n" + (sel || "command here") + "\n```");
  else if (kind === "callout") block("> [!NOTE]\n> " + (sel || "Something the reader should notice.").split("\n").join("\n> "));
  else if (kind === "break") block("* * *\n");
  else if (kind === "table") block("| Column | Column |\n|---|---|\n| Cell | Cell |");
  ta.dispatchEvent(new Event("input"));
  ta.focus();
}

async function uploadImage(file) {
  const r = await fetch("/api/upload?name=" + enc(file.name), { method: "POST", headers: { "X-Token": TOKEN }, body: file });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Upload failed");
  return d.path;
}

function bindEditor() {
  const ta = $("#md");
  ta.addEventListener("input", markDirty);
  $$(".md-tools button").forEach((b) => {
    if (b.dataset.need && !on(b.dataset.need)) b.hidden = true;
    b.addEventListener("click", () => { if (b.dataset.md === "image") $("#img-file").click(); else applyMd(ta, b.dataset.md); });
  });
  $("#img-file").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (!f) return;
    try {
      const path = await uploadImage(f);
      ta.setRangeText("![" + f.name.replace(/\.[^.]+$/, "") + "](" + path + ")", ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input"));
      toast("Image added to assets");
    } catch (e) { toast(e.message, "bad"); }
  });
  countWords();
}

function importInto(titleSel, slugSel) {
  const inp = el("input", { type: "file", accept: ".md,.markdown,.txt,text/markdown,text/plain", hidden: "" });
  document.body.appendChild(inp);
  inp.addEventListener("change", () => {
    const f = inp.files[0];
    inp.remove();
    if (!f) return;
    const ta = $("#md");
    if (ta.value.trim() && !window.confirm("Replace the text in the editor with " + f.name + "?")) return;
    const rd = new FileReader();
    rd.onload = () => {
      const text = String(rd.result).replace(/\r\n?/g, "\n");
      ta.value = text;
      if (!$(titleSel).value.trim()) {
        const first = text.split("\n").find((l) => l.trim()) || "";
        const m = first.match(/^\s*#\s+(.+?)\s*#*\s*$/);
        const t = m ? m[1] : f.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
        $(titleSel).value = t.charAt(0).toUpperCase() + t.slice(1);
        if (!slugTouched) $(slugSel).value = slugify($(titleSel).value);
      }
      markDirty();
      toast("Imported " + f.name + ". Save to keep it.");
    };
    rd.readAsText(f);
  });
  inp.click();
}

/* ---------- preview ---------- */
let pvMode = "desktop";
try { pvMode = localStorage.getItem("siteforge-preview") || "desktop"; } catch (e) { /* ignore */ }

function fitPreview() {
  const wrap = $("#pv-wrap"), f = $("#preview");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!W || !H) return;
  const target = pvMode === "desktop" ? 1280 : pvMode === "mobile" ? 390 : W;
  const s = Math.min(1, W / target);
  f.style.width = target + "px";
  f.style.height = (H / s) + "px";
  f.style.transform = "translateX(" + Math.max(0, (W - target * s) / 2) + "px) scale(" + s + ")";
  $$("#pv-size button").forEach((b) => b.classList.toggle("on", b.dataset.pv === pvMode));
}
$$("#pv-size button").forEach((b) => b.addEventListener("click", () => {
  pvMode = b.dataset.pv;
  try { localStorage.setItem("siteforge-preview", pvMode); } catch (e) { /* ignore */ }
  fitPreview();
}));
new ResizeObserver(fitPreview).observe($("#pv-wrap"));

function schedulePreview() { clearTimeout(timer); timer = setTimeout(renderPreview, 260); }

function previewPayload() {
  const md = () => $("#md").value;
  switch (view.type) {
    case "page": return Object.assign({ view: "page", slug: view.slug, title: $("#g-title").value, show_title: $("#g-show").checked, markdown: md() }, seoVals());
    case "landing": return { view: { blog: "blogintro", wiki: "wikihome", library: "libhome" }[view.mod], markdown: md() };
    case "post": return Object.assign({ view: "post", orig_slug: view.slug, new_slug: $("#p-slug").value, title: $("#p-title").value,
      date: $("#p-date").value, summary: $("#p-summary").value, tags: $("#p-tags").value, markdown: md() }, seoVals());
    case "section": return { view: "section", slug: view.slug, title: $("#s-title").value, description: $("#s-desc").value, markdown: md() };
    case "article": return { view: "article", section: $("#a-sec").value, orig_section: view.sec || "", orig_slug: view.slug || "",
      new_slug: $("#a-slug").value, title: $("#a-title").value, description: $("#a-desc").value, markdown: md() };
    case "story": return storyPayload();
    case "chapter": return { view: "chapter", story: $("#c-story").value, orig_story: view.story || "", orig_slug: view.slug || "",
      new_slug: $("#c-slug").value, title: $("#c-title").value, date: $("#c-date").value, note: $("#c-note").value, markdown: md() };
    case "css": return { view: "css", target: $("#css-target").value, css: $("#css").value };
    case "settings": return { view: "settings", target: "home", site: readSettings() };
    case "menu": return { view: "menu", target: "home", nav: S.nav };
  }
  return null;
}

async function renderPreview() {
  const p = previewPayload();
  if (!p) return;
  try {
    const r = await api("POST", "preview", p);
    const f = $("#preview");
    let y = 0;
    try { y = f.contentWindow.scrollY || 0; } catch (e) { y = 0; }
    f.onload = () => { try { f.contentWindow.scrollTo(0, y); } catch (e) { /* ignore */ } };
    f.srcdoc = r.html;
    const m = $("#missing");
    if (m) {
      m.hidden = !r.missing.length;
      m.textContent = r.missing.length ? "Wiki links with no matching article: " + r.missing.join(", ") : "";
    }
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- sidebar ---------- */
function renderTop() {
  $("#folder").textContent = S.folder;
  $("#folder").title = S.folder;
  $$("#top [data-mod]").forEach((b) => { b.hidden = !on(b.dataset.mod); });
  $("#b-rescan").hidden = !(on("wiki") || on("library"));
}

function renderSide() {
  const side = $("#side");
  side.innerHTML = "";
  const item = (text, cls, active, fn, chip) => {
    const b = el("button", { class: "item " + cls + (active ? " active" : "") }, [el("span", { class: "t", text })]);
    if (chip) b.appendChild(el("span", { class: "chip " + (chip[1] || ""), text: chip[0] }));
    b.addEventListener("click", fn);
    side.appendChild(b);
  };
  const head = (text, btn, fn) => {
    const h = el("h3", {}, [el("span", { text })]);
    if (btn) h.appendChild(el("button", { text: btn, on: { click: () => go(fn) } }));
    side.appendChild(h);
  };
  const homeLabel = S.home === "page" ? "Home page" : "Home page (" + label(S.home) + ")";
  item(homeLabel, "", view.type === "page" && view.slug === "index" || (view.type === "landing" && view.mod === S.home), () => go(openHome), ["Home", "home"]);
  head("Pages", "+ Page", newPage);
  S.pages.filter((p) => p.slug !== "index").forEach((p) => item(p.title, "", view.type === "page" && view.slug === p.slug, () => go(() => openPage(p.slug))));
  if (on("blog")) {
    head(label("blog"), "+ Post", () => openPost(null));
    if (S.home !== "blog") item("Feed page", "", view.type === "landing" && view.mod === "blog", () => go(() => openLanding("blog")));
    S.posts.slice().sort((a, b) => (b.date + b.title).localeCompare(a.date + a.title)).forEach((p) =>
      item(p.title, "", view.type === "post" && view.slug === p.slug, () => go(() => openPost(p.slug)), p.status === "draft" ? ["Draft", "draft"] : null));
  }
  if (on("wiki")) {
    head(label("wiki"), "+ Section", newSection);
    if (S.home !== "wiki") item(label("wiki") + " home", "", view.type === "landing" && view.mod === "wiki", () => go(() => openLanding("wiki")));
    S.wiki.sections.forEach((s) => {
      item(s.title, "group", view.type === "section" && view.slug === s.slug, () => go(() => openSection(s.slug)), [String(s.articles.length)]);
      s.articles.forEach((a) => item(a.title, "child", view.type === "article" && view.sec === s.slug && view.slug === a.slug,
        () => go(() => openArticle(s.slug, a.slug)), a.draft ? ["Draft", "draft"] : null));
      item("+ Article", "add", false, () => go(() => openArticle(null, null, s.slug)));
    });
  }
  if (on("library")) {
    head(label("library"), "+ Story", newStory);
    if (S.home !== "library") item(label("library") + " page", "", view.type === "landing" && view.mod === "library", () => go(() => openLanding("library")));
    S.library.stories.forEach((s) => {
      const chip = s.draft ? ["Draft", "draft"] : (s.kind === "single" ? ["Single"] : [String(s.chapters.length)]);
      item(s.title, "group", view.type === "story" && view.slug === s.slug, () => go(() => openStory(s.slug)), chip);
      s.chapters.forEach((ch) => item(ch.title, "child", view.type === "chapter" && view.story === s.slug && view.slug === ch.slug,
        () => go(() => openChapter(s.slug, ch.slug)), ch.draft ? ["Draft", "draft"] : null));
      if (s.kind !== "single") item("+ Chapter", "add", false, () => go(() => openChapter(null, null, s.slug)));
    });
  }
  if (!on("blog") && !on("wiki") && !on("library")) {
    side.appendChild(el("p", { class: "empty", text: "Add a blog, wiki or library in Site structure." }));
  }
}

async function refresh() { S = await api("GET", "state"); renderTop(); renderSide(); }
async function go(fn) {
  if (!(await guard())) return;
  setDirty(false);
  try { await fn(); } catch (e) { toast(e.message, "bad"); }
}
function show(html) { $("#left").innerHTML = html.replace("MDTOOLS", MD_TOOLS).replace("SEOBLOCK", SEO_BLOCK); }
function watch(ids) { ids.forEach((id) => { const e = $(id); if (e) e.addEventListener(e.type === "checkbox" || e.tagName === "SELECT" ? "change" : "input", markDirty); }); }

/* ---------- home ---------- */
async function openHome() {
  if (S.home === "page") await openPage("index");
  else await openLanding(S.home);
}

/* ---------- pages ---------- */
const PAGE_TPL = `
<div class="fill">
  <div class="bar">
    <input id="g-title" class="title-input" placeholder="Page title" aria-label="Page title">
    <label><input type="checkbox" id="g-show"> Show title</label>
    <button id="g-save" class="primary">Save</button>
  </div>
  <div class="bar sub" id="g-sub">
    <label>Address <span class="slug-wrap"><input id="g-slug" aria-label="Page address">.html</span></label>
    <span class="grow"></span>
    <button id="g-delete" class="danger">Delete page</button>
  </div>
  <div class="bar warn" id="g-unused" hidden></div>
  SEOBLOCK
  <div class="bar warn" id="missing" hidden></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the page in Markdown"></textarea>
</div>`;

async function newPage() {
  const title = window.prompt("Page title. Type About or Privacy policy to start from a template.");
  if (!title || !title.trim()) return;
  const t = title.trim().toLowerCase();
  const template = t === "about" ? "about" : (t.startsWith("privacy") ? "privacy" : "");
  const r = await api("POST", "pages", { title: title.trim(), template });
  await refresh();
  await openPage(r.slug);
}

async function openPage(slug) {
  const d = await api("GET", "pages/" + enc(slug));
  view = { type: "page", slug };
  show(PAGE_TPL);
  $("#g-title").value = d.page.title;
  $("#g-show").checked = !!d.page.show_title;
  $("#g-slug").value = d.page.slug;
  $("#md").value = d.markdown;
  $("#g-sub").hidden = slug === "index";
  if (slug === "index" && S.home !== "page") {
    $("#g-unused").hidden = false;
    $("#g-unused").textContent = "The homepage is currently the " + label(S.home) + ", so this page is not published. Change that in Site structure.";
  }
  setSeo(d.page);
  watch(["#g-title", "#g-show", "#g-slug"]);
  $("#g-save").addEventListener("click", savePage);
  $("#g-delete").addEventListener("click", deletePage);
  bindEditor();
  renderSide();
  renderPreview();
}

async function savePage() {
  try {
    const r = await api("PUT", "pages/" + enc(view.slug), Object.assign({ title: $("#g-title").value, show_title: $("#g-show").checked,
      new_slug: $("#g-slug").value, markdown: $("#md").value }, seoVals()));
    view.slug = r.slug;
    $("#g-slug").value = r.slug;
    setDirty(false);
    await refresh();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function deletePage() {
  if (!window.confirm("Delete this page? Its markdown is moved to the trash folder.")) return;
  try { await api("DELETE", "pages/" + enc(view.slug)); setDirty(false); await refresh(); await openHome(); toast("Page deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- module landing pages ---------- */
const LANDING_TPL = `
<div class="fill">
  <div class="bar"><strong id="l-title"></strong><span class="hint" id="l-hint"></span><span class="grow"></span><button id="l-save" class="primary">Save</button></div>
  <div class="bar warn" id="missing" hidden></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true"></textarea>
</div>`;
const LANDING_API = { blog: "blog-intro", wiki: "wiki-home", library: "lib-home" };

async function openLanding(mod) {
  const d = await api("GET", LANDING_API[mod]);
  view = { type: "landing", mod };
  show(LANDING_TPL);
  $("#l-title").textContent = (S.home === mod ? "Home page: " : "") + label(mod) + (mod === "blog" ? " feed page" : mod === "wiki" ? " home" : " page");
  $("#l-hint").textContent = { blog: "Optional introduction shown above the list of posts.",
    wiki: "Shown above the section cards and recent changes.", library: "Optional introduction shown above your stories." }[mod];
  $("#md").value = d.markdown;
  $("#l-save").addEventListener("click", saveLanding);
  bindEditor();
  renderSide();
  renderPreview();
}

async function saveLanding() {
  try { await api("PUT", LANDING_API[view.mod], { markdown: $("#md").value }); setDirty(false); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- blog posts ---------- */
const POST_TPL = `
<div class="fill">
  <div class="bar">
    <input id="p-title" class="title-input" placeholder="Post title" aria-label="Post title">
    <span class="chip" id="p-status"></span>
    <button id="p-save">Save draft</button>
    <button id="p-publish" class="primary">Publish</button>
    <button id="p-unpublish" hidden>Unpublish</button>
  </div>
  <div class="bar sub">
    <label>Date <input type="date" id="p-date"></label>
    <label>Address <span class="slug-wrap"><span id="p-base"></span><input id="p-slug" aria-label="Post address">.html</span></label>
    <span class="grow"></span>
    <button id="p-import">Import .md</button>
    <button id="p-delete" class="danger">Delete</button>
  </div>
  <div class="bar sub"><label class="wide">Summary <input id="p-summary" maxlength="400" placeholder="Optional. Defaults to the first paragraph."></label></div>
  <div class="bar sub"><label class="wide">Tags <input id="p-tags" placeholder="Life, Tech (separate with commas)"></label><span class="tagpick" id="p-tagpick"></span></div>
  SEOBLOCK
  <div class="bar warn" id="missing" hidden></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the post in Markdown"></textarea>
</div>`;

function renderTagPick() {
  const box = $("#p-tagpick");
  if (!box) return;
  const have = $("#p-tags").value.split(",").map((t) => t.trim().toLowerCase()).filter(Boolean);
  box.innerHTML = "";
  S.tags.forEach((t) => {
    const b = el("button", { type: "button", text: t, class: have.includes(t.toLowerCase()) ? "on" : "" });
    b.addEventListener("click", () => {
      let list = $("#p-tags").value.split(",").map((x) => x.trim()).filter(Boolean);
      if (list.some((x) => x.toLowerCase() === t.toLowerCase())) list = list.filter((x) => x.toLowerCase() !== t.toLowerCase());
      else list.push(t);
      $("#p-tags").value = list.join(", ");
      renderTagPick();
      markDirty();
    });
    box.appendChild(b);
  });
}

async function openPost(slug) {
  let p = { title: "", date: new Date().toLocaleDateString("en-CA"), summary: "", tags: [], status: "draft", slug: "" }, markdown = "";
  if (slug) { const d = await api("GET", "posts/" + enc(slug)); p = d.post; markdown = d.markdown; }
  view = { type: "post", slug: slug || "" };
  show(POST_TPL);
  $("#p-title").value = p.title;
  $("#p-date").value = p.date;
  $("#p-slug").value = p.slug;
  $("#p-base").textContent = (S.modules.blog.mount || "blog") + "/posts/";
  $("#p-summary").value = p.summary || "";
  $("#p-tags").value = (p.tags || []).join(", ");
  $("#md").value = markdown;
  slugTouched = !!slug;
  syncPost(p);
  setSeo(p);
  $("#p-title").addEventListener("input", () => { if (!slugTouched) $("#p-slug").value = slugify($("#p-title").value); markDirty(); });
  $("#p-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  $("#p-tags").addEventListener("input", () => { renderTagPick(); markDirty(); });
  watch(["#p-date", "#p-summary"]);
  $("#p-save").addEventListener("click", () => savePost(false));
  $("#p-publish").addEventListener("click", () => savePost(true));
  $("#p-unpublish").addEventListener("click", unpublishPost);
  $("#p-delete").addEventListener("click", deletePost);
  $("#p-import").addEventListener("click", () => importInto("#p-title", "#p-slug"));
  renderTagPick();
  bindEditor();
  renderSide();
  renderPreview();
  if (!slug) $("#p-title").focus();
}

function syncPost(p) {
  const pub = p.status === "published";
  $("#p-status").textContent = view.slug ? (pub ? "Published" : "Draft") : "New";
  $("#p-status").className = "chip" + (pub ? "" : " draft");
  $("#p-save").textContent = pub ? "Save" : "Save draft";
  $("#p-publish").textContent = pub ? "Save and update" : "Publish";
  $("#p-unpublish").hidden = !pub;
  $("#p-delete").hidden = !view.slug;
}

async function savePost(publish) {
  try {
    const r = await api("POST", "posts", Object.assign({ orig_slug: view.slug, new_slug: $("#p-slug").value, title: $("#p-title").value,
      date: $("#p-date").value, summary: $("#p-summary").value, tags: $("#p-tags").value, markdown: $("#md").value, publish }, seoVals()));
    view.slug = r.slug;
    $("#p-slug").value = r.slug;
    slugTouched = true;
    setDirty(false);
    await refresh();
    syncPost(r.post);
    renderTagPick();
    toast(publish ? "Published" : "Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function unpublishPost() {
  try { const r = await api("POST", "posts/" + enc(view.slug) + "/unpublish"); await refresh(); syncPost(r.post); toast("Moved back to drafts"); }
  catch (e) { toast(e.message, "bad"); }
}

async function deletePost() {
  if (!window.confirm("Delete this post? Its markdown is moved to the trash folder.")) return;
  try { await api("DELETE", "posts/" + enc(view.slug)); setDirty(false); await refresh(); await openHome(); toast("Post deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- shared list reordering ---------- */
function orderRows(box, items, labelOf, onMove, onEdit, extra) {
  box.innerHTML = "";
  if (!items.length) { box.appendChild(el("div", { class: "empty", text: "Nothing here yet." })); return; }
  items.forEach((it, i) => {
    const row = el("div", { class: "row" }, [el("span", { class: "n", text: String(i + 1) }), el("span", { class: "t", text: labelOf(it) })]);
    if (extra) row.appendChild(el("span", { class: "words", text: extra(it) }));
    const mk = (t, dis, fn) => { const b = el("button", { text: t }); b.disabled = dis; b.addEventListener("click", fn); row.appendChild(b); };
    mk("Up", i === 0, () => onMove(i, i - 1));
    mk("Down", i === items.length - 1, () => onMove(i, i + 1));
    mk("Edit", false, () => onEdit(it));
    box.appendChild(row);
  });
}
function moved(list, i, j) { const l = list.slice(); l.splice(j, 0, l.splice(i, 1)[0]); return l; }

/* ---------- wiki ---------- */
const SEC_TPL = `
<div class="fill">
  <div class="bar"><input id="s-title" class="title-input" placeholder="Section name" aria-label="Section name"><button id="s-save" class="primary">Save</button></div>
  <div class="bar sub">
    <label>Address <span class="slug-wrap"><span id="s-base"></span><input id="s-slug" aria-label="Section address">/</span></label>
    <span class="grow"></span>
    <button id="s-up">Move up</button><button id="s-down">Move down</button>
    <button id="s-delete" class="danger">Delete section</button>
  </div>
  <div class="bar sub"><label class="wide">Description <input id="s-desc" maxlength="320" placeholder="One line, shown under the section title and on the wiki home"></label></div>
  <details class="box" open><summary>Articles, in sidebar order</summary><div id="s-arts"></div>
    <div class="bar"><button id="s-new">New article here</button><button id="s-sort">Sort A to Z</button><span class="hint">Order changes save immediately.</span></div>
  </details>
  <div class="bar sub"><span class="hint">Introduction shown above the article list on the section page</span></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Optional introduction in Markdown"></textarea>
</div>`;

const ART_TPL = `
<div class="fill">
  <div class="bar">
    <input id="a-title" class="title-input" placeholder="Article title" aria-label="Article title">
    <label title="Drafts are saved but left out of the built site"><input type="checkbox" id="a-draft"> Draft</label>
    <button id="a-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Section <select id="a-sec"></select></label>
    <label>Address <span class="slug-wrap"><span id="a-base"></span><input id="a-slug" aria-label="Article address">.html</span></label>
    <span class="grow"></span>
    <button id="a-import">Import .md</button><button id="a-up">Move up</button><button id="a-down">Move down</button>
    <button id="a-delete" class="danger">Delete</button>
  </div>
  <div class="bar sub"><label class="wide">Description <input id="a-desc" maxlength="320" placeholder="Optional one line for search results and section lists"></label></div>
  <div class="bar warn" id="missing" hidden></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the article in Markdown"></textarea>
</div>`;

function wikiBase() { return S.modules.wiki.mount ? S.modules.wiki.mount + "/" : ""; }

async function newSection() {
  const title = window.prompt("Section name, for example Fun stuff or Linux - Technical");
  if (!title || !title.trim()) return;
  const r = await api("POST", "sections", { title: title.trim() });
  await refresh();
  await openSection(r.slug);
}

async function openSection(slug) {
  const d = await api("GET", "sections/" + enc(slug));
  view = { type: "section", slug };
  show(SEC_TPL);
  $("#s-title").value = d.section.title;
  $("#s-slug").value = d.section.slug;
  $("#s-base").textContent = wikiBase();
  $("#s-desc").value = d.section.description || "";
  $("#md").value = d.markdown;
  watch(["#s-title", "#s-slug", "#s-desc"]);
  $("#s-save").addEventListener("click", saveSection);
  $("#s-delete").addEventListener("click", deleteSection);
  $("#s-up").addEventListener("click", () => moveSection(-1));
  $("#s-down").addEventListener("click", () => moveSection(1));
  $("#s-new").addEventListener("click", () => go(() => openArticle(null, null, view.slug)));
  $("#s-sort").addEventListener("click", () => saveArtOrder(section(view.slug).articles.slice()
    .sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" })).map((a) => a.slug)));
  sectionRows();
  bindEditor();
  renderSide();
  renderPreview();
}

function sectionRows() {
  const s = section(view.slug);
  const i = S.wiki.sections.indexOf(s);
  $("#s-up").disabled = i <= 0;
  $("#s-down").disabled = i >= S.wiki.sections.length - 1;
  orderRows($("#s-arts"), s.articles, (a) => a.title + (a.draft ? " (draft)" : ""),
    (i2, j) => saveArtOrder(moved(s.articles.map((a) => a.slug), i2, j)), (a) => go(() => openArticle(s.slug, a.slug)));
}

async function saveArtOrder(list, sec) {
  try {
    await api("POST", "sections/" + enc(sec || view.slug) + "/order", { articles: list });
    await refresh();
    if (view.type === "section") sectionRows();
    if (view.type === "article") syncArticle();
    schedulePreview();
  } catch (e) { toast(e.message, "bad"); }
}

async function saveSection() {
  try {
    const r = await api("PUT", "sections/" + enc(view.slug), { title: $("#s-title").value, new_slug: $("#s-slug").value,
      description: $("#s-desc").value, markdown: $("#md").value });
    view.slug = r.slug;
    $("#s-slug").value = r.slug;
    setDirty(false);
    await refresh();
    sectionRows();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveSection(dir) {
  try { await api("POST", "sections/" + enc(view.slug) + "/move", { dir }); await refresh(); sectionRows(); schedulePreview(); }
  catch (e) { toast(e.message, "bad"); }
}

async function deleteSection() {
  const s = section(view.slug);
  if (!window.confirm("Delete the section \"" + s.title + "\"" + (s.articles.length ? " and its " + s.articles.length + " article(s)" : "") +
    "?\n\nThe markdown files are moved to the trash folder, not destroyed.")) return;
  try { await api("DELETE", "sections/" + enc(view.slug)); setDirty(false); await refresh(); await openHome(); toast("Section deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

async function openArticle(secSlug, slug, forSec) {
  if (!S.wiki.sections.length) { toast("Create a wiki section first. Articles live inside sections.", "bad"); await newSection(); return; }
  let art = { title: "", description: "", draft: false, slug: "" }, markdown = "", where = forSec || secSlug;
  if (slug) { const d = await api("GET", "articles/" + enc(secSlug) + "/" + enc(slug)); art = d.article; markdown = d.markdown; where = d.section; }
  if (!where) where = (view.type === "section" && view.slug) || (view.type === "article" && view.sec) || S.wiki.sections[0].slug;
  view = { type: "article", sec: slug ? where : "", slug: slug || "" };
  show(ART_TPL);
  opts($("#a-sec"), S.wiki.sections.map((s) => [s.slug, s.title]), where);
  $("#a-base").textContent = wikiBase() + where + "/";
  $("#a-title").value = art.title;
  $("#a-slug").value = art.slug;
  $("#a-desc").value = art.description || "";
  $("#a-draft").checked = !!art.draft;
  $("#md").value = markdown;
  slugTouched = !!slug;
  $("#a-title").addEventListener("input", () => { if (!slugTouched) $("#a-slug").value = slugify($("#a-title").value); markDirty(); });
  $("#a-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  watch(["#a-desc", "#a-draft"]);
  $("#a-sec").addEventListener("change", () => { $("#a-base").textContent = wikiBase() + $("#a-sec").value + "/"; markDirty(); });
  $("#a-save").addEventListener("click", saveArticle);
  $("#a-delete").addEventListener("click", deleteArticle);
  $("#a-up").addEventListener("click", () => moveArticle(-1));
  $("#a-down").addEventListener("click", () => moveArticle(1));
  $("#a-import").addEventListener("click", () => importInto("#a-title", "#a-slug"));
  syncArticle();
  bindEditor();
  renderSide();
  renderPreview();
  if (!slug) $("#a-title").focus();
}

function syncArticle() {
  const saved = !!view.slug;
  ["#a-delete", "#a-up", "#a-down"].forEach((id) => { $(id).hidden = !saved; });
  if (!saved) return;
  const s = section(view.sec);
  const i = s ? s.articles.findIndex((a) => a.slug === view.slug) : -1;
  $("#a-up").disabled = i <= 0;
  $("#a-down").disabled = i < 0 || i >= s.articles.length - 1;
}

async function saveArticle() {
  try {
    const r = await api("POST", "articles", { section: $("#a-sec").value, orig_section: view.sec || "", orig_slug: view.slug || "",
      new_slug: $("#a-slug").value, title: $("#a-title").value, description: $("#a-desc").value, draft: $("#a-draft").checked, markdown: $("#md").value });
    view.sec = r.section; view.slug = r.slug;
    $("#a-slug").value = r.slug;
    slugTouched = true;
    setDirty(false);
    await refresh();
    syncArticle();
    toast(r.article.draft ? "Saved as draft" : "Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveArticle(dir) {
  const list = section(view.sec).articles.map((a) => a.slug);
  const i = list.indexOf(view.slug);
  if (i < 0 || i + dir < 0 || i + dir >= list.length) return;
  await saveArtOrder(moved(list, i, i + dir), view.sec);
}

async function deleteArticle() {
  if (!window.confirm("Delete this article? Its markdown is moved to the trash folder.")) return;
  try { const s = view.sec; await api("DELETE", "articles/" + enc(s) + "/" + enc(view.slug)); setDirty(false); await refresh(); await openSection(s); toast("Article deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- library ---------- */
const STORY_TPL = `
<div class="fill">
  <div class="bar">
    <input id="t-title" class="title-input" placeholder="Story title" aria-label="Story title">
    <label title="Drafts are saved but left out of the built site"><input type="checkbox" id="t-draft"> Draft</label>
    <button id="t-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Address <span class="slug-wrap"><span id="t-base"></span><input id="t-slug" aria-label="Story address">/</span></label>
    <label>Status <select id="t-status"></select></label>
    <label>Form <select id="t-kind"><option value="chaptered">Chapters</option><option value="single">Single piece</option></select></label>
    <span class="grow"></span>
    <button id="t-up">Move up</button><button id="t-down">Move down</button>
    <button id="t-delete" class="danger">Delete</button>
  </div>
  <details class="box" id="t-details">
    <summary>Story details: blurb, cover, genres, author, series, dates</summary>
    <div class="pad">
      <div class="two"><label class="field">Subtitle <input id="t-subtitle"></label><label class="field">Author <input id="t-author" placeholder="Defaults to the name in Settings"></label></div>
      <label class="field">Blurb <textarea id="t-blurb" rows="3" maxlength="600" placeholder="The pitch, shown on the library page and the story page"></textarea></label>
      <div class="two">
        <label class="field">Cover image <span style="display:flex;gap:6px"><input id="t-cover" placeholder="assets/cover.jpg"><button id="t-cover-pick" type="button">Upload</button></span></label>
        <label class="field">Genres <input id="t-genres" placeholder="Horror, Novelette (separate with commas)"></label>
      </div>
      <label class="field">Content notes <input id="t-warnings" maxlength="400"></label>
      <div class="two"><label class="field">Series <input id="t-series"></label><label class="field">Number in series <input id="t-series_no"></label></div>
      <div class="two"><label class="field">Started <input type="date" id="t-started"></label><label class="field">Completed <input type="date" id="t-completed"></label></div>
      <label class="field">Meta description <input id="t-description" maxlength="320" placeholder="Defaults to the blurb"></label>
    </div>
  </details>
  <details class="box" open id="t-chapbox"><summary>Chapters, in reading order</summary><div id="t-chapters"></div>
    <div class="bar"><button id="t-new">New chapter</button><span class="hint" id="t-total"></span></div>
  </details>
  <div class="bar sub"><span class="hint" id="t-md-hint"></span></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true"></textarea>
</div>`;

const CHAP_TPL = `
<div class="fill">
  <div class="bar">
    <input id="c-title" class="title-input" placeholder="Chapter title" aria-label="Chapter title">
    <label title="Drafts are saved but left out of the built site"><input type="checkbox" id="c-draft"> Draft</label>
    <button id="c-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Story <select id="c-story"></select></label>
    <label>Date <input type="date" id="c-date"></label>
    <label>Address <span class="slug-wrap"><span id="c-base"></span><input id="c-slug" aria-label="Chapter address">.html</span></label>
    <span class="grow"></span>
    <button id="c-import">Import file</button><button id="c-up">Move up</button><button id="c-down">Move down</button>
    <button id="c-delete" class="danger">Delete</button>
  </div>
  <details class="box" id="c-notebox"><summary>Author's note, shown after the chapter</summary>
    <div class="pad"><textarea id="c-note" class="small" rows="3" placeholder="Optional. Markdown works here too."></textarea></div>
  </details>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the chapter here. A blank line starts a new paragraph, and * * * makes a scene break."></textarea>
</div>`;

const STORY_FIELDS = ["subtitle", "author", "blurb", "cover", "genres", "warnings", "series", "series_no", "description"];
function libBase() { return S.modules.library.mount ? S.modules.library.mount + "/" : ""; }

async function newStory() {
  const title = window.prompt("Story title");
  if (!title || !title.trim()) return;
  const kind = window.confirm("Is this a single piece, with no chapters?\n\nOK for a single short story, Cancel for something with chapters.") ? "single" : "chaptered";
  const r = await api("POST", "stories", { title: title.trim(), kind });
  await refresh();
  await openStory(r.slug);
}

function storyPayload() {
  const o = { view: "story", slug: view.slug, title: $("#t-title").value, status: $("#t-status").value, kind: $("#t-kind").value, markdown: $("#md").value };
  STORY_FIELDS.forEach((k) => { o[k] = $("#t-" + k).value; });
  return o;
}

async function openStory(slug) {
  const d = await api("GET", "stories/" + enc(slug));
  const s = d.story;
  view = { type: "story", slug };
  show(STORY_TPL);
  opts($("#t-status"), S.statuses.map((x) => [x.id, x.label]), s.status);
  $("#t-kind").value = s.kind;
  $("#t-title").value = s.title;
  $("#t-slug").value = s.slug;
  $("#t-base").textContent = libBase();
  $("#t-draft").checked = !!s.draft;
  STORY_FIELDS.forEach((k) => { $("#t-" + k).value = k === "genres" ? (s.genres || []).join(", ") : (s[k] || ""); });
  $("#t-started").value = s.started || "";
  $("#t-completed").value = s.completed || "";
  $("#md").value = d.markdown;
  $$("#left input, #left textarea, #left select").forEach((i) => {
    if (i.id !== "md" && i.type !== "file") i.addEventListener(i.type === "checkbox" || i.tagName === "SELECT" ? "change" : "input", markDirty);
  });
  $("#t-kind").addEventListener("change", syncStory);
  $("#t-save").addEventListener("click", saveStory);
  $("#t-delete").addEventListener("click", deleteStory);
  $("#t-up").addEventListener("click", () => moveStory(-1));
  $("#t-down").addEventListener("click", () => moveStory(1));
  $("#t-new").addEventListener("click", () => go(() => openChapter(null, null, view.slug)));
  $("#t-cover-pick").addEventListener("click", () => {
    const inp = el("input", { type: "file", accept: "image/*", hidden: "" });
    document.body.appendChild(inp);
    inp.addEventListener("change", async () => {
      const f = inp.files[0]; inp.remove();
      if (f) { try { $("#t-cover").value = await uploadImage(f); markDirty(); toast("Cover uploaded"); } catch (e) { toast(e.message, "bad"); } }
    });
    inp.click();
  });
  if (!s.blurb && !s.cover) $("#t-details").open = true;
  syncStory();
  bindEditor();
  renderSide();
  renderPreview();
}

function syncStory() {
  const single = $("#t-kind").value === "single";
  $("#t-chapbox").hidden = single;
  $("#t-md-hint").textContent = single ? "The story itself, shown on the story page under the blurb." : "Optional foreword, shown on the story page above the chapter list.";
  $("#md").placeholder = single ? "Write the story here" : "Optional foreword";
  const i = S.library.stories.findIndex((x) => x.slug === view.slug);
  $("#t-up").disabled = i <= 0;
  $("#t-down").disabled = i < 0 || i >= S.library.stories.length - 1;
  const s = story(view.slug);
  if (!s) return;
  orderRows($("#t-chapters"), s.chapters, (ch) => ch.title + (ch.draft ? " (draft)" : ""),
    (a, b) => saveChOrder(moved(s.chapters.map((ch) => ch.slug), a, b)), (ch) => go(() => openChapter(s.slug, ch.slug)),
    (ch) => ch.words ? fmt(ch.words) : "");
  const total = s.chapters.reduce((n, ch) => n + (ch.words || 0), 0);
  $("#t-total").textContent = s.chapters.length ? fmt(total) + " words across " + s.chapters.length + " chapter(s)" : "";
}

async function saveChOrder(list, st) {
  try {
    await api("POST", "stories/" + enc(st || view.slug) + "/order", { chapters: list });
    await refresh();
    if (view.type === "story") syncStory();
    if (view.type === "chapter") syncChapter();
    schedulePreview();
  } catch (e) { toast(e.message, "bad"); }
}

async function saveStory() {
  const body = storyPayload();
  delete body.view;
  Object.assign(body, { new_slug: $("#t-slug").value, draft: $("#t-draft").checked, started: $("#t-started").value, completed: $("#t-completed").value });
  try {
    const r = await api("PUT", "stories/" + enc(view.slug), body);
    view.slug = r.slug;
    $("#t-slug").value = r.slug;
    setDirty(false);
    await refresh();
    syncStory();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveStory(dir) {
  try { await api("POST", "stories/" + enc(view.slug) + "/move", { dir }); await refresh(); syncStory(); schedulePreview(); }
  catch (e) { toast(e.message, "bad"); }
}

async function deleteStory() {
  const s = story(view.slug);
  if (!window.confirm("Delete \"" + s.title + "\"" + (s.chapters.length ? " and its " + s.chapters.length + " chapter(s)" : "") +
    "?\n\nThe markdown files are moved to the trash folder, not destroyed.")) return;
  try { await api("DELETE", "stories/" + enc(view.slug)); setDirty(false); await refresh(); await openHome(); toast("Story deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

async function openChapter(storySlug, slug, forStory) {
  const withCh = S.library.stories.filter((s) => s.kind !== "single");
  if (!withCh.length) { toast("Create a story with chapters first.", "bad"); return; }
  let ch = { title: "", slug: "", draft: false, note: "", date: "" }, markdown = "", where = forStory || storySlug;
  if (slug) { const d = await api("GET", "chapters/" + enc(storySlug) + "/" + enc(slug)); ch = d.chapter; markdown = d.markdown; where = d.story; }
  if (!where || !withCh.some((s) => s.slug === where)) {
    where = (view.type === "story" && story(view.slug) && story(view.slug).kind !== "single" && view.slug) || (view.type === "chapter" && view.story) || withCh[0].slug;
  }
  view = { type: "chapter", story: slug ? where : "", slug: slug || "" };
  show(CHAP_TPL);
  opts($("#c-story"), withCh.map((s) => [s.slug, s.title]), where);
  $("#c-base").textContent = libBase() + where + "/";
  $("#c-title").value = ch.title;
  $("#c-slug").value = ch.slug;
  $("#c-date").value = ch.date || new Date().toLocaleDateString("en-CA");
  $("#c-draft").checked = !!ch.draft;
  $("#c-note").value = ch.note || "";
  $("#md").value = markdown;
  if (ch.note) $("#c-notebox").open = true;
  slugTouched = !!slug;
  $("#c-title").addEventListener("input", () => { if (!slugTouched) $("#c-slug").value = slugify($("#c-title").value); markDirty(); });
  $("#c-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  watch(["#c-date", "#c-note", "#c-draft"]);
  $("#c-story").addEventListener("change", () => { $("#c-base").textContent = libBase() + $("#c-story").value + "/"; markDirty(); });
  $("#c-save").addEventListener("click", saveChapter);
  $("#c-delete").addEventListener("click", deleteChapter);
  $("#c-up").addEventListener("click", () => moveChapter(-1));
  $("#c-down").addEventListener("click", () => moveChapter(1));
  $("#c-import").addEventListener("click", () => importInto("#c-title", "#c-slug"));
  syncChapter();
  bindEditor();
  renderSide();
  renderPreview();
  if (!slug) $("#c-title").focus();
}

function syncChapter() {
  const saved = !!view.slug;
  ["#c-delete", "#c-up", "#c-down"].forEach((id) => { $(id).hidden = !saved; });
  if (!saved) return;
  const s = story(view.story);
  const i = s ? s.chapters.findIndex((c) => c.slug === view.slug) : -1;
  $("#c-up").disabled = i <= 0;
  $("#c-down").disabled = i < 0 || i >= s.chapters.length - 1;
}

async function saveChapter() {
  try {
    const r = await api("POST", "chapters", { story: $("#c-story").value, orig_story: view.story || "", orig_slug: view.slug || "",
      new_slug: $("#c-slug").value, title: $("#c-title").value, date: $("#c-date").value, note: $("#c-note").value,
      draft: $("#c-draft").checked, markdown: $("#md").value });
    view.story = r.story; view.slug = r.slug;
    $("#c-slug").value = r.slug;
    slugTouched = true;
    setDirty(false);
    await refresh();
    syncChapter();
    toast(r.chapter.draft ? "Saved as draft" : "Saved, " + fmt(r.chapter.words) + " words");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveChapter(dir) {
  const list = story(view.story).chapters.map((c) => c.slug);
  const i = list.indexOf(view.slug);
  if (i < 0 || i + dir < 0 || i + dir >= list.length) return;
  await saveChOrder(moved(list, i, i + dir), view.story);
}

async function deleteChapter() {
  if (!window.confirm("Delete this chapter? Its markdown is moved to the trash folder.")) return;
  try { const s = view.story; await api("DELETE", "chapters/" + enc(s) + "/" + enc(view.slug)); setDirty(false); await refresh(); await openStory(s); toast("Chapter deleted"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- menu ---------- */
const MENU_TPL = `
<div class="fill">
  <div class="bar"><strong>Menu</strong><span class="hint">Pages and modules appear here on their own. Untick one to hide it without deleting it.</span><span class="grow"></span><button id="m-save" class="primary">Save menu</button></div>
  <div id="m-list"></div>
  <div class="bar"><input id="m-label" placeholder="Link text"><input id="m-url" placeholder="https://example.com or about.html" style="flex:1"><button id="m-add">Add link</button></div>
</div>`;
let menuItems = [];

function openMenu() {
  view = { type: "menu" };
  menuItems = JSON.parse(JSON.stringify(S.nav));
  show(MENU_TPL);
  $("#m-save").addEventListener("click", saveMenu);
  $("#m-add").addEventListener("click", () => {
    const url = $("#m-url").value.trim();
    if (!url) { toast("Enter a link address.", "bad"); return; }
    menuItems.push({ type: "link", label: $("#m-label").value.trim() || url, url, visible: true });
    $("#m-label").value = ""; $("#m-url").value = "";
    renderMenu(); markDirty();
  });
  renderMenu();
  renderSide();
  renderPreview();
}

function menuName(n) {
  if (n.type === "page") { const p = page(n.slug); return p ? (p.slug === "index" ? "Home" : "Page: " + p.title) : n.slug; }
  if (n.type === "module") return label(n.id) + (on(n.id) ? "" : " (switched off)");
  return "Link: " + n.url;
}

function renderMenu() {
  const box = $("#m-list");
  box.innerHTML = "";
  menuItems.forEach((n, i) => {
    const vis = el("input", { type: "checkbox", title: "Show in the menu" });
    vis.checked = n.visible !== false;
    vis.addEventListener("change", () => { n.visible = vis.checked; markDirty(); });
    const lbl = el("input", { placeholder: n.type === "link" ? "Link text" : "Label (optional)" });
    lbl.value = n.label || "";
    lbl.addEventListener("input", () => { n.label = lbl.value; markDirty(); });
    const row = el("div", { class: "row" }, [vis, el("span", { class: "t", text: menuName(n) }), lbl]);
    const mk = (t, dis, fn) => { const b = el("button", { text: t }); b.disabled = dis; b.addEventListener("click", fn); row.appendChild(b); };
    mk("Up", i === 0, () => { menuItems = moved(menuItems, i, i - 1); renderMenu(); markDirty(); });
    mk("Down", i === menuItems.length - 1, () => { menuItems = moved(menuItems, i, i + 1); renderMenu(); markDirty(); });
    if (n.type === "link") mk("Remove", false, () => { menuItems.splice(i, 1); renderMenu(); markDirty(); });
    box.appendChild(row);
  });
  S.nav = menuItems;
}

async function saveMenu() {
  try { const r = await api("PUT", "nav", { nav: menuItems }); menuItems = r.nav; setDirty(false); await refresh(); renderMenu(); toast("Menu saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- css ---------- */
const CSS_TPL = `
<div class="fill">
  <div class="bar"><label>Theme <select id="theme"></select></label><button id="apply-theme">Apply theme</button><span class="grow"></span><button id="css-save" class="primary">Save CSS</button></div>
  <div class="bar sub"><label>Preview <select id="css-target"></select></label><span class="grow"></span><button id="jump-global">Go to theme</button><button id="jump-pages">Go to your rules</button></div>
  <textarea id="css" class="code" spellcheck="false" wrap="off"></textarea>
</div>`;

function themeSelect(select, value) {
  select.innerHTML = "";
  const groups = {};
  S.themes.forEach((t) => {
    if (!groups[t.group]) { groups[t.group] = el("optgroup", { label: t.group + " themes" }); select.appendChild(groups[t.group]); }
    groups[t.group].appendChild(el("option", { value: t.id, text: t.label }));
  });
  if (value) select.value = value;
}

async function openCss() {
  const d = await api("GET", "css");
  const t = await api("GET", "preview-targets");
  view = { type: "css" };
  show(CSS_TPL);
  themeSelect($("#theme"));
  opts($("#css-target"), t.targets.map((x) => [x.id, x.label]));
  $("#css").value = d.css;
  $("#css").addEventListener("input", markDirty);
  $("#css-target").addEventListener("change", renderPreview);
  $("#css-save").addEventListener("click", async () => {
    try { await api("PUT", "css", { css: $("#css").value }); setDirty(false); toast("Saved"); } catch (e) { toast(e.message, "bad"); }
  });
  $("#apply-theme").addEventListener("click", () => {
    const th = S.themes.find((x) => x.id === $("#theme").value);
    if (!window.confirm("Replace the theme variables at the top of your CSS with \"" + th.label + "\"?\n\nEdits inside that top :root block are overwritten. Everything else stays.")) return;
    const ta = $("#css"), re = /:root\s*\{[^}]*\}/;
    ta.value = re.test(ta.value) ? ta.value.replace(re, () => th.block) : th.block + "\n\n" + ta.value;
    markDirty();
  });
  const jump = (marker) => {
    const ta = $("#css"), i = ta.value.indexOf(marker);
    if (i < 0) { toast("That marker is not in your CSS any more.", "bad"); return; }
    ta.focus(); ta.setSelectionRange(i, i);
    ta.scrollTop = Math.max(0, (ta.value.slice(0, i).split("\n").length - 2) * (parseFloat(getComputedStyle(ta).lineHeight) || 20));
  };
  $("#jump-global").addEventListener("click", () => jump("GLOBAL THEME"));
  $("#jump-pages").addEventListener("click", () => jump("PAGE SPECIFIC RULES"));
  renderSide();
  renderPreview();
}

/* ---------- settings ---------- */
const SET_TPL = `
<div class="fill">
  <div class="bar"><strong>Settings</strong><span class="grow"></span><button id="st-save" class="primary">Save settings</button></div>
  <div class="pad">
    <fieldset><legend>Site</legend>
      <label class="field">Title <input id="st-title"></label>
      <label class="field">Tagline <input id="st-tagline"></label>
      <label class="field">Author <input id="st-author" placeholder="Used for bylines, meta tags and {author} in the footer"></label>
      <label class="field">Footer text <input id="st-footer"><span class="hint">You can use {year}, {title} and {author}. Inline markdown works.</span></label>
      <label class="field">Language code <input id="st-lang" style="max-width:100px"></label>
      <label class="field">Site URL <input id="st-url" placeholder="https://example.com"><span class="hint">Needed for feeds, canonical links, the sitemap and social tags.</span></label>
    </fieldset>
    <fieldset><legend>Search engines and feeds</legend>
      <label class="field">Meta description <textarea id="st-description" rows="2" maxlength="320" placeholder="Default description. Pages and posts can override it."></textarea></label>
      <label class="field">Default social image <input id="st-image" placeholder="assets/cover.jpg or a full URL"></label>
      <label class="field">Twitter / X handle <input id="st-twitter" style="max-width:220px"></label>
      <label><input type="checkbox" id="st-feeds"> Publish RSS feeds of new posts and chapters</label>
      <label><input type="checkbox" id="st-sitemap"> Generate sitemap.xml and robots.txt</label>
      <label><input type="checkbox" id="st-noindex"> Ask search engines not to index this site (for staging)</label>
    </fieldset>
    <fieldset data-need="blog"><legend>Blog</legend>
      <label><input type="checkbox" id="st-post_nav"> Older and newer links at the bottom of posts</label>
    </fieldset>
    <fieldset data-need="wiki"><legend>Wiki</legend>
      <label><input type="radio" name="st-search" value="builtin"> Built-in search. Instant, works offline, adds one small script (search.js).</label>
      <label><input type="radio" name="st-search" value="web"> Web search form. No JavaScript; searches your site with DuckDuckGo.</label>
      <label><input type="radio" name="st-search" value="off"> No search box. No JavaScript anywhere in the site.</label>
      <label><input type="checkbox" id="st-toc"> Table of contents on articles with three or more headings</label>
      <label><input type="checkbox" id="st-updated"> Show the last updated date on articles</label>
      <label><input type="checkbox" id="st-expand"> Keep every sidebar section open</label>
      <label><input type="checkbox" id="st-wiki_cards"> Section cards on the wiki home</label>
      <label><input type="checkbox" id="st-wiki_recent"> Recently updated articles on the wiki home</label>
    </fieldset>
    <fieldset data-need="library"><legend>Library</legend>
      <label><input type="radio" name="st-prose" value="indented"> Indented paragraphs, like a printed book</label>
      <label><input type="radio" name="st-prose" value="spaced"> Spaced paragraphs, like a web page</label>
      <label><input type="checkbox" id="st-dropcap"> Drop capital on the first paragraph of each chapter</label>
      <label><input type="checkbox" id="st-wordcount"> Show word counts</label>
      <label><input type="checkbox" id="st-reading_time"> Show estimated reading time</label>
      <label class="field">Reading speed (words per minute) <input type="number" id="st-wpm" min="60" max="1000" step="10" style="max-width:120px"></label>
      <label><input type="checkbox" id="st-lib_updates"> Latest chapters on the library page</label>
    </fieldset>
  </div>
</div>`;
const SET_TEXT = ["title", "tagline", "author", "footer", "lang", "url", "description", "image", "twitter", "wpm"];
const SET_BOOL = ["feeds", "sitemap", "noindex", "post_nav", "toc", "updated", "expand", "wiki_cards", "wiki_recent", "dropcap", "wordcount", "reading_time", "lib_updates"];

function readSettings() {
  const o = {};
  SET_TEXT.forEach((k) => { o[k] = $("#st-" + k).value; });
  SET_BOOL.forEach((k) => { o[k] = $("#st-" + k).checked; });
  o.wpm = parseInt(o.wpm, 10) || 240;
  o.search = ($("input[name=st-search]:checked") || { value: "builtin" }).value;
  o.prose = ($("input[name=st-prose]:checked") || { value: "indented" }).value;
  return o;
}

function openSettings() {
  view = { type: "settings" };
  show(SET_TPL);
  const s = S.site;
  SET_TEXT.forEach((k) => { $("#st-" + k).value = s[k] == null ? "" : s[k]; });
  SET_BOOL.forEach((k) => { $("#st-" + k).checked = !!s[k]; });
  [["search", s.search], ["prose", s.prose]].forEach(([n, v]) => { const r = $("input[name=st-" + n + "][value=" + v + "]"); if (r) r.checked = true; });
  $$("#left fieldset[data-need]").forEach((f) => { f.hidden = !on(f.dataset.need); });
  $$("#left input, #left textarea").forEach((i) => i.addEventListener(i.type === "checkbox" || i.type === "radio" ? "change" : "input", markDirty));
  $("#st-save").addEventListener("click", async () => {
    try { await api("PUT", "settings", readSettings()); setDirty(false); await refresh(); toast("Saved"); } catch (e) { toast(e.message, "bad"); }
  });
  renderSide();
  renderPreview();
}

/* ---------- site structure ---------- */
const STRUCT_TPL = `
<div class="fill">
  <div class="bar"><strong>Site structure</strong><span class="grow"></span><button id="x-save" class="primary">Save structure</button></div>
  <div class="pad">
    <label class="field">Homepage <select id="x-home"></select></label>
    <label id="x-rootrow"><input type="checkbox" id="x-root"> Serve it from the site root <span class="hint">(its sections or stories get addresses like /linux/portainer.html instead of /wiki/linux/portainer.html, which is what WikiGen and StoryGen produced)</span></label>
    <div id="x-mods"></div>
    <p class="hint">Switching a module off removes its pages from the built site but keeps everything you wrote, so switching it back on restores it. Changing the homepage or where a module is served from changes addresses, so links to the old ones from elsewhere will break.</p>
    <fieldset><legend>Import a SiteGen, WikiGen or StoryGen site</legend>
      <div style="display:flex;gap:6px"><input id="x-import" style="flex:1" placeholder="Folder that contains .sitegen, .wikigen or .storygen"><button id="x-browse">Browse</button><button id="x-check">Check</button></div>
      <span class="hint" id="x-found"></span>
      <label><input type="checkbox" id="x-settings"> Also copy its title, URL and other site settings</label>
      <button id="x-go" disabled>Import into this site</button>
    </fieldset>
  </div>
</div>`;

function openStructure() {
  view = { type: "structure" };
  show(STRUCT_TPL);
  const homeOpts = [["page", "A page"]].concat(["blog", "wiki", "library"].map((m) => [m, label(m) + " (" + ({ blog: "blog feed", wiki: "wiki home", library: "book list" }[m]) + ")"]));
  opts($("#x-home"), homeOpts, S.home);
  $("#x-root").checked = S.home !== "page" && S.home !== "blog" && S.modules[S.home].mount === "";
  const box = $("#x-mods");
  ["blog", "wiki", "library"].forEach((m) => {
    const cb = el("input", { type: "checkbox", id: "x-on-" + m });
    cb.checked = on(m);
    const name = el("input", { id: "x-label-" + m, placeholder: S.names[m] });
    name.value = S.modules[m].label;
    const counts = { blog: S.posts.length + " post(s)", wiki: S.wiki.sections.length + " section(s)", library: S.library.stories.length + " stor(ies)" }[m];
    const rm = el("button", { class: "danger", text: "Move its content to the trash" });
    rm.hidden = on(m) || counts.startsWith("0");
    rm.addEventListener("click", async () => {
      if (!window.confirm("Move all " + S.names[m] + " content (" + counts + ") to the trash folder?")) return;
      try { await api("POST", "modules/" + m + "/remove"); await refresh(); openStructure(); toast("Moved to the trash folder"); } catch (e) { toast(e.message, "bad"); }
    });
    box.appendChild(el("div", { class: "mod-card" }, [
      el("h4", {}, [cb, el("label", { for: "x-on-" + m, text: S.names[m] }), el("span", { class: "hint", text: counts })]),
      el("label", { class: "field" }, [el("span", { text: "Name in the menu and headings" }), name]), rm]));
  });
  const sync = () => {
    const h = $("#x-home").value;
    $("#x-rootrow").hidden = !(h === "wiki" || h === "library");
    if (h !== "page") $("#x-on-" + h).checked = true;
  };
  $("#x-home").addEventListener("change", () => { sync(); markDirty(); });
  $$("#left input").forEach((i) => i.addEventListener(i.type === "checkbox" ? "change" : "input", markDirty));
  sync();
  $("#x-save").addEventListener("click", saveStructure);
  $("#x-browse").addEventListener("click", async () => { const r = await api("POST", "pick-folder"); if (r.path) { $("#x-import").value = r.path; checkImport(); } });
  $("#x-check").addEventListener("click", checkImport);
  $("#x-go").addEventListener("click", runImport);
  renderSide();
}

async function checkImport() {
  $("#x-go").disabled = true;
  try {
    const r = await api("POST", "detect-import", { path: $("#x-import").value });
    $("#x-found").textContent = "Found a " + { sitegen: "SiteGen", wikigen: "WikiGen", storygen: "StoryGen" }[r.kind] + " site: " + r.title;
    $("#x-go").disabled = false;
  } catch (e) { $("#x-found").textContent = e.message; }
}

async function runImport() {
  if (!(await guard())) return;
  try {
    const r = await api("POST", "import", { path: $("#x-import").value, settings: $("#x-settings").checked });
    S = r.state; renderTop(); renderSide(); setDirty(false);
    toast(r.notes.join("\n"));
    openStructure();
  } catch (e) { toast(e.message, "bad"); }
}

async function saveStructure() {
  const mods = {};
  ["blog", "wiki", "library"].forEach((m) => { mods[m] = { enabled: $("#x-on-" + m).checked, label: $("#x-label-" + m).value }; });
  const home = $("#x-home").value;
  const off = ["blog", "wiki", "library"].filter((m) => on(m) && !mods[m].enabled);
  if (off.length && !window.confirm("Switch off " + off.map(label).join(" and ") + "? Its pages leave the built site; your content is kept.")) return;
  try {
    await api("PUT", "structure", { home, modules: mods, root_mount: $("#x-root").checked });
    setDirty(false);
    await refresh();
    openStructure();
    toast("Structure saved");
    schedulePreview();
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- publish panel ---------- */
const PUB_TPL = `
<div class="fill">
  <div class="bar">
    <strong>Publish to a server</strong>
    <span class="grow"></span>
    <button id="d-test">Test connection</button>
    <button id="d-dry">Dry run</button>
    <button id="d-save">Save settings</button>
    <button id="d-go" class="primary">Build and publish</button>
    <button id="d-stop" class="danger" hidden>Stop</button>
  </div>
  <div class="pad">
    <label class="field">Method
      <select id="d-method">
        <option value="rsync">rsync over SSH (fastest, only sends what changed)</option>
        <option value="sftp">SFTP (OpenSSH)</option>
        <option value="ftps">FTPS (FTP with TLS)</option>
        <option value="ftp">FTP (unencrypted)</option>
        <option value="git">git push</option>
        <option value="folder">Copy to a local folder or mount</option>
      </select>
    </label>
    <div class="two" id="d-hostrow">
      <label class="field">Host <input id="d-host" placeholder="vps.example.com"></label>
      <label class="field">Port <input id="d-port" placeholder="22"></label>
    </div>
    <div class="two" id="d-userrow">
      <label class="field">User <input id="d-user" placeholder="deploy"></label>
      <label class="field">Password <input type="password" id="d-pass" placeholder="Leave blank to use an SSH key"></label>
    </div>
    <label id="d-remember"><input type="checkbox" id="d-save_password"> Remember the password in <span class="mono" id="d-secret-path"></span> (plain text, readable by your user only)</label>
    <label class="field" id="d-pathrow">Path <input id="d-path" placeholder="/var/www/example.com"><span class="hint">The folder on the server that the web server serves. Its contents are replaced by your site.</span></label>
    <label class="field" id="d-keyrow">SSH key file <input id="d-key" placeholder="Optional, for example ~/.ssh/id_ed25519"></label>
    <div class="two" id="d-gitrow">
      <label class="field">Git remote URL <input id="d-remote" placeholder="git@github.com:you/site.git"></label>
      <label class="field">Branch <input id="d-branch" placeholder="main"></label>
    </div>
    <label id="d-mirrorrow"><input type="checkbox" id="d-mirror"> Remove files on the server that are no longer part of the site</label>
    <p class="hint" id="d-mirrorhint"></p>
    <label id="d-srcrow"><input type="checkbox" id="d-include_sources"> Include the hidden source folder</label>
    <label id="d-passiverow"><input type="checkbox" id="d-passive"> Passive mode (leave this on unless the server says otherwise)</label>
    <label id="d-tlsrow"><input type="checkbox" id="d-insecure_tls"> Trust a self-signed certificate (FTPS only, weaker)</label>
    <p class="hint" id="d-tools"></p>
    <p class="hint" id="d-note"></p>
  </div>
  <pre class="log" id="d-log" aria-live="polite" aria-label="Publish log"></pre>
</div>`;

let pubTimer = null, pubSeen = 0;
const DEPLOY_BOOLS = ["mirror", "include_sources", "save_password", "passive", "insecure_tls"];

function readDeploy() {
  const o = { method: $("#d-method").value };
  ["host", "port", "user", "path", "key", "remote", "branch"].forEach((k) => { o[k] = $("#d-" + k).value.trim(); });
  DEPLOY_BOOLS.forEach((k) => { o[k] = $("#d-" + k).checked; });
  return o;
}

function pubSync(info) {
  const m = $("#d-method").value;
  const ssh = m === "rsync" || m === "sftp";
  const ftp = m === "ftp" || m === "ftps";
  $("#d-hostrow").hidden = !(ssh || ftp);
  $("#d-userrow").hidden = m === "folder";
  $("#d-remember").hidden = m === "folder";
  $("#d-keyrow").hidden = !(ssh || m === "git");
  $("#d-gitrow").hidden = m !== "git";
  $("#d-pathrow").hidden = m === "git";
  $("#d-srcrow").hidden = m !== "git";
  $("#d-passiverow").hidden = !ftp;
  $("#d-tlsrow").hidden = m !== "ftps";
  $("#d-mirrorrow").hidden = m === "git";
  $("#d-mirrorhint").hidden = m === "git";
  $("#d-mirrorhint").textContent = m === "rsync"
    ? "rsync mirrors the folder: anything there that is not part of the site is deleted, apart from hidden files. The first publish to a new folder asks before doing that."
    : "Only files that an earlier publish sent are removed. Nothing else on the server is touched.";
  const t = info.tools || {};
  const notes = [];
  if (ssh && !t[m === "rsync" ? "rsync" : "sftp"]) notes.push((m === "rsync" ? "rsync" : "sftp") + " is not installed on this machine, so this method will not work yet.");
  if (ssh && !t.sshpass) notes.push("sshpass is not installed, so a password cannot be handed to ssh. Use an SSH key, or install sshpass.");
  if (m === "git" && !t.git) notes.push("git is not installed on this machine.");
  if (m === "ftp") notes.push("Plain FTP sends your password and files in the clear. Prefer FTPS or SFTP.");
  if (m === "rsync") notes.push("rsync only transfers what changed, so repeat publishes are quick.");
  if (m === "git") notes.push("For GitHub or GitLab pages, or a bare repo on your VPS with a post-receive hook that checks the files out into the web root.");
  $("#d-note").textContent = notes.join(" ");
  $("#d-tools").textContent = "On this machine: " + ["rsync", "ssh", "sftp", "sshpass", "git"]
    .map((x) => x + (t[x] ? " yes" : " no")).join(", ") + ". The password can also come from the "
    + info.env_var + " environment variable.";
}

function pubLog(lines, clear) {
  const el = $("#d-log");
  if (!el) return;
  if (clear) el.textContent = "";
  lines.forEach((l) => { el.textContent += l + "\n"; });
  el.scrollTop = el.scrollHeight;
}

async function pollJob() {
  try {
    const r = await api("GET", "publish/status?since=" + pubSeen);
    if (r.lines.length) { pubLog(r.lines); pubSeen = r.total; }
    if (r.running) { pubTimer = setTimeout(pollJob, 600); return; }
    pubTimer = null;
    pubBusy(false);
    if (r.ok === true) toast(r.what + " finished");
    else if (r.ok === false) toast(r.what + " failed. See the log.", "bad");
  } catch (e) { pubTimer = null; toast(e.message, "bad"); }
}

function pubBusy(on) {
  $$("#left .bar button").forEach((b) => { b.disabled = on; });
  const stop = $("#d-stop");
  if (stop) { stop.hidden = !on; stop.disabled = false; }
}

async function pubRun(kind, confirmed) {
  if (pubTimer) return;
  const body = readDeploy();
  body.password = $("#d-pass").value;
  body.dry = kind === "dry";
  body.test = kind === "test";
  if (confirmed) body.confirm_mirror = true;
  try {
    const r = await api("POST", "publish", body);
    if (r.needs_confirm) {
      if (window.confirm(r.message)) return pubRun(kind, true);
      return;
    }
    pubSeen = 0;
    pubLog([], true);
    pubBusy(true);
    setDirty(false);
    pollJob();
  } catch (e) {
    pubBusy(false);
    toast(e.message, "bad");
  }
}

async function pubStop() {
  try { $("#d-stop").disabled = true; await api("POST", "publish/cancel"); }
  catch (e) { toast(e.message, "bad"); }
}

async function saveDeploy() {
  try {
    const body = readDeploy();
    body.password = $("#d-pass").value;
    const r = await api("PUT", "deploy", body);
    setDirty(false);
    $("#d-pass").value = "";
    $("#d-pass").placeholder = r.has_password ? "Saved password in use. Type to change it." : "Leave blank to use an SSH key";
    toast("Publish settings saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function openPublish() {
  const info = await api("GET", "deploy");
  view = { type: "publish" };
  $("#left").innerHTML = PUB_TPL;
  const d = info.deploy;
  $("#d-method").value = d.method;
  ["host", "port", "user", "path", "key", "remote", "branch"].forEach((k) => { $("#d-" + k).value = d[k] || ""; });
  DEPLOY_BOOLS.forEach((k) => { $("#d-" + k).checked = !!d[k]; });
  $("#d-secret-path").textContent = info.secret_path;
  if (info.has_password) $("#d-pass").placeholder = "Saved password in use. Type to change it.";
  $("#d-method").addEventListener("change", () => { pubSync(info); markDirty(); });
  $$("#left input").forEach((i) => i.addEventListener(i.type === "checkbox" ? "change" : "input", markDirty));
  $("#d-save").addEventListener("click", saveDeploy);
  $("#d-go").addEventListener("click", () => pubRun("publish"));
  $("#d-dry").addEventListener("click", () => pubRun("dry"));
  $("#d-test").addEventListener("click", () => pubRun("test"));
  $("#d-stop").addEventListener("click", pubStop);
  pubSync(info);
  renderSide();
  if (info.running) { pubSeen = 0; pubBusy(true); pollJob(); }
}

/* ---------- top bar ---------- */
function themeLabel() { $("#b-theme").textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode"; }

async function act(name) {
  if (name === "theme") {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("siteforge-theme", next); } catch (e) { /* ignore */ }
    themeLabel();
    return;
  }
  if (name === "quit") {
    if (!(await guard())) return;
    await api("POST", "quit");
    document.body.innerHTML = "<p style='padding:40px;font:16px system-ui'>SiteForge has stopped. You can close this tab.</p>";
    return;
  }
  await go(async () => {
    const fns = {
      "new-page": newPage, "new-post": () => openPost(null), "new-article": () => openArticle(null, null, null),
      "new-story": newStory, "new-chapter": () => openChapter(null, null, null), structure: openStructure, menu: openMenu,
      css: openCss, settings: openSettings, publish: openPublish, switch: chooseFolder,
      rescan: async () => {
        const r = await api("POST", "rescan");
        await refresh();
        const bits = Object.entries(r).filter(([, v]) => v).map(([k, v]) => v + " " + k.replace(/_/g, " "));
        toast(bits.length ? bits.join(", ") : "No new files found. Site rebuilt.");
        schedulePreview();
      },
    };
    if (fns[name]) await fns[name]();
  });
}

/* ---------- folders, setup and startup ---------- */
async function openFolder(path, ok, setup) {
  const r = await api("POST", "open", { path, confirm: !!ok, setup });
  if (r.needs_confirm) { if (window.confirm(r.message)) return openFolder(path, true, setup); return; }
  if (r.needs_setup) { showSetup(r.folder); return; }
  S = r.state;
  await boot();
}

async function chooseFolder(inputEl) {
  const r = await api("POST", "pick-folder");
  let path = r.path;
  if (!r.available) path = window.prompt("Folder path for your site", (S && S.folder) || (inputEl && inputEl.value) || "");
  if (!path) return;
  if (inputEl) inputEl.value = path;
  await openFolder(path, false);
}

function showWelcome() {
  $("#workspace").hidden = true; $("#setup").hidden = true; $("#welcome").hidden = false;
  $("#w-path").value = S.last || S.suggest || "";
  $("#w-browse").onclick = () => chooseFolder($("#w-path")).catch((e) => toast(e.message, "bad"));
  $("#w-open").onclick = () => openFolder($("#w-path").value, false).catch((e) => toast(e.message, "bad"));
}

function showSetup(folder) {
  $("#workspace").hidden = true; $("#welcome").hidden = true; $("#setup").hidden = false;
  $("#u-folder").textContent = folder;
  $("#u-title").value = folder.split("/").filter(Boolean).pop().replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());
  const sel = $("#u-theme");
  if (!sel.options.length) {
    const groups = {};
    (S.themes || []).forEach((t) => {
      if (!groups[t.group]) { groups[t.group] = el("optgroup", { label: t.group + " themes" }); sel.appendChild(groups[t.group]); }
      groups[t.group].appendChild(el("option", { value: t.id, text: t.label }));
    });
  }
  const pickTheme = () => { sel.value = { page: "site-clean", blog: "site-clean", wiki: "wiki-classic", library: "book-paperback" }[$("input[name=u-home]:checked").value]; };
  $$("input[name=u-home]").forEach((r) => { r.onchange = pickTheme; });
  pickTheme();
  $("#u-detect").onclick = async () => {
    try {
      const r = await api("POST", "detect-import", { path: $("#u-import").value });
      $("#u-detected").textContent = "Found a " + { sitegen: "SiteGen", wikigen: "WikiGen", storygen: "StoryGen" }[r.kind] + " site: " + r.title;
      $("input[name=u-home][value=" + r.home + "]").checked = true;
      pickTheme();
      $("#u-rootrow").hidden = r.home === "page";
    } catch (e) { $("#u-detected").textContent = e.message; }
  };
  $("#u-import-browse").onclick = async () => { const r = await api("POST", "pick-folder"); if (r.path) { $("#u-import").value = r.path; $("#u-detect").onclick(); } };
  $("#u-cancel").onclick = () => { S = S || {}; showWelcome(); };
  $("#u-create").onclick = async () => {
    const home = $("input[name=u-home]:checked").value;
    const setup = { title: $("#u-title").value, home, theme: sel.value, modules: $$(".u-mod:checked").map((c) => c.value),
      import: $("#u-import").value.trim(), root_mount: !$("#u-rootrow").hidden && $("#u-root").checked };
    try { await openFolder(folder, true, setup); } catch (e) { toast(e.message, "bad"); }
  };
}

async function boot() {
  $("#welcome").hidden = true; $("#setup").hidden = true; $("#workspace").hidden = false;
  setDirty(false);
  view = { type: "none" };
  renderTop();
  renderSide();
  await openHome();
}

$$("#top [data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (view.type === "publish") return;
    const saveBtn = view.type === "post" ? $("#p-save") : $("#left .bar .primary");
    if (saveBtn && !saveBtn.disabled) saveBtn.click();
  }
});
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
themeLabel();

(async function init() {
  try {
    S = await api("GET", "state");
    if (S.folder) await boot();
    else if (S.pending) showSetup(S.pending);
    else showWelcome();
  } catch (e) { toast(e.message, "bad"); }
})();

</script>
</body>
</html>
'''


def main():
    ap = argparse.ArgumentParser(description="SiteForge: one site generator for pages, a blog, a wiki and a story library.")
    ap.add_argument("folder", nargs="?", help="site folder to open (created if it does not exist)")
    ap.add_argument("--port", type=int, default=8768, help="port for the local editor (default 8768)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab automatically")
    args = ap.parse_args()

    if args.folder:
        try:
            r = open_site(args.folder)
            if r.get("needs_confirm"):
                PENDING["folder"] = str(Path(args.folder).expanduser().resolve())
                print("That folder has files SiteForge did not create. Confirm in the editor to use it.")
            elif r.get("needs_setup"):
                print("New site: finish the setup in your browser.")
        except ApiError as e:
            print("Could not open folder:", e)
    else:
        last = last_folder()
        if last and (Path(last) / META_DIR / "config.json").exists():
            try:
                open_site(last)
            except Exception as e:  # noqa: BLE001
                print("Could not reopen the last site:", e)

    httpd = None
    for port in (args.port, 0):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("Could not start the local server.")
    httpd.daemon_threads = True
    SERVER["httpd"] = httpd
    SERVER["port"] = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % SERVER["port"]
    print("SiteForge is running at %s (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
