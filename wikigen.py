#!/usr/bin/env python3
"""
WikiGen: a local wiki-style static site generator with a markdown editor and live preview.

Run it:
    python3 wikigen.py                 opens the last wiki you used
    python3 wikigen.py ~/my-wiki       opens (or creates) a wiki in that folder
    python3 wikigen.py --help

It needs Python 3.8 or newer and nothing else (standard library only).
It starts a small web server on 127.0.0.1 and opens the editor in your browser.

The generated wiki is HTML and CSS. The sidebar sections are plain <details> dropdowns and the
mobile menu is a CSS toggle, so both work with JavaScript turned off. The one exception is the
built-in search, which is a single small script (search.js). Search can be switched to a
no-JavaScript web search form, or turned off, in Settings.

Folder layout (inside the wiki folder you choose):
    index.html                      generated home page
    <section>/index.html            generated section overview
    <section>/<article>.html        generated articles
    style.css                       your stylesheet (edited through "Edit CSS", never overwritten)
    search.js                       generated search index and script (built-in search only)
    assets/                         images you add from the editor
    .wikigen/                       your markdown sources and settings:
        config.json                 sections, article order, settings
        home.md                     the home page
        sections/<section>/_index.md          section introduction
        sections/<section>/<article>.md       one file per article

You can also write articles in any text editor: drop .md files into a folder under
.wikigen/sections/ and press "Rescan files". New folders become sections.

Do not hand-edit the generated .html files, they are rewritten on every save.
"""
from __future__ import annotations

import argparse
import copy
import datetime
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

APP = "WikiGen"
META_DIR = ".wikigen"
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.RLock()
CURRENT = {"site": None}
SERVER = {"httpd": None, "port": 0}
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "wikigen"
RESERVED_SECTIONS = {"index", "style", "assets", "search", "sitemap", "robots", "site", "api", "404"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_UPLOAD = 25 * 1024 * 1024
SEARCH_TEXT_LIMIT = 8000
CALLOUTS = {"NOTE": "Note", "TIP": "Tip", "IMPORTANT": "Important", "WARNING": "Warning", "CAUTION": "Caution"}


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
# ----------------------------------------------------------------------------

FONT_SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
FONT_SERIF = 'Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif'
FONT_MONO = 'ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, "DejaVu Sans Mono", monospace'

THEME_DEFAULTS = {
    "bg": "#f6f7f9",
    "surface": "#ffffff",
    "text": "#1f2328",
    "muted": "#5b6470",
    "accent": "#0b5fd6",
    "border": "#d8dde4",
    "header-bg": "#ffffff",
    "header-text": "#1f2328",
    "sidebar-bg": "#f0f2f5",
    "sidebar-text": "#1f2328",
    "sidebar-active": "#e1e7f0",
    "code-bg": "#f2f4f7",
    "font-body": FONT_SANS,
    "font-heading": FONT_SANS,
    "font-mono": FONT_MONO,
    "base-size": "16px",
    "line-height": "1.65",
    "content-width": "54rem",
    "sidebar-width": "17rem",
    "radius": "6px",
}
VAR_ORDER = list(THEME_DEFAULTS.keys())

THEMES = {
    "clean": ("Clean light", {}),
    "classic": ("Classic wiki", {
        "bg": "#f8f9fa", "surface": "#ffffff", "text": "#202122", "muted": "#54595d", "accent": "#3366cc",
        "border": "#c8ccd1", "header-bg": "#ffffff", "header-text": "#202122", "sidebar-bg": "#f8f9fa",
        "sidebar-text": "#202122", "sidebar-active": "#eaecf0", "code-bg": "#f8f9fa",
        "font-heading": FONT_SERIF, "radius": "2px",
    }),
    "slate": ("Slate dark", {
        "bg": "#0f141b", "surface": "#151b24", "text": "#dde3ea", "muted": "#95a1b1", "accent": "#7aa7ff",
        "border": "#263041", "header-bg": "#111720", "header-text": "#e8edf3", "sidebar-bg": "#111720",
        "sidebar-text": "#cfd7e2", "sidebar-active": "#1d2635", "code-bg": "#0b1017",
    }),
    "nord": ("Nord", {
        "bg": "#2e3440", "surface": "#3b4252", "text": "#eceff4", "muted": "#b0b8c8", "accent": "#88c0d0",
        "border": "#4c566a", "header-bg": "#2e3440", "header-text": "#eceff4", "sidebar-bg": "#353c4a",
        "sidebar-text": "#e5e9f0", "sidebar-active": "#434c5e", "code-bg": "#2e3440",
    }),
    "gruvbox": ("Gruvbox dark", {
        "bg": "#1d2021", "surface": "#282828", "text": "#ebdbb2", "muted": "#b0a391", "accent": "#fabd2f",
        "border": "#3c3836", "header-bg": "#1d2021", "header-text": "#ebdbb2", "sidebar-bg": "#202324",
        "sidebar-text": "#d5c4a1", "sidebar-active": "#3c3836", "code-bg": "#1d2021", "radius": "3px",
    }),
    "dracula": ("Dracula", {
        "bg": "#21222c", "surface": "#282a36", "text": "#f8f8f2", "muted": "#a4abcc", "accent": "#bd93f9",
        "border": "#3a3d4e", "header-bg": "#21222c", "header-text": "#f8f8f2", "sidebar-bg": "#242631",
        "sidebar-text": "#e6e6f0", "sidebar-active": "#363949", "code-bg": "#1e1f29",
    }),
    "solarized": ("Solarized light", {
        "bg": "#fdf6e3", "surface": "#fffbef", "text": "#3c4d54", "muted": "#687b82", "accent": "#1f6fb2",
        "border": "#e4dcc2", "header-bg": "#fdf6e3", "header-text": "#3c4d54", "sidebar-bg": "#f6efd9",
        "sidebar-text": "#3c4d54", "sidebar-active": "#ebe2c4", "code-bg": "#f5eed8",
    }),
    "paper": ("Paper", {
        "bg": "#ebe7dc", "surface": "#f8f5ec", "text": "#2b2a26", "muted": "#66635a", "accent": "#1d5c63",
        "border": "#d3cdbd", "header-bg": "#f8f5ec", "header-text": "#2b2a26", "sidebar-bg": "#efeadf",
        "sidebar-text": "#2b2a26", "sidebar-active": "#e1dbc9", "code-bg": "#efeadf",
        "font-body": FONT_SERIF, "font-heading": FONT_SERIF, "base-size": "17px", "line-height": "1.7",
    }),
    "terminal": ("Terminal", {
        "bg": "#050805", "surface": "#0a100a", "text": "#3dff7a", "muted": "#2fc862", "accent": "#b6ffcb",
        "border": "#145c2c", "header-bg": "#050805", "header-text": "#3dff7a", "sidebar-bg": "#070b07",
        "sidebar-text": "#3dff7a", "sidebar-active": "#0f2414", "code-bg": "#030503",
        "font-body": FONT_MONO, "font-heading": FONT_MONO, "base-size": "15px", "radius": "0px",
    }),
}


def root_block(theme_id):
    label, over = THEMES[theme_id]
    t = dict(THEME_DEFAULTS)
    t.update(over)
    lines = [":root {", "  /* Theme: %s. Change any value below. */" % label]
    for k in VAR_ORDER:
        lines.append("  --%s: %s;" % (k, t[k]))
    lines.append("}")
    return "\n".join(lines)


CSS_HEADER = """/*
  WikiGen stylesheet

  TOP:     global theme variables (colors, fonts, sizes). They apply to every page.
  MIDDLE:  base styles for the layout: top bar, sidebar, content, search, callouts.
  BOTTOM:  page specific rules. Every page has body classes, so a rule under one only
           affects those pages.

  WikiGen never overwrites this file. "Apply theme" in the editor only replaces the
  :root block at the top.
*/

/* ============ GLOBAL THEME (every page) ============ */
"""

BASE_CSS = """
/* ============ GLOBAL BASE STYLES (every page) ============ */
*, *::before, *::after { box-sizing: border-box; }
html { font-size: var(--base-size); -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-body); line-height: var(--line-height); }
a { color: var(--accent); text-underline-offset: 0.18em; }
a:hover { text-decoration-thickness: 2px; }
a:focus-visible, summary:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 1rem; top: 1rem; z-index: 20; padding: 0.5rem 0.75rem; background: var(--surface); color: var(--text); }

/* top bar */
.nav-toggle { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.topbar {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem 1rem; padding: 0.8rem 1.25rem;
  background: var(--header-bg); color: var(--header-text); border-bottom: 1px solid var(--border);
}
.site-title { font-family: var(--font-heading); font-weight: 700; font-size: 1.3rem; color: var(--header-text); text-decoration: none; }
.tagline { color: var(--muted); font-size: 0.9rem; }
.nav-button {
  display: none; cursor: pointer; user-select: none; font-size: 0.9rem; padding: 0.15rem 0.7rem;
  border: 1px solid var(--border); border-radius: var(--radius); align-self: center;
}
.nav-toggle:focus-visible + .topbar .nav-button { outline: 2px solid var(--accent); outline-offset: 2px; }

/* layout */
.layout { display: grid; grid-template-columns: var(--sidebar-width) minmax(0, 1fr); align-items: start; }
.sidebar {
  position: sticky; top: 0; max-height: 100vh; overflow-y: auto; padding: 1rem 0.75rem 2rem;
  background: var(--sidebar-bg); color: var(--sidebar-text); border-right: 1px solid var(--border);
  font-size: 0.93rem; min-height: calc(100vh - 3.5rem);
}
.sidebar ul { list-style: none; margin: 0; padding: 0; }
.sidebar a { display: block; padding: 0.25rem 0.6rem; border-radius: calc(var(--radius) - 1px); color: var(--sidebar-text); text-decoration: none; }
.sidebar a:hover { background: var(--sidebar-active); }
.sidebar a[aria-current="page"] { background: var(--sidebar-active); color: var(--accent); font-weight: 600; }
.nav-home { margin: 0 0 0.6rem; }
.nav-empty { color: var(--muted); padding: 0 0.6rem; font-size: 0.85rem; }
.nav-section { margin: 0.1rem 0; }
.nav-section > summary {
  display: flex; align-items: center; gap: 0.5rem; padding: 0.35rem 0.6rem; cursor: pointer;
  list-style: none; font-weight: 600; border-radius: calc(var(--radius) - 1px);
}
.nav-section > summary::-webkit-details-marker { display: none; }
.nav-section > summary::before {
  content: ""; flex: none; width: 0.42em; height: 0.42em; margin-right: 0.1rem; opacity: 0.7;
  border-right: 2px solid currentColor; border-bottom: 2px solid currentColor; transform: rotate(-45deg);
  transition: transform 0.15s;
}
.nav-section[open] > summary::before { transform: rotate(45deg); }
.nav-section > summary:hover { background: var(--sidebar-active); }
.nav-count { margin-left: auto; font-weight: 400; font-size: 0.78rem; color: var(--muted); }
.nav-section ul { margin: 0.1rem 0 0.5rem 0.95rem; padding-left: 0.4rem; border-left: 1px solid var(--border); }

/* search */
.search { position: relative; margin: 0 0 1rem; }
.search input {
  width: 100%; padding: 0.45rem 0.65rem; font: inherit; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius);
}
.search-results {
  list-style: none; margin: 0.35rem 0 0; padding: 0.25rem; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 6px 18px rgba(0, 0, 0, 0.14);
}
.sidebar .search-results a { padding: 0.4rem 0.5rem; }
.search-results a.active { background: var(--sidebar-active); }
.search-results .r-title { display: block; font-weight: 600; color: var(--text); }
.search-results .r-sec { display: block; font-size: 0.75rem; color: var(--muted); }
.search-results .r-snip { display: block; font-size: 0.8rem; color: var(--muted); }
.search-results mark { background: rgba(255, 200, 0, 0.35); color: inherit; border-radius: 2px; }
.search-empty { padding: 0.4rem 0.5rem; color: var(--muted); font-size: 0.85rem; }

/* content */
.main { min-width: 0; padding: 1.5rem clamp(1rem, 4vw, 3rem) 3rem; }
.content {
  max-width: var(--content-width); background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1.75rem clamp(1rem, 3vw, 2.5rem);
}
.content > :last-child { margin-bottom: 0; }
.breadcrumbs ol { display: flex; flex-wrap: wrap; gap: 0.35rem; list-style: none; margin: 0 0 1rem; padding: 0; font-size: 0.85rem; color: var(--muted); }
.breadcrumbs li + li::before { content: "/"; margin-right: 0.35rem; opacity: 0.6; }
.content h1, .content h2, .content h3, .content h4, .content h5, .content h6 { font-family: var(--font-heading); line-height: 1.25; }
.content h1 { font-size: 2rem; margin: 0 0 1rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--border); }
.content h2 { font-size: 1.45rem; margin: 2rem 0 0.75rem; padding-bottom: 0.25rem; border-bottom: 1px solid var(--border); }
.content h3 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
.content h4, .content h5, .content h6 { font-size: 1rem; margin: 1.25rem 0 0.5rem; }
.anchor { margin-left: 0.35rem; font-weight: 400; color: var(--muted); text-decoration: none; opacity: 0; }
.content h2:hover .anchor, .content h3:hover .anchor, .content h4:hover .anchor, .anchor:focus { opacity: 1; }
.lead { color: var(--muted); font-size: 1.05rem; margin-top: -0.25rem; }
.content p, .content ul, .content ol, .content dl { margin: 0 0 1rem; }
.content li + li { margin-top: 0.2rem; }
.content img, .content video, .content iframe { max-width: 100%; height: auto; border-radius: calc(var(--radius) / 2); }
.content blockquote { margin: 0 0 1rem; padding: 0.1rem 1rem; border-left: 3px solid var(--border); color: var(--muted); }
.content code { font-family: var(--font-mono); font-size: 0.88em; padding: 0.1em 0.35em; background: var(--code-bg); border: 1px solid var(--border); border-radius: 4px; }
.content pre {
  overflow-x: auto; margin: 0 0 1rem; padding: 0.9rem 1rem; line-height: 1.5; font-size: 0.9rem;
  background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius);
}
.content pre code { padding: 0; border: 0; background: none; font-size: inherit; }
.content kbd { font-family: var(--font-mono); font-size: 0.82em; padding: 0.05em 0.4em; border: 1px solid var(--border); border-bottom-width: 2px; border-radius: 4px; background: var(--code-bg); }
.table-wrap { overflow-x: auto; margin: 0 0 1rem; }
.content table { border-collapse: collapse; min-width: 100%; }
.content th, .content td { border: 1px solid var(--border); padding: 0.4rem 0.7rem; text-align: left; vertical-align: top; }
.content th { background: var(--code-bg); }
.content hr { border: 0; border-top: 1px solid var(--border); margin: 2rem 0; }
.task-item { list-style: none; margin-left: -1.25rem; }
.task-item input { margin-right: 0.5rem; }

/* table of contents */
.toc { margin: 0 0 1.5rem; padding: 0.6rem 1rem; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); font-size: 0.92rem; }
.toc summary { cursor: pointer; font-weight: 600; }
.toc ol { margin: 0.4rem 0 0.2rem; padding-left: 1.3rem; }
.toc ol ol { margin: 0.1rem 0; }

/* callouts: > [!NOTE], [!TIP], [!IMPORTANT], [!WARNING], [!CAUTION] */
.callout { --c: #2f6fdb; margin: 0 0 1rem; padding: 0.65rem 1rem; border-left: 4px solid var(--c); border-radius: 0 var(--radius) var(--radius) 0; background: var(--code-bg); }
.callout-tip { --c: #1f8a4c; }
.callout-important { --c: #8250df; }
.callout-warning { --c: #c27c0e; }
.callout-caution { --c: #d13b3b; }
.content .callout-title { margin: 0 0 0.3rem; font-weight: 700; color: var(--c); }
.callout > :last-child { margin-bottom: 0; }

/* wiki links */
.missing-link { color: #d33a3a; border-bottom: 1px dashed currentColor; cursor: help; }

/* article footer and pager */
.article-meta { margin: 2rem 0 0; padding-top: 0.75rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.85rem; }
.pager { display: flex; gap: 1rem; margin-top: 1.25rem; }
.pager a { flex: 1 1 0; padding: 0.6rem 0.85rem; border: 1px solid var(--border); border-radius: var(--radius); text-decoration: none; }
.pager a:hover { border-color: var(--accent); }
.pager .next { text-align: right; margin-left: auto; }
.pager small { display: block; color: var(--muted); font-size: 0.75rem; }

/* home and section listings */
.home-heading { margin-top: 2rem; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 1rem; margin: 0 0 1.5rem; }
.card { padding: 1rem 1.1rem; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg); }
.content .card h3 { margin: 0 0 0.35rem; font-size: 1.1rem; }
.card h3 a { text-decoration: none; }
.content .card p { margin: 0 0 0.5rem; color: var(--muted); font-size: 0.9rem; }
.content .card ul { margin: 0; padding-left: 1.1rem; font-size: 0.92rem; }
.card .more { display: inline-block; margin-top: 0.4rem; font-size: 0.85rem; }
.content .article-list { list-style: none; margin: 0.5rem 0 1rem; padding: 0; }
.article-list li { padding: 0.65rem 0; border-top: 1px solid var(--border); }
.content .article-list li + li { margin-top: 0; }
.article-list li:first-child { border-top: 0; }
.article-list a { font-weight: 600; }
.content .article-list p { margin: 0.15rem 0 0; color: var(--muted); font-size: 0.92rem; }
.article-list .when { display: block; color: var(--muted); font-size: 0.8rem; }
.empty { color: var(--muted); }

.site-footer { padding: 1rem 1.25rem 2rem; color: var(--muted); font-size: 0.85rem; border-top: 1px solid var(--border); }
.site-footer p { margin: 0; }

/* narrow screens: the sidebar folds away behind the Menu button (pure CSS, no script) */
@media (max-width: 820px) {
  .layout { display: block; }
  .nav-button { display: inline-block; }
  .sidebar { display: none; position: static; max-height: none; min-height: 0; border-right: 0; border-bottom: 1px solid var(--border); }
  .nav-toggle:checked ~ .layout .sidebar { display: block; }
  .main { padding: 1rem 0.75rem 2rem; }
}
@media print {
  .sidebar, .nav-button, .pager, .anchor { display: none !important; }
  .layout { display: block; }
  .content { border: 0; padding: 0; max-width: none; }
}
"""

PAGE_MARK = "/* ============ PAGE SPECIFIC RULES ============ */"
PAGE_HELP = """/*
  Home page:                 body.page-home
  Every section overview:    body.page-section
  Every article:             body.page-article
  Everything in one section: body.sec-your-section-slug
  One article:               body.art-your-section-slug-your-article-slug
  Example: body.sec-fun-stuff .content h1 { color: var(--accent); }
*/
"""


def css_stub(label, cls):
    return "\n/* --- %s (body.%s) --- */\nbody.%s {\n}\n" % (label.replace("*/", ""), cls, cls)


def default_css(theme="clean"):
    return (
        CSS_HEADER + root_block(theme) + "\n" + BASE_CSS + "\n" + PAGE_MARK + "\n" + PAGE_HELP
        + css_stub("Home page", "page-home") + css_stub("All articles", "page-article")
    )


# ----------------------------------------------------------------------------
# Seed content
# ----------------------------------------------------------------------------

HOME_MD = """# Welcome

This is the home page of your wiki. Edit it with **Home page** in the editor.

Pick a section from the sidebar, or use the search box to find a topic.
"""

HELP_SECTION_MD = """Notes on how this wiki works. Delete this section when you no longer need it.
"""

HELP_ARTICLE_MD = """Articles are written in Markdown. This page shows what the editor supports.
Delete it whenever you like.

## Text

**Bold**, *italic*, `inline code`, ~~strikethrough~~ and [links](https://example.com).
Keyboard keys: <kbd>Ctrl</kbd> + <kbd>C</kbd>.

## Steps and lists

1. Numbered steps
2. Keep counting on their own
   - and can hold nested bullets

- [x] Task lists
- [ ] with checkboxes

## Code

```bash
sudo zypper install flatpak
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
```

## Callouts

> [!NOTE]
> Useful information the reader should notice.

> [!TIP]
> A better or faster way to do something.

> [!WARNING]
> Something that can go wrong.

## Links between articles

Write `[[Article title]]` to link to another article by its title, or
`[[Section/Article]]` when two articles share a name. `[[Title|shown text]]` changes the
link text and `[[Title#heading]]` jumps to a heading. A link to an article that does not
exist yet shows in red, like this: [[An article nobody wrote]].

## Tables

| Command | What it does |
|---|---|
| `ls -la` | List files, including hidden ones |
| `df -h` | Show free disk space |
"""


# ----------------------------------------------------------------------------
# Wiki model
# ----------------------------------------------------------------------------

SITE_BOOLS = ("toc", "updated", "expand", "home_sections", "home_recent", "sitemap", "noindex")
SEARCH_MODES = ("builtin", "web", "off")


def default_cfg(title):
    return {
        "version": 1,
        "site": {
            "title": title or "My wiki",
            "tagline": "",
            "footer": "&copy; {year} {title}",
            "author": "",
            "lang": "en",
            "url": "",
            "description": "",
            "search": "builtin",
            "toc": True,
            "updated": True,
            "expand": False,
            "home_sections": True,
            "home_recent": True,
            "sitemap": False,
            "noindex": False,
        },
        "sections": [],
        "generated": [],
    }


def today():
    return datetime.date.today().isoformat()


def title_from_name(name):
    t = re.sub(r"[-_]+", " ", str(name)).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:1].upper() + t[1:]) if t else "Untitled"


def first_h1(text):
    for line in text.splitlines()[:30]:
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        return m.group(1).strip() if m else ""
    return ""


def strip_title_h1(text, title):
    """Drop a leading '# Title' line when it repeats the article title, since the page adds its own."""
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
    tmp = src.with_name(src.name + ".wg-rename")
    os.replace(src, tmp)
    os.replace(tmp, dst)


def plain_text(html_):
    t = re.sub(r'<a class="anchor"[^>]*>#</a>', "", html_)
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _unescape(t)).strip()


def published(sec):
    return [a for a in sec.get("articles", []) if not a.get("draft")]


def write_if_changed(path, text):
    if path.is_file() and read_text(path) == text:
        return
    write_text(path, text)


class Wiki:
    def __init__(self, root):
        self.root = root
        self.meta = root / META_DIR
        self.cfg = None

    # ---- paths
    def home_md(self):
        return self.meta / "home.md"

    def sec_dir(self, s):
        return self.meta / "sections" / s

    def sec_md(self, s):
        return self.sec_dir(s) / "_index.md"

    def art_md(self, s, a):
        return self.sec_dir(s) / (a + ".md")

    def css_path(self):
        return self.root / "style.css"

    # ---- load and save
    def load(self):
        f = self.meta / "config.json"
        if f.exists():
            try:
                self.cfg = json.loads(f.read_text(encoding="utf-8"))
            except ValueError as e:
                raise ApiError("The wiki settings file %s is not valid JSON (%s). Fix or remove it." % (f, e))
        else:
            self.cfg = default_cfg(title_from_name(self.root.name))
            write_text(self.home_md(), HOME_MD)
            write_text(self.sec_md("help"), HELP_SECTION_MD)
            write_text(self.art_md("help", "formatting-guide"), HELP_ARTICLE_MD)
            self.cfg["sections"].append({
                "slug": "help", "title": "Help", "description": "How this wiki works.",
                "articles": [{"slug": "formatting-guide", "title": "Formatting guide",
                              "description": "Markdown, callouts and wiki links.", "draft": False,
                              "updated": today()}],
            })
        (self.root / "assets").mkdir(parents=True, exist_ok=True)
        if not self.css_path().exists():
            write_text(self.css_path(), default_css("clean"))
        self.normalize()
        self.rescan()
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
        for k, v in d["site"].items():
            c["site"].setdefault(k, v)
        if c["site"].get("search") not in SEARCH_MODES:
            c["site"]["search"] = "builtin"
        if not isinstance(c.get("generated"), list):
            c["generated"] = []
        secs, seen = [], set()
        for s in c.get("sections") if isinstance(c.get("sections"), list) else []:
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
        c["sections"] = secs

    def state(self):
        return {
            "folder": str(self.root),
            "site": self.cfg["site"],
            "sections": self.cfg["sections"],
            "themes": [{"id": k, "label": v[0], "block": root_block(k)} for k, v in THEMES.items()],
        }

    # ---- lookups
    def find_section(self, slug, cfg=None):
        return next((s for s in (cfg or self.cfg)["sections"] if s["slug"] == slug), None)

    def get_section(self, slug):
        s = self.find_section(slug)
        if not s:
            raise ApiError("That section does not exist.", 404)
        return s

    def get_article(self, sslug, aslug):
        s = self.get_section(sslug)
        a = next((x for x in s["articles"] if x["slug"] == aslug), None)
        if not a:
            raise ApiError("That article does not exist.", 404)
        return s, a

    def unique_section_slug(self, base, exclude=None):
        base = slugify(base) or "section"
        taken = {s["slug"] for s in self.cfg["sections"] if s["slug"] != exclude} | RESERVED_SECTIONS
        folder = self.meta / "sections"
        slug, n = base, 2
        while slug in taken or (slug != exclude and (folder / slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def unique_article_slug(self, sec, base, exclude=None):
        base = slugify(base) or "article"
        taken = {a["slug"] for a in sec["articles"] if a["slug"] != exclude} | {"index", "_index"}
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.art_md(sec["slug"], slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    # ---- trash (deleted markdown is moved here, never destroyed)
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

    # ---- rescan: pick up .md files and folders added outside the editor
    def rescan(self):
        base = self.meta / "sections"
        base.mkdir(parents=True, exist_ok=True)
        result = {"sections_added": 0, "articles_added": 0, "articles_removed": 0}
        known = {s["slug"] for s in self.cfg["sections"]}
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith(".") or d.name in known:
                continue
            slug = self.unique_section_slug(d.name, exclude=slugify(d.name) if slugify(d.name) == d.name else None)
            if slug != d.name:
                if (base / slug).exists() and not (base / slug).samefile(d):
                    continue
                rename_path(d, base / slug)
            self.cfg["sections"].append({"slug": slug, "title": title_from_name(d.name), "description": "", "articles": []})
            known.add(slug)
            result["sections_added"] += 1

        for sec in self.cfg["sections"]:
            d = self.sec_dir(sec["slug"])
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
                orig_stem = f.stem
                slug = f.stem if slugify(f.stem) == f.stem and f.stem not in ("index", "_index") else None
                if slug is None:
                    slug = self.unique_article_slug(sec, f.stem)
                    target = self.art_md(sec["slug"], slug)
                    if target.exists() and not target.samefile(f):
                        continue
                    rename_path(f, target)
                    f = target
                art = {
                    "slug": slug, "title": first_h1(read_text(f)) or title_from_name(orig_stem),
                    "description": "", "draft": False, "updated": file_date(f),
                }
                sec["articles"].append(art)
                arts[slug] = art
                result["articles_added"] += 1
            keep = [a for a in sec["articles"] if self.art_md(sec["slug"], a["slug"]).is_file()]
            result["articles_removed"] += len(sec["articles"]) - len(keep)
            sec["articles"] = keep
        return result

    def api_rescan(self):
        r = self.rescan()
        self.save_cfg()
        self.build()
        return r

    # ---- rendering helpers
    def make_resolver(self, cfg):
        """Map wiki link targets to URLs. Section/Article paths win, then article titles and
        slugs, then section titles and slugs. Draft articles are not linkable."""
        idx = {}

        def norm(s):
            return re.sub(r"\s+", " ", str(s).strip().lower())

        arts = [(s, a) for s in cfg["sections"] for a in published(s)]
        for s, a in arts:
            u = "%s/%s.html" % (s["slug"], a["slug"])
            for key in (s["slug"] + "/" + a["slug"], norm(s["title"]) + "/" + norm(a["title"])):
                idx.setdefault(key, u)
        for s, a in arts:
            idx.setdefault(norm(a["title"]), "%s/%s.html" % (s["slug"], a["slug"]))
        for s, a in arts:
            idx.setdefault(a["slug"], "%s/%s.html" % (s["slug"], a["slug"]))
        for s in cfg["sections"]:
            idx.setdefault(norm(s["title"]), "%s/index.html" % s["slug"])
            idx.setdefault(s["slug"], "%s/index.html" % s["slug"])
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

    def search_box(self, cfg, prefix):
        site = cfg["site"]
        mode = site.get("search")
        if mode == "builtin":
            return (
                '<div class="search" data-root="%s">\n'
                '<input type="search" id="wiki-search" placeholder="Search the wiki" aria-label="Search the wiki" '
                'autocomplete="off" spellcheck="false">\n'
                '<ul class="search-results" id="wiki-results" hidden></ul>\n</div>' % attr(prefix)
            )
        if mode == "web" and site.get("url"):
            host = urlsplit(site["url"]).netloc
            return (
                '<form class="search" role="search" action="https://duckduckgo.com/" method="get">\n'
                '<input type="hidden" name="sites" value="%s">\n'
                '<input type="search" name="q" placeholder="Search the wiki" aria-label="Search the wiki">\n'
                "</form>" % attr(host)
            )
        return ""

    def sidebar(self, cfg, prefix, cur_sec=None, cur_art=None, home=False):
        site = cfg["site"]
        out = ['<nav class="sidebar" id="sidebar" aria-label="Wiki">', self.search_box(cfg, prefix)]
        out.append('<ul class="nav-home"><li><a href="%sindex.html"%s>Home</a></li></ul>'
                   % (prefix, ' aria-current="page"' if home else ""))
        if not cfg["sections"]:
            out.append('<p class="nav-empty">No sections yet.</p>')
        for s in cfg["sections"]:
            arts = published(s)
            is_cur = s["slug"] == cur_sec
            items = ['<li><a href="%s%s/index.html"%s>Overview</a></li>'
                     % (prefix, s["slug"], ' aria-current="page"' if is_cur and cur_art is None else "")]
            for a in arts:
                cur = ' aria-current="page"' if is_cur and a["slug"] == cur_art else ""
                items.append('<li><a href="%s%s/%s.html"%s>%s</a></li>' % (prefix, s["slug"], a["slug"], cur, esc(a["title"])))
            out.append(
                '<details class="nav-section"%s>\n<summary>%s<span class="nav-count">%d</span></summary>\n<ul>\n%s\n</ul>\n</details>'
                % (" open" if (site.get("expand") or is_cur) else "", esc(s["title"]), len(arts), "\n".join(items))
            )
        out.append("</nav>")
        return "\n".join(x for x in out if x)

    def doc(self, cfg, *, prefix, body_class, title, desc, rel, main_html, sidebar_html, css=None, base=None,
            og_type="website", when=""):
        site = cfg["site"]
        st = site["title"]
        full = st if not title or title == st else "%s | %s" % (title, st)
        desc = (desc or "").strip()
        canonical = abs_url(site.get("url"), rel)
        head = ['<meta charset="utf-8">']
        if base:
            head.append('<base href="%s">' % attr(base))
        head.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        head.append("<title>%s</title>" % esc(full))
        if desc:
            head.append('<meta name="description" content="%s">' % attr(desc))
        if site.get("author"):
            head.append('<meta name="author" content="%s">' % attr(site["author"]))
        if canonical:
            head.append('<link rel="canonical" href="%s">' % attr(canonical))
        if site.get("noindex"):
            head.append('<meta name="robots" content="noindex, follow">')
        head.append('<meta property="og:type" content="%s">' % og_type)
        head.append('<meta property="og:title" content="%s">' % attr(title or st))
        head.append('<meta property="og:site_name" content="%s">' % attr(st))
        if desc:
            head.append('<meta property="og:description" content="%s">' % attr(desc))
        if canonical:
            head.append('<meta property="og:url" content="%s">' % attr(canonical))
        if og_type == "article" and when:
            head.append('<meta property="article:modified_time" content="%s">' % attr(when))
        head.append('<meta name="twitter:card" content="summary">')
        if css is None:
            head.append('<link rel="stylesheet" href="%sstyle.css">' % prefix)
            if site.get("search") == "builtin":
                head.append('<script src="%ssearch.js" defer></script>' % prefix)
                head.append("<noscript><style>.search { display: none; }</style></noscript>")
        else:
            head.append("<style>\n%s\n</style>" % css.replace("</", "<\\/"))
        tagline = '\n<span class="tagline">%s</span>' % esc(site["tagline"]) if site.get("tagline") else ""
        foot = site.get("footer", "").replace("{year}", str(datetime.date.today().year)).replace("{title}", st)
        footer = ""
        if foot.strip():
            footer = '\n<footer class="site-footer"><p>%s</p></footer>' % Markdown(prefix).inline(foot)
        return (
            '<!DOCTYPE html>\n<html lang="%s">\n<head>\n%s\n</head>\n<body class="%s">\n'
            '<a class="skip" href="#content">Skip to content</a>\n'
            '<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-label="Show navigation">\n'
            '<header class="topbar">\n<label for="nav-toggle" class="nav-button">Menu</label>\n'
            '<a class="site-title" href="%sindex.html">%s</a>%s\n</header>\n'
            '<div class="layout">\n%s\n<main class="main" id="content">\n<div class="content">\n%s\n</div>\n</main>\n</div>%s\n'
            "</body>\n</html>\n"
            % (attr(site.get("lang") or "en"), "\n".join(head), attr(body_class), prefix, esc(st), tagline,
               sidebar_html, main_html, footer)
        )

    @staticmethod
    def breadcrumbs(prefix, trail):
        items = []
        for label, href in trail:
            if href:
                items.append('<li><a href="%s">%s</a></li>' % (attr(href), esc(label)))
            else:
                items.append('<li aria-current="page">%s</li>' % esc(label))
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
        out = ['<nav class="toc" aria-label="Contents"><details open><summary>Contents</summary><ol>']
        for (hid, text), subs in groups:
            sub = ""
            if subs:
                sub = "<ol>%s</ol>" % "".join('<li><a href="#%s">%s</a></li>' % (h, t) for h, t in subs)
            out.append('<li><a href="#%s">%s</a>%s</li>' % (hid, text, sub))
        out.append("</ol></details></nav>")
        return "".join(out)

    # ---- pages
    def render_home(self, cfg, markdown, resolver, css=None, base=None):
        site = cfg["site"]
        M = Markdown("", resolver)
        parts = ['<article class="home">', M.render(markdown)]
        secs = [s for s in cfg["sections"] if published(s)]
        if site.get("home_sections") and secs:
            cards = []
            for s in secs:
                arts = published(s)
                lis = "".join('<li><a href="%s/%s.html">%s</a></li>' % (s["slug"], a["slug"], esc(a["title"])) for a in arts[:6])
                more = ""
                if len(arts) > 6:
                    more = '<a class="more" href="%s/index.html">%d more</a>' % (s["slug"], len(arts) - 6)
                desc = '<p>%s</p>' % esc(s["description"]) if s.get("description") else ""
                cards.append('<div class="card"><h3><a href="%s/index.html">%s</a></h3>%s<ul>%s</ul>%s</div>'
                             % (s["slug"], esc(s["title"]), desc, lis, more))
            parts.append('<h2 class="home-heading">Sections</h2>\n<div class="card-grid">\n%s\n</div>' % "\n".join(cards))
        if site.get("home_recent"):
            recent = [(a, s) for s in cfg["sections"] for a in published(s)]
            recent.sort(key=lambda x: (x[0].get("updated", ""), x[0]["title"].lower()), reverse=True)
            if recent:
                lis = []
                for a, s in recent[:6]:
                    lis.append('<li><a href="%s/%s.html">%s</a><span class="when">%s, %s</span></li>'
                               % (s["slug"], a["slug"], esc(a["title"]), esc(s["title"]), esc(nice_date(a["updated"]))))
                parts.append('<h2 class="home-heading">Recently updated</h2>\n<ul class="article-list recent">\n%s\n</ul>' % "\n".join(lis))
        parts.append("</article>")
        desc = site.get("description") or site.get("tagline") or auto_summary(parts[1])
        html_ = self.doc(
            cfg, prefix="", body_class="wiki page-home", title="", desc=desc, rel="index.html",
            main_html="\n".join(parts), sidebar_html=self.sidebar(cfg, "", home=True), css=css, base=base,
        )
        return html_, M.missing

    def render_section(self, cfg, sec, markdown, resolver, css=None, base=None):
        prefix = "../"
        M = Markdown(prefix, resolver)
        intro = M.render(markdown)
        arts = published(sec)
        parts = [
            self.breadcrumbs(prefix, [("Home", prefix + "index.html"), (sec["title"], "")]),
            '<article>\n<h1>%s</h1>' % esc(sec["title"]),
        ]
        if sec.get("description"):
            parts.append('<p class="lead">%s</p>' % esc(sec["description"]))
        if intro:
            parts.append(intro)
        if arts:
            lis = []
            for a in arts:
                d = '<p>%s</p>' % esc(a["description"]) if a.get("description") else ""
                lis.append('<li><a href="%s.html">%s</a>%s</li>' % (a["slug"], esc(a["title"]), d))
            parts.append('<h2>Articles</h2>\n<ul class="article-list">\n%s\n</ul>' % "\n".join(lis))
        else:
            parts.append('<p class="empty">No articles in this section yet.</p>')
        parts.append("</article>")
        desc = sec.get("description") or auto_summary(intro) or "Articles in %s." % sec["title"]
        html_ = self.doc(
            cfg, prefix=prefix, body_class="wiki page-section sec-%s" % sec["slug"], title=sec["title"], desc=desc,
            rel="%s/index.html" % sec["slug"], main_html="\n".join(parts),
            sidebar_html=self.sidebar(cfg, prefix, sec["slug"]), css=css, base=base,
        )
        return html_, M.missing

    def render_article(self, cfg, sec, art, markdown, resolver, css=None, base=None):
        site = cfg["site"]
        prefix = "../"
        M = Markdown(prefix, resolver)
        body = M.render(strip_title_h1(markdown, art["title"]))
        parts = [
            self.breadcrumbs(prefix, [("Home", prefix + "index.html"), (sec["title"], "index.html"), (art["title"], "")]),
            '<article>\n<h1>%s</h1>' % esc(art["title"]),
        ]
        if site.get("toc"):
            parts.append(self.toc_html(M.headings))
        parts.append(body)
        if site.get("updated"):
            parts.append('<p class="article-meta">Last updated <time datetime="%s">%s</time> in <a href="index.html">%s</a></p>'
                         % (attr(art["updated"]), esc(nice_date(art["updated"])), esc(sec["title"])))
        arts = published(sec)
        slugs = [a["slug"] for a in arts]
        if art["slug"] in slugs:
            i = slugs.index(art["slug"])
            pager = []
            if i > 0:
                p = arts[i - 1]
                pager.append('<a class="prev" href="%s.html"><small>Previous</small>%s</a>' % (p["slug"], esc(p["title"])))
            if i + 1 < len(arts):
                n = arts[i + 1]
                pager.append('<a class="next" href="%s.html"><small>Next</small>%s</a>' % (n["slug"], esc(n["title"])))
            if pager:
                parts.append('<nav class="pager" aria-label="More in this section">%s</nav>' % "".join(pager))
        parts.append("</article>")
        desc = art.get("description") or auto_summary(body)
        html_ = self.doc(
            cfg, prefix=prefix, body_class="wiki page-article sec-%s art-%s-%s" % (sec["slug"], sec["slug"], art["slug"]),
            title=art["title"], desc=desc, rel="%s/%s.html" % (sec["slug"], art["slug"]), main_html="\n".join(parts),
            sidebar_html=self.sidebar(cfg, prefix, sec["slug"], art["slug"]), css=css, base=base,
            og_type="article", when=art.get("updated", ""),
        )
        return html_, M.missing, body

    # ---- build
    def search_js(self, entries):
        data = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
        return "/* generated by WikiGen: search index and script */\n" + SEARCH_JS.replace("__INDEX__", data)

    def build(self):
        c = self.cfg
        site = c["site"]
        resolver = self.make_resolver(c)
        files = {}
        files["index.html"], _ = self.render_home(c, read_text(self.home_md()), resolver)
        entries = []
        for s in c["sections"]:
            intro_md = read_text(self.sec_md(s["slug"]))
            html_, _ = self.render_section(c, s, intro_md, resolver)
            files["%s/index.html" % s["slug"]] = html_
            entries.append({"t": s["title"], "s": "Section", "u": "%s/index.html" % s["slug"],
                            "d": s.get("description", ""), "x": plain_text(Markdown("").render(intro_md))[:2000]})
            for a in published(s):
                html_, _, body = self.render_article(c, s, a, read_text(self.art_md(s["slug"], a["slug"])), resolver)
                files["%s/%s.html" % (s["slug"], a["slug"])] = html_
                entries.append({"t": a["title"], "s": s["title"], "u": "%s/%s.html" % (s["slug"], a["slug"]),
                                "d": a.get("description", ""), "x": plain_text(body)[:SEARCH_TEXT_LIMIT]})
        if site.get("search") == "builtin":
            files["search.js"] = self.search_js(entries)
        for rel, text in files.items():
            write_if_changed(self.root / rel, text)
        for rel in set(c.get("generated", [])) - set(files):
            self.remove_generated(rel)
        c["generated"] = sorted(files)
        self.write_seo_files()
        self.save_cfg()

    def remove_generated(self, rel):
        """Delete a file this app generated earlier and no longer produces. Paths are checked so a
        tampered config can never reach outside the wiki folder or into the markdown sources."""
        try:
            p = (self.root / str(rel)).resolve()
            parts = p.relative_to(self.root.resolve()).parts
        except (ValueError, OSError):
            return
        if not parts or parts[0].startswith(".") or parts[0] == "assets" or p.suffix not in (".html", ".js"):
            return
        if p.is_file():
            p.unlink()
        parent = p.parent
        if parent != self.root.resolve():
            try:
                parent.rmdir()
            except OSError:
                pass

    def sitemap_xml(self):
        c = self.cfg
        url = c["site"]["url"].rstrip("/")
        rows = ["<url><loc>%s/index.html</loc></url>" % _escape(url, quote=False)]
        for s in c["sections"]:
            rows.append("<url><loc>%s</loc></url>" % _escape("%s/%s/index.html" % (url, s["slug"]), quote=False))
            for a in published(s):
                rows.append("<url><loc>%s</loc><lastmod>%s</lastmod></url>"
                            % (_escape("%s/%s/%s.html" % (url, s["slug"], a["slug"]), quote=False), a["updated"]))
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- generated by WikiGen -->\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % "\n".join(rows))

    def write_seo_files(self):
        """sitemap.xml and robots.txt. Files WikiGen did not create are never touched."""
        site = self.cfg["site"]
        url = (site.get("url") or "").rstrip("/")
        sm, rb = self.root / "sitemap.xml", self.root / "robots.txt"

        def ours(path):
            return path.is_file() and "generated by WikiGen" in read_text(path)[:300]

        want = bool(site.get("sitemap")) and bool(url) and not site.get("noindex")
        if want and not sm.is_dir() and (not sm.exists() or ours(sm)):
            write_if_changed(sm, self.sitemap_xml())
        elif not want and ours(sm):
            sm.unlink()
        if url and site.get("noindex"):
            robots = "# generated by WikiGen\nUser-agent: *\nDisallow: /\n"
        elif want:
            robots = "# generated by WikiGen\nUser-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % url
        else:
            robots = None
        if robots and not rb.is_dir() and (not rb.exists() or ours(rb)):
            write_if_changed(rb, robots)
        elif robots is None and ours(rb):
            rb.unlink()

    # ---- css
    def add_css_stub(self, label, cls):
        css = read_text(self.css_path())
        if ("body.%s " % cls) not in css and ("body.%s{" % cls) not in css:
            write_text(self.css_path(), css.rstrip("\n") + "\n" + css_stub(label, cls))

    def get_css(self):
        return {"css": read_text(self.css_path())}

    def save_css(self, css):
        write_text(self.css_path(), str(css))
        return {"ok": True}

    # ---- home
    def get_home(self):
        return {"markdown": read_text(self.home_md())}

    def save_home(self, d):
        write_text(self.home_md(), str(d.get("markdown") or ""))
        self.build()
        return {"ok": True}

    # ---- sections
    def add_section(self, title):
        title = str(title or "").strip()[:120]
        if not title:
            raise ApiError("Give the section a name.")
        slug = self.unique_section_slug(title)
        sec = {"slug": slug, "title": title, "description": "", "articles": []}
        self.cfg["sections"].append(sec)
        write_text(self.sec_md(slug), "")
        self.add_css_stub("Section: " + title, "sec-" + slug)
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_section_api(self, slug):
        s = self.get_section(slug)
        return {"section": s, "markdown": read_text(self.sec_md(slug))}

    def save_section(self, slug, d):
        s = self.get_section(slug)
        s["title"] = str(d.get("title") or "").strip()[:120] or s["title"]
        s["description"] = str(d.get("description") or "").strip()[:320]
        wanted = slugify(str(d.get("new_slug") or "")) or s["slug"]
        if wanted != s["slug"]:
            if wanted in RESERVED_SECTIONS:
                raise ApiError("The address %s is reserved. Pick another." % wanted)
            if self.find_section(wanted) or self.sec_dir(wanted).exists():
                raise ApiError("Another section already uses the address %s." % wanted)
            rename_path(self.sec_dir(s["slug"]), self.sec_dir(wanted))
            s["slug"] = wanted
        write_text(self.sec_md(s["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": s["slug"]}

    def delete_section(self, slug):
        s = self.get_section(slug)
        self.cfg["sections"].remove(s)
        self.to_trash(self.sec_dir(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def move_section(self, slug, direction):
        secs = self.cfg["sections"]
        s = self.get_section(slug)
        i = secs.index(s)
        j = max(0, min(len(secs) - 1, i + (1 if int(direction) > 0 else -1)))
        secs.insert(j, secs.pop(i))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def order_articles(self, slug, order):
        s = self.get_section(slug)
        by = {a["slug"]: a for a in s["articles"]}
        new = [by.pop(x) for x in (order if isinstance(order, list) else []) if x in by]
        s["articles"] = new + list(by.values())
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- articles
    def get_article_api(self, sslug, aslug):
        s, a = self.get_article(sslug, aslug)
        return {"section": s["slug"], "article": a, "markdown": read_text(self.art_md(s["slug"], a["slug"]))}

    def save_article(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the article a title.")
        target = self.find_section(str(d.get("section") or ""))
        if not target:
            raise ApiError("Pick a section for this article.")
        md = str(d.get("markdown") or "")
        desc = str(d.get("description") or "").strip()[:320]
        wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "article"
        orig_sec, orig_slug = str(d.get("orig_section") or ""), str(d.get("orig_slug") or "")
        if orig_slug:
            osec, art = self.get_article(orig_sec, orig_slug)
            old_path = self.art_md(osec["slug"], art["slug"])
            changed = read_text(old_path) != md or art["title"] != title
            same = osec is target
            if not same or wanted != art["slug"]:
                new_slug = self.unique_article_slug(target, wanted, exclude=art["slug"] if same else None)
                if not same:
                    osec["articles"].remove(art)
                    target["articles"].append(art)
                art["slug"] = new_slug
            new_path = self.art_md(target["slug"], art["slug"])
            write_text(new_path, md)
            if old_path != new_path:
                old_path.unlink(missing_ok=True)
        else:
            art = {"slug": self.unique_article_slug(target, wanted), "draft": False}
            target["articles"].append(art)
            changed = True
            write_text(self.art_md(target["slug"], art["slug"]), md)
        art.update(title=title, description=desc, draft=bool(d.get("draft")))
        if changed or not art.get("updated"):
            art["updated"] = today()
        self.save_cfg()
        self.build()
        return {"section": target["slug"], "slug": art["slug"], "article": art}

    def delete_article(self, sslug, aslug):
        s, a = self.get_article(sslug, aslug)
        s["articles"].remove(a)
        self.to_trash(self.art_md(s["slug"], a["slug"]))
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- settings
    @staticmethod
    def clean_site(d, current):
        out = dict(current)
        for k, limit in (("title", 120), ("tagline", 200), ("footer", 400), ("author", 120), ("lang", 12),
                         ("description", 320)):
            if k in d:
                out[k] = str(d[k]).strip()[:limit]
        if "url" in d:
            out["url"] = str(d["url"]).strip().rstrip("/")[:300]
        if d.get("search") in SEARCH_MODES:
            out["search"] = d["search"]
        for k in SITE_BOOLS:
            if k in d:
                out[k] = bool(d[k])
        out["title"] = out.get("title") or current.get("title") or "My wiki"
        out["lang"] = out.get("lang") or "en"
        return out

    def save_settings(self, d):
        site = self.clean_site(d, self.cfg["site"])
        url = site.get("url", "")
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise ApiError("The wiki URL must start with http:// or https://, for example https://wiki.example.com")
        if site.get("search") == "web" and not url:
            raise ApiError("Web search needs the wiki URL, so the search engine knows which site to look in.")
        if site.get("sitemap") and not url:
            raise ApiError("Enter the wiki URL to generate a sitemap. It needs full web addresses.")
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

    # ---- preview
    def preview(self, p):
        cfg = copy.deepcopy(self.cfg)
        if isinstance(p.get("site"), dict):
            cfg["site"] = self.clean_site(p["site"], cfg["site"])
        css = p.get("css") if isinstance(p.get("css"), str) else read_text(self.css_path())
        view = p.get("view")
        md = str(p.get("markdown") or "")

        if view == "home":
            html_, missing = self.render_home(cfg, md, self.make_resolver(cfg), css=css, base="/site/")
            return {"html": html_, "missing": missing}

        if view == "section":
            sec = self.find_section(str(p.get("slug") or ""), cfg)
            if not sec:
                raise ApiError("That section does not exist.", 404)
            sec["title"] = str(p.get("title") or "").strip() or sec["title"]
            sec["description"] = str(p.get("description") or "").strip()
            html_, missing = self.render_section(cfg, sec, md, self.make_resolver(cfg), css=css,
                                                 base="/site/%s/" % sec["slug"])
            return {"html": html_, "missing": missing}

        if view == "article":
            target = self.find_section(str(p.get("section") or ""), cfg)
            if not target:
                raise ApiError("Create a section first. Articles live inside sections.", 409)
            osec = self.find_section(str(p.get("orig_section") or ""), cfg)
            orig = None
            pos = None
            if osec:
                orig = next((a for a in osec["articles"] if a["slug"] == str(p.get("orig_slug") or "")), None)
                if orig:
                    pos = osec["articles"].index(orig) if osec is target else None
                    osec["articles"].remove(orig)
            art = dict(orig or {})
            art.update(
                slug=slugify(str(p.get("new_slug") or "")) or slugify(str(p.get("title") or "")) or "article",
                title=str(p.get("title") or "").strip() or "Untitled article",
                description=str(p.get("description") or "").strip(),
                draft=False,
                updated=art.get("updated") or today(),
            )
            if pos is None:
                target["articles"].append(art)
            else:
                target["articles"].insert(pos, art)
            html_, missing, _ = self.render_article(cfg, target, art, md, self.make_resolver(cfg), css=css,
                                                    base="/site/%s/" % target["slug"])
            return {"html": html_, "missing": missing}

        # css and settings views preview an existing page
        target = str(p.get("target") or "home")
        kind, _, rest = target.partition(":")
        resolver = self.make_resolver(cfg)
        if kind == "section":
            sec = self.find_section(rest, cfg)
            if sec:
                html_, _ = self.render_section(cfg, sec, read_text(self.sec_md(sec["slug"])), resolver, css=css,
                                               base="/site/%s/" % sec["slug"])
                return {"html": html_, "missing": []}
        if kind == "article":
            sslug, _, aslug = rest.partition("/")
            sec = self.find_section(sslug, cfg)
            art = next((a for a in sec["articles"] if a["slug"] == aslug), None) if sec else None
            if art:
                html_, _, _ = self.render_article(cfg, sec, art, read_text(self.art_md(sslug, aslug)), resolver,
                                                  css=css, base="/site/%s/" % sslug)
                return {"html": html_, "missing": []}
        html_, _ = self.render_home(cfg, read_text(self.home_md()), resolver, css=css, base="/site/")
        return {"html": html_, "missing": []}


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
# Opening wikis, remembering the last one
# ----------------------------------------------------------------------------

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


def open_site(path, confirm=False):
    path = str(path or "").strip()
    if not path:
        raise ApiError("Enter a folder path.")
    root = Path(path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ApiError("That path is a file, not a folder.")
    is_wiki = (root / META_DIR / "config.json").exists()
    if not is_wiki and root.exists() and not confirm:
        if any(not n.name.startswith(".") for n in root.iterdir()):
            return {
                "needs_confirm": True,
                "message": (
                    "This folder already has files that WikiGen did not create.\n\n"
                    "WikiGen will add its own files here and will overwrite index.html and any page "
                    "it generates with the same name. Existing style.css and images are kept.\n\n"
                    "Use this folder anyway?"
                ),
            }
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ApiError("Could not create that folder: %s" % e)
    site = Wiki(root)
    site.load()
    CURRENT["site"] = site
    remember_folder(root)
    return {"state": site.state()}


def app_state():
    site = CURRENT["site"]
    if site is None:
        return {"folder": None, "suggest": str(Path.home() / "my-wiki"), "last": last_folder()}
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
# HTTP layer
# ----------------------------------------------------------------------------

def api(method, route, query, h):
    q = parse_qs(query)
    parts = [unquote(x) for x in route.strip("/").split("/")]

    if parts == ["state"] and method == "GET":
        return app_state()
    if parts == ["open"] and method == "POST":
        d = h.read_json()
        return open_site(d.get("path", ""), bool(d.get("confirm")))

    w = CURRENT["site"]
    if w is None:
        raise ApiError("Open a wiki folder first.", 409)

    if parts == ["home"]:
        if method == "GET":
            return w.get_home()
        if method == "PUT":
            return w.save_home(h.read_json())
    if parts == ["sections"] and method == "POST":
        return w.add_section(h.read_json().get("title", ""))
    if len(parts) == 2 and parts[0] == "sections":
        if method == "GET":
            return w.get_section_api(parts[1])
        if method == "PUT":
            return w.save_section(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_section(parts[1])
    if len(parts) == 3 and parts[0] == "sections" and method == "POST":
        if parts[2] == "move":
            return w.move_section(parts[1], h.read_json().get("dir", 1))
        if parts[2] == "order":
            return w.order_articles(parts[1], h.read_json().get("articles", []))
    if parts == ["articles"] and method == "POST":
        return w.save_article(h.read_json())
    if len(parts) == 3 and parts[0] == "articles":
        if method == "GET":
            return w.get_article_api(parts[1], parts[2])
        if method == "DELETE":
            return w.delete_article(parts[1], parts[2])
    if parts == ["css"]:
        if method == "GET":
            return w.get_css()
        if method == "PUT":
            return w.save_css(h.read_json().get("css", ""))
    if parts == ["settings"] and method == "PUT":
        return w.save_settings(h.read_json())
    if parts == ["preview"] and method == "POST":
        return w.preview(h.read_json())
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
            return self.send_text(404, "No wiki is open.")
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
<title>WikiGen</title>
<style>
:root {
  color-scheme: light;
  --ink: #1a2130; --ink2: #252e42; --paper: #eef1f6; --panel: #ffffff; --panel2: #f7f9fc; --field: #ffffff;
  --line: #d3d9e3; --text: #1c2432; --muted: #5b6678; --accent: #2c5fe6;
  --primary: #2c5fe6; --primary-d: #2249b8;
  --btn-bg: #ffffff; --btn-hover: #f7f9fc; --btn-line-hover: #aab3c2;
  --side: #e5e9f0; --side-hover: #d9dfe9; --side-active: #ffffff;
  --right: #dde2ea; --right-bar: #eef1f6;
  --danger: #b3261e; --amber: #8a5a00; --amber-bg: #fff8e6; --amber-line: #e2c98f;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #0b0f19; --ink2: #1a2233; --paper: #0f1420; --panel: #161c2a; --panel2: #1b2233; --field: #0f1522;
  --line: #2c3548; --text: #e2e8f4; --muted: #98a3b8; --accent: #7ea2ff;
  --primary: #3d68e6; --primary-d: #5079ee;
  --btn-bg: #1f2740; --btn-hover: #263050; --btn-line-hover: #4a5878;
  --side: #121826; --side-hover: #1d2536; --side-active: #1f2940;
  --right: #0c111b; --right-bar: #131a28;
  --danger: #ff8a80; --amber: #f0c060; --amber-bg: #2b2412; --amber-line: #5a4a1e;
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
input[type=text], input:not([type]), input[type=url], select, textarea.small {
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); min-width: 0;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.grow { flex: 1; }
.hint { color: var(--muted); font-size: 12px; }

#workspace { display: flex; flex-direction: column; height: 100vh; }
#top { background: var(--ink); border-bottom: 1px solid var(--ink2); color: #fff; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 12px; }
#top button { padding: 4px 9px; background: transparent; border-color: #3b465e; color: #e8ecf5; }
#top button:hover { background: var(--ink2); border-color: #5a6785; }
#top .brand { font-weight: 700; margin-right: 6px; }
#top .sep { width: 1px; height: 22px; background: #3b465e; margin: 0 6px; }
#folder { color: #9aa6bf; font: 12px ui-monospace, Consolas, monospace; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#dirty { color: #ffd479; font-size: 12px; }

#main { flex: 1; min-height: 0; display: grid; grid-template-columns: 240px minmax(0, 1fr) minmax(0, 1fr); }
#side { background: var(--side); border-right: 1px solid var(--line); overflow: auto; padding: 8px; }
#side h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 14px 6px 4px; }
.item { display: flex; justify-content: space-between; align-items: center; gap: 6px; width: 100%; text-align: left; border: 0; background: transparent; padding: 5px 8px; border-radius: 6px; }
.item:hover { background: var(--side-hover); }
.item.active { background: var(--side-active); box-shadow: inset 3px 0 0 var(--accent); }
.item .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item.sec { font-weight: 600; margin-top: 6px; }
.item.art { padding-left: 22px; }
.item.add { padding-left: 22px; color: var(--muted); font-size: 12px; }
.chip { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); white-space: nowrap; color: var(--muted); }
.chip.draft { color: var(--amber); border-color: var(--amber-line); background: var(--amber-bg); }
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
.md-tools { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--line); }
.md-tools button { padding: 2px 8px; font-size: 13px; }
.code { flex: 1; width: 100%; min-height: 240px; border: 0; resize: none; padding: 14px; background: var(--panel); color: var(--text);
  font: 14px/20px ui-monospace, "SF Mono", Consolas, "DejaVu Sans Mono", monospace; tab-size: 2; }
.slug-wrap { display: inline-flex; align-items: center; gap: 2px; color: var(--muted); }
.slug-wrap input { width: 170px; }
.pad { padding: 14px; display: flex; flex-direction: column; gap: 12px; max-width: 600px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field input, .field select, .field textarea { width: 100%; }
.field textarea { border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); resize: vertical; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
legend { font-weight: 600; padding: 0 4px; }
details.box { border-bottom: 1px solid var(--line); }
details.box > summary { cursor: pointer; padding: 8px 10px; font-weight: 600; background: var(--panel2); }
.art-row { display: flex; align-items: center; gap: 6px; padding: 5px 10px; border-top: 1px solid var(--line); }
.art-row .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.art-row button { padding: 1px 8px; }

#right { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--right); }
#right .bar { background: var(--right-bar); }
#right a { color: var(--accent); }
#pv-wrap { flex: 1; position: relative; overflow: hidden; }
#preview { position: absolute; top: 0; left: 0; border: 0; background: #fff; transform-origin: 0 0; }
#pv-size button { padding: 2px 8px; font-size: 12px; }
#pv-size button.on { background: var(--primary); border-color: var(--primary); color: #fff; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: #fff; padding: 8px 16px; border-radius: 8px; opacity: 0; pointer-events: none; transition: opacity 0.15s; max-width: 80vw; }
#toast.show { opacity: 1; }
#toast.bad { background: #b3261e; }

.log { flex: 1; min-height: 160px; margin: 0; padding: 10px 12px; overflow: auto; white-space: pre-wrap;
  background: var(--field); border-top: 1px solid var(--line); font: 12px/1.5 ui-monospace, Consolas, monospace; }
.log .bad { color: var(--danger); }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); margin-right: 5px; }
.dot.on { background: var(--ok, #1c7a45); }
.tools { display: flex; flex-wrap: wrap; gap: 4px 14px; }
#welcome { max-width: 540px; margin: 12vh auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 28px; }
#welcome h1 { margin: 0 0 6px; font-size: 22px; }
#welcome p { color: var(--muted); margin: 0 0 18px; }
#welcome .row { display: flex; gap: 8px; margin-bottom: 12px; }
#welcome .row input { flex: 1; }
</style>
<script>
(function () {
  var t = null;
  try { t = localStorage.getItem("wikigen-theme"); } catch (e) { t = null; }
  if (t !== "dark" && t !== "light") t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
</head>
<body>

<div id="welcome" hidden>
  <h1>WikiGen</h1>
  <p>Choose the folder where your wiki lives. If the folder is new or empty, a fresh wiki is created there. If it holds a WikiGen wiki, it opens as you left it.</p>
  <div class="row">
    <input id="w-path" aria-label="Wiki folder path" placeholder="/home/you/my-wiki">
    <button id="w-browse">Browse</button>
  </div>
  <button id="w-open" class="primary">Open wiki</button>
</div>

<div id="workspace" hidden>
  <div id="top">
    <span class="brand">WikiGen</span>
    <button data-act="new-section">New section</button>
    <button data-act="new-article">New article</button>
    <button data-act="home">Home page</button>
    <span class="sep"></span>
    <button data-act="css">Edit CSS</button>
    <button data-act="settings">Settings</button>
    <button data-act="rescan" title="Pick up .md files and folders added under .wikigen/sections">Rescan files</button>
    <span class="grow"></span>
    <span id="dirty" hidden>Unsaved changes</span>
    <span id="folder" title=""></span>
    <button data-act="theme" id="b-theme">Dark mode</button>
    <button data-act="publish">Publish</button>
    <button data-act="switch">Switch folder</button>
    <button data-act="quit">Quit</button>
  </div>
  <div id="main">
    <aside id="side" aria-label="Sections and articles"></aside>
    <section id="left" aria-label="Editor"></section>
    <section id="right" aria-label="Preview">
      <div class="bar"><span class="hint">Live preview. Search runs on the built wiki only.</span><span id="pv-size" role="group" aria-label="Preview width"><button data-pv="desktop">Desktop</button> <button data-pv="mobile">Mobile</button> <button data-pv="fit">Fit</button></span><span class="grow"></span><a id="open-built" href="/site/index.html" target="_blank" rel="noopener">Open built wiki in a tab</a></div>
      <div id="pv-wrap"><iframe id="preview" title="Wiki preview" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe></div>
    </section>
  </div>
</div>
<div id="toast" role="status"></div>

<script>
const TOKEN = "__TOKEN__";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

let S = null;
let view = { type: "none" };
let dirty = false;
let timer = null;
let slugTouched = false;

const MD_TOOLS = `
<div class="md-tools">
  <button data-md="bold" title="Bold"><b>B</b></button>
  <button data-md="italic" title="Italic"><i>I</i></button>
  <button data-md="heading">Heading</button>
  <button data-md="link">Link</button>
  <button data-md="wiki" title="Link to another article by title">Wiki link</button>
  <button data-md="image">Image</button>
  <button data-md="code">Code</button>
  <button data-md="block">Code block</button>
  <button data-md="list">List</button>
  <button data-md="steps">Steps</button>
  <button data-md="callout">Callout</button>
  <button data-md="table">Table</button>
  <input type="file" id="img-file" accept="image/*" hidden>
</div>`;

const HOME_TPL = `
<div class="fill">
  <div class="bar"><strong>Home page</strong><span class="hint">Section cards and recent changes are added below this text (see Settings).</span><span class="grow"></span><button id="h-save" class="primary">Save</button></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the home page in Markdown"></textarea>
</div>`;

const SEC_TPL = `
<div class="fill">
  <div class="bar">
    <input id="s-title" class="title-input" placeholder="Section name" aria-label="Section name">
    <button id="s-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Address <span class="slug-wrap"><input id="s-slug" aria-label="Section address">/index.html</span></label>
    <span class="grow"></span>
    <button id="s-up">Move up</button>
    <button id="s-down">Move down</button>
    <button id="s-delete" class="danger">Delete section</button>
  </div>
  <div class="bar sub">
    <label class="wide">Description <input id="s-desc" maxlength="320" placeholder="One line, shown under the section title and on the home page"></label>
  </div>
  <details class="box" open>
    <summary>Articles in this section, in sidebar order</summary>
    <div id="s-arts"></div>
    <div class="bar"><button id="s-new">New article here</button><button id="s-sort">Sort A to Z</button><span class="hint">Order changes save immediately.</span></div>
  </details>
  <div class="bar sub"><span class="hint">Introduction, shown above the list of articles on the section page</span></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Optional introduction in Markdown"></textarea>
</div>`;

const ART_TPL = `
<div class="fill">
  <div class="bar">
    <input id="a-title" class="title-input" placeholder="Article title" aria-label="Article title">
    <label title="Drafts are saved but left out of the built wiki"><input type="checkbox" id="a-draft"> Draft</label>
    <button id="a-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Section <select id="a-sec" aria-label="Section"></select></label>
    <label>Address <span class="slug-wrap"><span id="a-secslug"></span>/<input id="a-slug" aria-label="Article address">.html</span></label>
    <span class="grow"></span>
    <button id="a-import" title="Load a Markdown file from your computer into this editor">Import .md</button>
    <input type="file" id="a-file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden>
    <button id="a-up">Move up</button>
    <button id="a-down">Move down</button>
    <button id="a-delete" class="danger">Delete</button>
  </div>
  <div class="bar sub">
    <label class="wide">Description <input id="a-desc" maxlength="320" placeholder="Optional one line, used in search results, section lists and search engines"></label>
  </div>
  <div class="bar warn" id="a-missing" hidden></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the article in Markdown"></textarea>
</div>`;

const CSS_TPL = `
<div class="fill">
  <div class="bar">
    <label>Theme <select id="theme"></select></label>
    <button id="apply-theme">Apply theme</button>
    <span class="grow"></span>
    <button id="css-save" class="primary">Save CSS</button>
  </div>
  <div class="bar sub">
    <label>Preview <select id="css-target"></select></label>
    <span class="grow"></span>
    <button id="jump-global">Go to theme</button>
    <button id="jump-pages">Go to page specific</button>
  </div>
  <textarea id="css" class="code" spellcheck="false" wrap="off"></textarea>
</div>`;

const SET_TPL = `
<div class="fill">
  <div class="bar"><strong>Settings</strong><span class="grow"></span><button id="st-save" class="primary">Save settings</button></div>
  <div class="pad">
    <label class="field">Wiki title <input id="st-title"></label>
    <label class="field">Tagline <input id="st-tagline" placeholder="Shown next to the title in the top bar"></label>
    <label class="field">Meta description <textarea id="st-description" rows="2" maxlength="320" placeholder="Used for the home page in search engines"></textarea></label>
    <label class="field">Footer text <input id="st-footer"><span class="hint">You can use {year} and {title}. Inline markdown works.</span></label>
    <label class="field">Author <input id="st-author"></label>
    <label class="field">Language code <input id="st-lang" placeholder="en" style="max-width:100px"></label>
    <label class="field">Wiki URL <input id="st-url" placeholder="https://wiki.example.com"><span class="hint">Where the wiki will be hosted. Needed for canonical links, the sitemap and web search.</span></label>
    <fieldset>
      <legend>Search box</legend>
      <label><input type="radio" name="st-search" value="builtin"> Built-in search. Instant results, works offline. Adds one small script, search.js.</label>
      <label><input type="radio" name="st-search" value="web"> Web search form. No JavaScript. Sends the query to DuckDuckGo limited to your wiki URL, so it only finds pages the search engine has indexed.</label>
      <label><input type="radio" name="st-search" value="off"> No search box. The wiki uses no JavaScript at all.</label>
    </fieldset>
    <fieldset>
      <legend>Pages</legend>
      <label><input type="checkbox" id="st-toc"> Table of contents on articles with three or more headings</label>
      <label><input type="checkbox" id="st-updated"> Show the last updated date on articles</label>
      <label><input type="checkbox" id="st-expand"> Keep every sidebar section open (otherwise only the current one opens)</label>
      <label><input type="checkbox" id="st-home_sections"> List sections on the home page</label>
      <label><input type="checkbox" id="st-home_recent"> List recently updated articles on the home page</label>
    </fieldset>
    <fieldset>
      <legend>Search engines</legend>
      <label><input type="checkbox" id="st-sitemap"> Generate sitemap.xml and robots.txt</label>
      <label><input type="checkbox" id="st-noindex"> Ask search engines not to index this wiki</label>
    </fieldset>
  </div>
</div>`;

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
  toast.t = setTimeout(() => { t.className = ""; }, 3200);
}

function setDirty(v) { dirty = v; $("#dirty").hidden = !v; }
function markDirty() { setDirty(true); schedulePreview(); }
async function guard() { return !dirty || window.confirm("You have unsaved changes. Discard them?"); }
function slugify(s) {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
const enc = encodeURIComponent;
function sec(slug) { return S.sections.find((s) => s.slug === slug); }

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

/* ---------- preview ---------- */
let pvMode = "desktop";
try { pvMode = localStorage.getItem("wikigen-preview") || "desktop"; } catch (e) { /* ignore */ }

function fitPreview() {
  const wrap = $("#pv-wrap"), f = $("#preview");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!W || !H) return;
  const target = pvMode === "desktop" ? 1280 : pvMode === "mobile" ? 390 : W;
  const s = Math.min(1, W / target);
  const dx = Math.max(0, (W - target * s) / 2);
  f.style.width = target + "px";
  f.style.height = (H / s) + "px";
  f.style.transform = "translateX(" + dx + "px) scale(" + s + ")";
  $$("#pv-size button").forEach((b) => b.classList.toggle("on", b.dataset.pv === pvMode));
}

$$("#pv-size button").forEach((b) => b.addEventListener("click", () => {
  pvMode = b.dataset.pv;
  try { localStorage.setItem("wikigen-preview", pvMode); } catch (e) { /* ignore */ }
  fitPreview();
}));
new ResizeObserver(fitPreview).observe($("#pv-wrap"));

function schedulePreview() { clearTimeout(timer); timer = setTimeout(renderPreview, 250); }

function previewPayload() {
  switch (view.type) {
    case "home": return { view: "home", markdown: $("#md").value };
    case "section": return { view: "section", slug: view.slug, title: $("#s-title").value, description: $("#s-desc").value, markdown: $("#md").value };
    case "article": return {
      view: "article", section: $("#a-sec").value, orig_section: view.sec || "", orig_slug: view.slug || "",
      new_slug: $("#a-slug").value, title: $("#a-title").value, description: $("#a-desc").value, markdown: $("#md").value,
    };
    case "css": return { view: "css", target: $("#css-target").value, css: $("#css").value };
    case "settings": return { view: "settings", target: "home", site: readSettings() };
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
    const m = $("#a-missing");
    if (m) {
      const list = Array.from(new Set(r.missing || []));
      m.hidden = !list.length;
      m.textContent = list.length ? "Wiki links with no matching article: " + list.join(", ") : "";
    }
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- markdown toolbar ---------- */
function applyMd(ta, kind) {
  const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
  const wrap = (a, b, ph) => {
    const t = sel || ph;
    ta.setRangeText(a + t + b, s, e, "end");
    ta.setSelectionRange(s + a.length, s + a.length + t.length);
  };
  const prefix = (p) => {
    const ls = ta.value.lastIndexOf("\n", s - 1) + 1;
    ta.setRangeText(p, ls, ls, "preserve");
    ta.setSelectionRange(s + p.length, e + p.length);
  };
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
  else if (kind === "steps") block(sel ? sel.split("\n").map((l, i) => (i + 1) + ". " + l).join("\n") : "1. First step\n2. Second step\n3. Third step");
  else if (kind === "block") block("```bash\n" + (sel || "command here") + "\n```");
  else if (kind === "callout") block("> [!NOTE]\n> " + (sel || "Something the reader should notice.").split("\n").join("\n> "));
  else if (kind === "table") block("| Column | Column |\n|---|---|\n| Cell | Cell |");
  ta.dispatchEvent(new Event("input"));
  ta.focus();
}

async function uploadImage(file, ta) {
  const r = await fetch("/api/upload?name=" + enc(file.name), { method: "POST", headers: { "X-Token": TOKEN }, body: file });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Upload failed");
  ta.setRangeText("![" + file.name.replace(/\.[^.]+$/, "") + "](" + d.path + ")", ta.selectionStart, ta.selectionEnd, "end");
  ta.dispatchEvent(new Event("input"));
  toast("Image added to assets");
}

function bindEditor() {
  const ta = $("#md");
  ta.addEventListener("input", markDirty);
  $$(".md-tools button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.md === "image") $("#img-file").click(); else applyMd(ta, b.dataset.md);
  }));
  $("#img-file").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (f) { try { await uploadImage(f, ta); } catch (e) { toast(e.message, "bad"); } }
  });
}

/* ---------- sidebar ---------- */
function renderTop() { $("#folder").textContent = S.folder; $("#folder").title = S.folder; }

function renderSide() {
  const side = $("#side");
  side.innerHTML = "";
  const item = (label, cls, active, fn, chip) => {
    const b = document.createElement("button");
    b.className = "item " + cls + (active ? " active" : "");
    const t = document.createElement("span"); t.className = "t"; t.textContent = label; b.appendChild(t);
    if (chip) { const c = document.createElement("span"); c.className = "chip " + (chip[1] || ""); c.textContent = chip[0]; b.appendChild(c); }
    b.addEventListener("click", fn);
    side.appendChild(b);
  };
  item("Home page", "", view.type === "home", () => go(openHome));
  const h = document.createElement("h3"); h.textContent = "Sections"; side.appendChild(h);
  if (!S.sections.length) {
    const e = document.createElement("div"); e.className = "empty"; e.textContent = "No sections yet. Use New section in the top bar."; side.appendChild(e);
  }
  S.sections.forEach((s) => {
    item(s.title, "sec", view.type === "section" && view.slug === s.slug, () => go(() => openSection(s.slug)), [String(s.articles.length)]);
    s.articles.forEach((a) => item(a.title, "art", view.type === "article" && view.sec === s.slug && view.slug === a.slug,
      () => go(() => openArticle(s.slug, a.slug)), a.draft ? ["Draft", "draft"] : null));
    item("+ New article", "add", false, () => go(() => openArticle(null, null, s.slug)));
  });
}

async function refresh() { S = await api("GET", "state"); renderTop(); renderSide(); }
async function go(fn) {
  if (!(await guard())) return;
  setDirty(false);
  try { await fn(); } catch (e) { toast(e.message, "bad"); }
}

/* ---------- home ---------- */
async function openHome() {
  const d = await api("GET", "home");
  view = { type: "home" };
  $("#left").innerHTML = HOME_TPL.replace("MDTOOLS", MD_TOOLS);
  $("#md").value = d.markdown;
  $("#h-save").addEventListener("click", saveHome);
  bindEditor();
  renderSide();
  renderPreview();
}

async function saveHome() {
  try { await api("PUT", "home", { markdown: $("#md").value }); setDirty(false); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- sections ---------- */
async function newSection() {
  const title = window.prompt("Section name, for example Fun stuff or Linux - Technical");
  if (!title || !title.trim()) return;
  const r = await api("POST", "sections", { title: title.trim() });
  await refresh();
  await openSection(r.slug);
  toast("Section created");
}

async function openSection(slug) {
  const d = await api("GET", "sections/" + enc(slug));
  view = { type: "section", slug };
  $("#left").innerHTML = SEC_TPL.replace("MDTOOLS", MD_TOOLS);
  $("#s-title").value = d.section.title;
  $("#s-slug").value = d.section.slug;
  $("#s-desc").value = d.section.description || "";
  $("#md").value = d.markdown;
  ["#s-title", "#s-slug", "#s-desc"].forEach((id) => $(id).addEventListener("input", markDirty));
  $("#s-save").addEventListener("click", saveSection);
  $("#s-delete").addEventListener("click", deleteSection);
  $("#s-up").addEventListener("click", () => moveSection(-1));
  $("#s-down").addEventListener("click", () => moveSection(1));
  $("#s-new").addEventListener("click", () => go(() => openArticle(null, null, view.slug)));
  $("#s-sort").addEventListener("click", sortArticles);
  renderArtRows();
  bindEditor();
  renderSide();
  renderPreview();
}

function renderArtRows() {
  const box = $("#s-arts");
  const s = sec(view.slug);
  box.innerHTML = "";
  $("#s-sort").disabled = !s || s.articles.length < 2;
  const i0 = S.sections.indexOf(s);
  $("#s-up").disabled = i0 <= 0;
  $("#s-down").disabled = i0 >= S.sections.length - 1;
  if (!s || !s.articles.length) { const e = document.createElement("div"); e.className = "empty"; e.textContent = "No articles yet."; box.appendChild(e); return; }
  s.articles.forEach((a, i) => {
    const row = document.createElement("div"); row.className = "art-row";
    const t = document.createElement("span"); t.className = "t"; t.textContent = a.title + (a.draft ? " (draft)" : ""); row.appendChild(t);
    const mk = (label, dis, fn) => { const b = document.createElement("button"); b.textContent = label; b.disabled = dis; b.addEventListener("click", fn); row.appendChild(b); };
    mk("Up", i === 0, () => reorder(i, i - 1));
    mk("Down", i === s.articles.length - 1, () => reorder(i, i + 1));
    mk("Edit", false, () => go(() => openArticle(s.slug, a.slug)));
    box.appendChild(row);
  });
}

async function saveOrder(slug, order) {
  await api("POST", "sections/" + enc(slug) + "/order", { articles: order });
  await refresh();
  if (view.type === "section") renderArtRows();
  schedulePreview();
}

async function reorder(i, j) {
  const list = sec(view.slug).articles.map((a) => a.slug);
  list.splice(j, 0, list.splice(i, 1)[0]);
  try { await saveOrder(view.slug, list); } catch (e) { toast(e.message, "bad"); }
}

async function sortArticles() {
  const list = sec(view.slug).articles.slice().sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" })).map((a) => a.slug);
  try { await saveOrder(view.slug, list); toast("Sorted"); } catch (e) { toast(e.message, "bad"); }
}

async function saveSection() {
  try {
    const r = await api("PUT", "sections/" + enc(view.slug), { title: $("#s-title").value, new_slug: $("#s-slug").value, description: $("#s-desc").value, markdown: $("#md").value });
    view.slug = r.slug;
    $("#s-slug").value = r.slug;
    setDirty(false);
    await refresh();
    renderArtRows();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveSection(dir) {
  try {
    await api("POST", "sections/" + enc(view.slug) + "/move", { dir });
    await refresh();
    renderArtRows();
    schedulePreview();
  } catch (e) { toast(e.message, "bad"); }
}

async function deleteSection() {
  const s = sec(view.slug);
  const n = s ? s.articles.length : 0;
  if (!window.confirm("Delete the section \"" + (s ? s.title : view.slug) + "\"" + (n ? " and its " + n + " article(s)" : "") + "?\n\nThe markdown files are moved to .wikigen/trash, not destroyed.")) return;
  try {
    await api("DELETE", "sections/" + enc(view.slug));
    setDirty(false);
    await refresh();
    await openHome();
    toast("Section deleted");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- articles ---------- */
async function openArticle(secSlug, slug, forSection) {
  if (!S.sections.length) {
    toast("Create a section first. Articles live inside sections.", "bad");
    await newSection();
    return;
  }
  let art = { title: "", description: "", draft: false, slug: "" }, markdown = "", where = forSection || secSlug;
  if (slug) {
    const d = await api("GET", "articles/" + enc(secSlug) + "/" + enc(slug));
    art = d.article; markdown = d.markdown; where = d.section;
  }
  if (!where) where = (view.type === "section" && view.slug) || (view.type === "article" && view.sec) || S.sections[0].slug;
  view = { type: "article", sec: slug ? where : "", slug: slug || "" };
  $("#left").innerHTML = ART_TPL.replace("MDTOOLS", MD_TOOLS);
  const sel = $("#a-sec");
  S.sections.forEach((s) => { const o = document.createElement("option"); o.value = s.slug; o.textContent = s.title; sel.appendChild(o); });
  sel.value = where;
  $("#a-secslug").textContent = where;
  $("#a-title").value = art.title;
  $("#a-slug").value = art.slug;
  $("#a-desc").value = art.description || "";
  $("#a-draft").checked = !!art.draft;
  $("#md").value = markdown;
  slugTouched = !!slug;
  $("#a-title").addEventListener("input", () => { if (!slugTouched) $("#a-slug").value = slugify($("#a-title").value); markDirty(); });
  $("#a-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  $("#a-desc").addEventListener("input", markDirty);
  $("#a-draft").addEventListener("change", markDirty);
  sel.addEventListener("change", () => { $("#a-secslug").textContent = sel.value; markDirty(); });
  $("#a-save").addEventListener("click", saveArticle);
  $("#a-delete").addEventListener("click", deleteArticle);
  $("#a-up").addEventListener("click", () => moveArticle(-1));
  $("#a-down").addEventListener("click", () => moveArticle(1));
  $("#a-import").addEventListener("click", () => $("#a-file").click());
  $("#a-file").addEventListener("change", importFile);
  syncArticleButtons();
  bindEditor();
  renderSide();
  renderPreview();
  if (!slug) $("#a-title").focus();
}

function syncArticleButtons() {
  const saved = !!view.slug;
  $("#a-delete").hidden = !saved;
  const s = saved ? sec(view.sec) : null;
  const i = s ? s.articles.findIndex((a) => a.slug === view.slug) : -1;
  $("#a-up").hidden = $("#a-down").hidden = !saved;
  if (saved) { $("#a-up").disabled = i <= 0; $("#a-down").disabled = i < 0 || i >= s.articles.length - 1; }
}

function importFile(ev) {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  const ta = $("#md");
  if (ta.value.trim() && !window.confirm("Replace the text in the editor with " + f.name + "?")) return;
  const rd = new FileReader();
  rd.onload = () => {
    const text = String(rd.result).replace(/\r\n?/g, "\n");
    ta.value = text;
    if (!$("#a-title").value.trim()) {
      const first = text.split("\n").find((l) => l.trim()) || "";
      const m = first.match(/^\s*#\s+(.+?)\s*#*\s*$/);
      const title = m ? m[1] : f.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
      $("#a-title").value = title.charAt(0).toUpperCase() + title.slice(1);
      if (!slugTouched) $("#a-slug").value = slugify($("#a-title").value);
    }
    markDirty();
    toast("Imported " + f.name + ". Save to keep it.");
  };
  rd.onerror = () => toast("Could not read that file.", "bad");
  rd.readAsText(f);
}

async function saveArticle() {
  const body = {
    section: $("#a-sec").value, orig_section: view.sec || "", orig_slug: view.slug || "",
    new_slug: $("#a-slug").value, title: $("#a-title").value, description: $("#a-desc").value,
    draft: $("#a-draft").checked, markdown: $("#md").value,
  };
  try {
    const r = await api("POST", "articles", body);
    view.sec = r.section; view.slug = r.slug;
    $("#a-slug").value = r.slug;
    $("#a-secslug").textContent = r.section;
    slugTouched = true;
    setDirty(false);
    await refresh();
    syncArticleButtons();
    toast(r.article.draft ? "Saved as draft" : "Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveArticle(dir) {
  const s = sec(view.sec);
  const list = s.articles.map((a) => a.slug);
  const i = list.indexOf(view.slug), j = i + dir;
  if (i < 0 || j < 0 || j >= list.length) return;
  list.splice(j, 0, list.splice(i, 1)[0]);
  try { await saveOrder(view.sec, list); syncArticleButtons(); } catch (e) { toast(e.message, "bad"); }
}

async function deleteArticle() {
  if (!window.confirm("Delete this article? The markdown file is moved to .wikigen/trash.")) return;
  try {
    const where = view.sec;
    await api("DELETE", "articles/" + enc(view.sec) + "/" + enc(view.slug));
    setDirty(false);
    await refresh();
    await openSection(where);
    toast("Article deleted");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- css ---------- */
async function openCss() {
  const d = await api("GET", "css");
  view = { type: "css" };
  $("#left").innerHTML = CSS_TPL;
  const th = $("#theme");
  S.themes.forEach((t) => { const o = document.createElement("option"); o.value = t.id; o.textContent = t.label; th.appendChild(o); });
  const tg = $("#css-target");
  const opt = (v, label) => { const o = document.createElement("option"); o.value = v; o.textContent = label; tg.appendChild(o); };
  opt("home", "Home page");
  S.sections.forEach((s) => {
    opt("section:" + s.slug, "Section: " + s.title);
    s.articles.forEach((a) => opt("article:" + s.slug + "/" + a.slug, "Article: " + a.title));
  });
  $("#css").value = d.css;
  $("#css").addEventListener("input", markDirty);
  tg.addEventListener("change", renderPreview);
  $("#css-save").addEventListener("click", saveCss);
  $("#apply-theme").addEventListener("click", applyTheme);
  $("#jump-global").addEventListener("click", () => jumpTo("GLOBAL THEME"));
  $("#jump-pages").addEventListener("click", () => jumpTo("PAGE SPECIFIC RULES"));
  renderSide();
  renderPreview();
}

function applyTheme() {
  const t = S.themes.find((x) => x.id === $("#theme").value);
  const ta = $("#css");
  if (!window.confirm("Replace the theme variables at the top of your CSS with \"" + t.label + "\"?\n\nEdits inside that top :root block are overwritten. Everything else stays.")) return;
  const re = /:root\s*\{[^}]*\}/;
  ta.value = re.test(ta.value) ? ta.value.replace(re, () => t.block) : t.block + "\n\n" + ta.value;
  markDirty();
}

function jumpTo(marker) {
  const ta = $("#css");
  const i = ta.value.indexOf(marker);
  if (i < 0) { toast("That section marker is not in your CSS any more.", "bad"); return; }
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  const line = ta.value.slice(0, i).split("\n").length - 1;
  ta.focus();
  ta.setSelectionRange(i, i);
  ta.scrollTop = Math.max(0, (line - 1) * lh);
}

async function saveCss() {
  try { await api("PUT", "css", { css: $("#css").value }); setDirty(false); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- settings ---------- */
const BOOLS = ["toc", "updated", "expand", "home_sections", "home_recent", "sitemap", "noindex"];
function readSettings() {
  const o = {
    title: $("#st-title").value, tagline: $("#st-tagline").value, description: $("#st-description").value,
    footer: $("#st-footer").value, author: $("#st-author").value, lang: $("#st-lang").value, url: $("#st-url").value,
    search: ($("input[name=st-search]:checked") || { value: "builtin" }).value,
  };
  BOOLS.forEach((k) => { o[k] = $("#st-" + k).checked; });
  return o;
}

async function openSettings() {
  view = { type: "settings" };
  $("#left").innerHTML = SET_TPL;
  const s = S.site;
  ["title", "tagline", "description", "footer", "author", "lang", "url"].forEach((k) => { $("#st-" + k).value = s[k] || ""; });
  BOOLS.forEach((k) => { $("#st-" + k).checked = !!s[k]; });
  const r = $("input[name=st-search][value=" + (s.search || "builtin") + "]");
  if (r) r.checked = true;
  $$("#left input, #left textarea").forEach((i) => i.addEventListener(i.type === "checkbox" || i.type === "radio" ? "change" : "input", markDirty));
  $("#st-save").addEventListener("click", saveSettings);
  renderSide();
  renderPreview();
}

async function saveSettings() {
  try { await api("PUT", "settings", readSettings()); setDirty(false); await refresh(); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- top bar ---------- */
function themeLabel() {
  $("#b-theme").textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode";
}

function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("wikigen-theme", next); } catch (e) { /* ignore */ }
  themeLabel();
}

async function rescan() {
  const r = await api("POST", "rescan");
  await refresh();
  const bits = [];
  if (r.sections_added) bits.push(r.sections_added + " section(s) added");
  if (r.articles_added) bits.push(r.articles_added + " article(s) added");
  if (r.articles_removed) bits.push(r.articles_removed + " article(s) removed because their file is gone");
  toast(bits.length ? bits.join(", ") : "No new files found. Wiki rebuilt.");
  schedulePreview();
}

async function act(name) {
  if (name === "theme") { toggleTheme(); return; }
  if (name === "quit") {
    if (!(await guard())) return;
    await api("POST", "quit");
    document.body.innerHTML = "<p style='padding:40px;font:16px system-ui'>WikiGen has stopped. You can close this tab.</p>";
    return;
  }
  await go(async () => {
    if (name === "new-section") await newSection();
    else if (name === "new-article") await openArticle(null, null, null);
    else if (name === "home") await openHome();
    else if (name === "css") await openCss();
    else if (name === "settings") await openSettings();
    else if (name === "rescan") await rescan();
    else if (name === "publish") await openPublish();
    else if (name === "switch") await chooseFolder();
  });
}

/* ---------- folders ---------- */
async function openFolder(path, ok) {
  const r = await api("POST", "open", { path, confirm: !!ok });
  if (r.needs_confirm) {
    if (window.confirm(r.message)) return openFolder(path, true);
    return;
  }
  S = r.state;
  await boot();
}

async function chooseFolder(inputEl) {
  const r = await api("POST", "pick-folder");
  let path = r.path;
  if (!r.available) path = window.prompt("Folder path for your wiki", (S && S.folder) || (inputEl && inputEl.value) || "");
  if (!path) return;
  if (inputEl) inputEl.value = path;
  await openFolder(path, false);
}

function showWelcome() {
  $("#workspace").hidden = true;
  $("#welcome").hidden = false;
  $("#w-path").value = S.last || S.suggest || "";
  $("#w-browse").onclick = () => chooseFolder($("#w-path")).catch((e) => toast(e.message, "bad"));
  $("#w-open").onclick = () => openFolder($("#w-path").value, false).catch((e) => toast(e.message, "bad"));
}

async function boot() {
  $("#welcome").hidden = true;
  $("#workspace").hidden = false;
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
    if (view.type === "home") saveHome();
    else if (view.type === "section") saveSection();
    else if (view.type === "article") saveArticle();
    else if (view.type === "css") saveCss();
    else if (view.type === "settings") saveSettings();
  }
});
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

themeLabel();

(async function init() {
  try {
    S = await api("GET", "state");
    if (!S.folder) showWelcome(); else await boot();
  } catch (e) { toast(e.message, "bad"); }
})();
</script>
</body>
</html>
'''


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="WikiGen: local wiki-style static site generator with a markdown editor and live preview.")
    ap.add_argument("folder", nargs="?", help="wiki folder to open (created if it does not exist)")
    ap.add_argument("--port", type=int, default=8766, help="port for the local editor (default 8766)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab automatically")
    args = ap.parse_args()

    if args.folder:
        try:
            r = open_site(args.folder)
            if r.get("needs_confirm"):
                print("That folder has files WikiGen did not create. Open it from the editor to confirm.")
        except ApiError as e:
            print("Could not open folder:", e)
    else:
        last = last_folder()
        if last and (Path(last) / META_DIR / "config.json").exists():
            try:
                open_site(last)
            except Exception as e:  # noqa: BLE001
                print("Could not reopen the last wiki:", e)

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
    print("WikiGen is running at %s (Ctrl+C to stop)" % url)
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
