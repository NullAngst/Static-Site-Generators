#!/usr/bin/env python3
"""Self-test for the generators. Standard library only, no network, no browser.

It starts each tool on a free port in a throwaway folder (with its own config folder, so your
real settings are never touched), talks to it over HTTP the way the editor does, and also
imports the tools as modules to test the publish code directly.

The podcast tests build shows in temporary folders, read the feeds back with ElementTree, publish
into a temporary folder served by a small local web server, and run ffmpeg when it is installed
(those tests are skipped otherwise). Nothing is sent over the internet: pings are switched off.

Run from anywhere: python3 tools/selftest.py
Add -v for one line per test.
"""
import functools
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True  # importing the tools must not leave __pycache__ in the repo

REPO = Path(__file__).resolve().parent.parent
ALL_TOOLS = ("siteforge.py", "sitegen.py", "wikigen.py", "storygen.py", "podcastgen.py")
TOOLS = [t for t in ALL_TOOLS if (REPO / t).is_file()]
_MODULES = {}


def load(tool):
    """Import a tool as a module. Each one guards main() behind __name__."""
    if tool not in _MODULES:
        spec = importlib.util.spec_from_file_location("selftest_" + tool[:-3], REPO / tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[tool] = mod
    return _MODULES[tool]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """One tool running as its own process on a free port."""

    def __init__(self, tool, scratch):
        self.port = free_port()
        env = dict(os.environ, XDG_CONFIG_HOME=str(scratch / "config"), HOME=str(scratch / "home"))
        (scratch / "home").mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / tool), "--no-browser", "--port", str(self.port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.base = "http://127.0.0.1:%d" % self.port
        deadline = time.time() + 15
        page = None
        while time.time() < deadline:
            try:
                page = urlopen(self.base + "/", timeout=2).read().decode("utf-8")
                break
            except OSError:
                time.sleep(0.1)
        if page is None:
            self.close()
            raise RuntimeError("%s did not start" % tool)
        self.token = re.search(r'const TOKEN = "([^"]+)"', page).group(1)

    def api(self, method, route, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + "/api/" + route, data=data, method=method,
                      headers={"X-Token": self.token, "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            raise AssertionError("%s %s -> %d %s" % (method, route, e.code, e.read()[:300]))

    def raw(self, method, path, headers=None):
        req = Request(self.base + path, method=method, headers=headers or {})
        try:
            with urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.proc.stderr:
            self.proc.stderr.close()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="selftest-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)


class ServerTests(Scratch):
    """Built files are streamed, answer HEAD, and honor a single byte range (fix 1)."""

    def open_tool(self, tool):
        app = Running(tool, self.scratch)
        self.addCleanup(app.close)
        site = self.scratch / "site"
        body = {"path": str(site), "confirm": True}
        if tool == "siteforge.py":
            body["setup"] = {"title": "Test", "home": "page", "modules": ["blog", "wiki"]}
        elif tool == "podcastgen.py":
            body["setup"] = {"title": "Test"}
        app.api("POST", "open", body)
        blob = bytes(range(256)) * 1200  # 307,200 bytes
        (site / "assets").mkdir(exist_ok=True)
        (site / "assets" / "blob.bin").write_bytes(blob)
        return app, site, blob

    def test_ranges_and_head(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                app, site, blob = self.open_tool(tool)
                status, headers, body = app.raw("GET", "/site/assets/blob.bin")
                self.assertEqual((status, body), (200, blob))
                self.assertEqual(headers.get("Accept-Ranges"), "bytes")

                status, headers, body = app.raw("HEAD", "/site/assets/blob.bin")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Length"), str(len(blob)))
                self.assertEqual(body, b"")

                status, headers, body = app.raw("GET", "/site/assets/blob.bin", {"Range": "bytes=0-99"})
                self.assertEqual((status, body), (206, blob[:100]))
                self.assertEqual(headers.get("Content-Range"), "bytes 0-99/%d" % len(blob))

                status, _, body = app.raw("GET", "/site/assets/blob.bin", {"Range": "bytes=-100"})
                self.assertEqual((status, body), (206, blob[-100:]))

                status, _, body = app.raw("GET", "/site/assets/blob.bin", {"Range": "bytes=307000-"})
                self.assertEqual((status, body), (206, blob[307000:]))

                status, headers, _ = app.raw("GET", "/site/assets/blob.bin", {"Range": "bytes=999999-"})
                self.assertEqual(status, 416)
                self.assertEqual(headers.get("Content-Range"), "bytes */%d" % len(blob))

                status, _, body = app.raw("GET", "/site/assets/blob.bin", {"Range": "bytes=0-1,5-9"})
                self.assertEqual((status, body), (200, blob))  # several ranges: whole file

                status, _, _ = app.raw("HEAD", "/api/state")
                self.assertEqual(status, 405)
                status, _, _ = app.raw("GET", "/site/../../etc/passwd")
                self.assertIn(status, (403, 404))
                app.close()

    def test_preview_script_policy(self):
        """Built pages may run only the generated search.js, named by its exact address (fix 8)."""
        expected = {
            "siteforge.py": "script-src http://127.0.0.1:{port}/site/wiki/search.js;",
            "wikigen.py": "script-src http://127.0.0.1:{port}/site/search.js;",
            "sitegen.py": "script-src 'none';",
            "storygen.py": "script-src 'none';",
            "podcastgen.py": "script-src 'none';",
        }
        for tool in TOOLS:
            if tool not in expected:
                continue
            with self.subTest(tool=tool):
                app, site, _ = self.open_tool(tool)
                _, headers, _ = app.raw("GET", "/site/assets/blob.bin")
                self.assertTrue(headers.get("Content-Security-Policy", "").startswith(
                    expected[tool].format(port=app.port)), headers.get("Content-Security-Policy"))
                app.close()


class FeedTests(Scratch):
    def test_siteforge_feed_links_are_absolute(self):
        """Feed bodies carry absolute links, and ]]> survives the CDATA section (fix 5)."""
        if "siteforge.py" not in TOOLS:
            self.skipTest("no siteforge.py")
        app = Running("siteforge.py", self.scratch)
        self.addCleanup(app.close)
        site = self.scratch / "site"
        app.api("POST", "open", {"path": str(site), "confirm": True,
                                 "setup": {"title": "Test", "home": "page", "modules": ["blog"]}})
        app.api("PUT", "settings", {"url": "https://example.com", "feeds": True})
        app.api("POST", "posts", {"title": "Hello & <welcome>", "publish": True,
                                  "markdown": "See ![pic](assets/a.png), [about](about.html) and `]]>`."})
        for name in ("rss.xml", "blog/rss.xml"):
            text = (site / name).read_text(encoding="utf-8")
            root = ET.fromstring(text)
            body = root.find("channel/item/{http://purl.org/rss/1.0/modules/content/}encoded").text
            self.assertIn('src="https://example.com/assets/a.png"', body)
            self.assertIn('href="https://example.com/about.html"', body)
            self.assertIn("]]&gt;", body)  # the code span's own escaping, intact after the CDATA split
            self.assertEqual(root.find("channel/item/title").text, "Hello & <welcome>")


class PublishTests(Scratch):
    """Hard-link staging and changed-files-only uploads (fixes 3, 4 and 6)."""

    def make_site(self, mod):
        root = self.scratch / "site"
        (root / "sub").mkdir(parents=True)
        (root / "index.html").write_text("<p>home</p>", encoding="utf-8")
        (root / "sub" / "page.html").write_text("<p>page</p>", encoding="utf-8")
        (root / ".meta").mkdir()

        class FakeSite:
            pass

        site = FakeSite()
        site.root, site.meta = root, root / ".meta"
        dest = self.scratch / "out"
        dep = mod.validate_deploy(mod.clean_deploy({"method": "folder", "path": str(dest)}, None))
        return site, dep, dest

    def last_lines(self, mod, n=6):
        return "\n".join(mod.JOB["lines"][-n:])

    def test_staging_links_and_cleans_up(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                mod = load(tool)
                site, _, _ = self.make_site(mod)
                files, _ = mod.local_files(site.root)
                staged, kept = mod.stage(site, files)
                try:
                    self.assertEqual(sorted(kept), ["index.html", "sub/page.html"])
                    self.assertEqual((staged / "index.html").stat().st_ino, (site.root / "index.html").stat().st_ino)
                    self.assertIn(site.meta / "staging", staged.parents)
                    self.assertEqual(mod.local_files(site.root)[0], files)  # staging is never published
                finally:
                    shutil.rmtree(staged)
                stale = site.meta / "staging" / "publish-old"
                stale.mkdir()
                staged, _ = mod.stage(site, files)
                self.assertFalse(stale.exists())
                shutil.rmtree(self.scratch / "site")

    def test_only_changed_files_are_sent(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                mod = load(tool)
                site, dep, dest = self.make_site(mod)
                mod.run_deploy(site, dep, "", False)
                self.assertEqual((dest / "sub" / "page.html").read_text(), "<p>page</p>")
                mod.run_deploy(site, dep, "", False)
                self.assertIn("0 of them changed", self.last_lines(mod))
                time.sleep(0.01)
                mod.write_text(site.root / "sub" / "page.html", "<p>new</p>")
                mod.run_deploy(site, dep, "", False)
                self.assertIn("1 of them changed", self.last_lines(mod))
                self.assertEqual((dest / "sub" / "page.html").read_text(), "<p>new</p>")
                dep["full_upload"] = True
                mod.run_deploy(site, dep, "", False)
                self.assertNotIn("of them changed", self.last_lines(mod, 3))
                self.assertEqual([p.name for p in site.root.iterdir() if p.name.endswith(".tmp")], [])
                shutil.rmtree(self.scratch / "site")
                shutil.rmtree(dest)

    def test_remote_dir_test_expands_home(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                mod = load(tool)
                self.assertEqual(mod.remote_dir_test("~/public_html"), 'test -d "$HOME"/public_html')
                self.assertEqual(mod.remote_dir_test("~"), 'test -d "$HOME"')
                self.assertEqual(mod.remote_dir_test("/var/www/my site"), "test -d '/var/www/my site'")


class RescanTests(Scratch):
    def test_md_files_skips_symlinks(self):
        """Rescans never follow symlinked folders or files (fix 9)."""
        mod = load(TOOLS[0])
        real = self.scratch / "real"
        real.mkdir()
        (real / "a.md").write_text("# A", encoding="utf-8")
        (real / "b.md").symlink_to(self.scratch / "outside.md")
        (self.scratch / "outside.md").write_text("# secret", encoding="utf-8")
        (self.scratch / "linked").symlink_to(real, target_is_directory=True)
        self.assertEqual([f.name for f in mod.md_files(real)], ["a.md"])
        self.assertEqual(mod.md_files(self.scratch / "linked"), [])


class ImportTests(Scratch):
    def test_clashing_asset_is_renamed_and_rewritten(self):
        """An imported image never silently takes a different existing image's place (fix 10)."""
        if "siteforge.py" not in TOOLS:
            self.skipTest("no siteforge.py")
        mod = load("siteforge.py")
        src, dst = self.scratch / "old", self.scratch / "new"
        (src / "assets").mkdir(parents=True)
        (dst / "assets").mkdir(parents=True)
        (src / "assets" / "photo.jpg").write_bytes(b"incoming")
        (src / "assets" / "same.jpg").write_bytes(b"same")
        (dst / "assets" / "photo.jpg").write_bytes(b"already here")
        (dst / "assets" / "same.jpg").write_bytes(b"same")

        class Fake:
            root = dst

        copied, renames = mod.ImportMixin._import_assets(Fake(), src)
        self.assertEqual(renames, {"photo.jpg": "photo-2.jpg"})
        self.assertEqual(copied, 1)
        self.assertEqual((dst / "assets" / "photo-2.jpg").read_bytes(), b"incoming")
        self.assertEqual((dst / "assets" / "photo.jpg").read_bytes(), b"already here")
        md = src / "post.md"
        md.write_text("![a](assets/photo.jpg) ![b](assets/photo.jpg.bak) ![c](assets/same.jpg)", encoding="utf-8")
        mod.IMPORT_RENAMES.update(renames)
        try:
            text = mod.src_text(md)
        finally:
            mod.IMPORT_RENAMES.clear()
        self.assertEqual(text, "![a](assets/photo-2.jpg) ![b](assets/photo.jpg.bak) ![c](assets/same.jpg)")


# ----------------------------------------------------------------------------
# Podcast
# ----------------------------------------------------------------------------

PODCAST_TOOLS = [t for t in ("siteforge.py", "podcastgen.py") if t in TOOLS]
FAKE_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 40
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0",
      "content": "http://purl.org/rss/1.0/modules/content/", "atom": "http://www.w3.org/2005/Atom"}
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_png(path, w, h, alpha=False):
    """A plain PNG written by hand, so the art tests need no image library or ffmpeg."""
    ch = 4 if alpha else 3

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = (b"\x00" + b"\x80" * (w * ch)) * h
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6 if alpha else 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class NoPath:
    """PATH emptied for the duration, so shutil.which finds no ffmpeg, ffprobe or rclone."""

    def __enter__(self):
        self.old = os.environ.get("PATH", "")
        os.environ["PATH"] = ""

    def __exit__(self, *exc):
        os.environ["PATH"] = self.old


class RangedHandler(SimpleHTTPRequestHandler):
    """A static server that says it answers byte ranges, like nginx or Apache."""

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, *args):
        pass


def serve(folder):
    folder.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(RangedHandler, directory=str(folder)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class PodcastCase(Scratch):
    def web(self, folder):
        """Serve a folder over HTTP for the length of the test; returns its base URL."""
        httpd = serve(folder)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    def show(self, tool="siteforge.py", url="https://example.com", home="podcast", root_mount=False, name="site"):
        """A show with art, an owner and a category, ready for episodes. Pings are off."""
        mod = load(tool)
        root = self.scratch / name
        root.mkdir(parents=True)
        site = mod.Site(root)
        site.create({"title": "Example Show", "home": home, "modules": ["podcast"], "root_mount": root_mount})
        site.save_settings({"url": url, "author": "Example Author", "footer": "&copy; {year} {title}"})
        make_png(root / "assets" / "art.png", 1400, 1400)
        site.save_show({"owner_email": "show@example.com", "owner_name": "Owner", "categories": [{"cat": "Technology"}],
                        "image": "assets/art.png", "ping": False, "markdown": "About the show."})
        return mod, site

    def episode(self, site, title, date, publish=True, notes="", audio=FAKE_MP3, **kw):
        d = {"title": title, "date": date, "markdown": notes}
        d.update(kw)
        slug = site.save_episode(d)["slug"]
        if audio is not None:
            self.give_audio(site, slug, audio)
        if publish:
            site.save_episode({"orig_slug": slug, "title": title, "publish": True})
        return slug

    def give_audio(self, site, slug, data, ext="mp3"):
        """Put a file in place as the episode's audio, the way processing leaves it."""
        e = site.find_episode(slug)
        folder = site.audio_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = site.audio_name(e, ext)
        (folder / name).write_bytes(data)
        e["audio"] = {"file": name, "bytes": len(data), "type": "audio/mpeg" if ext == "mp3" else "audio/x-m4a",
                      "probed": True}
        e["duration"] = 61
        site.normalize_podcast()
        site.save_cfg()
        site.build()
        return name

    def feed(self, site):
        return (site.root / site.feed_rel()).read_text(encoding="utf-8")


class PodcastFeedTests(PodcastCase):
    def test_required_tags_escaping_and_links(self):
        mod, site = self.show()
        title = "Fish & chips <live> \"quoted\" 'single' \U0001F3A7"
        slug = self.episode(site, title, "2026-03-01T06:00:00-08:00", number=3, season=1,
                            notes="Show notes with ![pic](assets/art.png), [about](about.html) and `]]>` in code.",
                            chapters=[{"start": "0:00", "title": "Intro"}, {"start": "1:02", "title": "Main"}])
        text = self.feed(site)
        root = ET.fromstring(text.encode("utf-8"))
        ch = root.find("channel")
        for tag in ("title", "link", "description", "language", "itunes:author", "itunes:image", "itunes:explicit",
                    "itunes:category", "itunes:owner/itunes:email", "itunes:type", "podcast:guid", "podcast:locked"):
            self.assertIsNotNone(ch.find(tag, NS), tag)
        self.assertEqual(ch.find("copyright").text, "\u00a9 %d Example Show" % time.localtime().tm_year)
        item = ch.find("item")
        for tag in ("title", "link", "guid", "pubDate", "description", "content:encoded", "enclosure",
                    "itunes:duration", "itunes:episode", "itunes:season", "itunes:episodeType", "podcast:chapters"):
            self.assertIsNotNone(item.find(tag, NS), tag)
        self.assertEqual(item.find("title").text, title)
        self.assertEqual(item.find("guid").get("isPermaLink"), "false")
        self.assertEqual(item.find("pubDate").text, "Sun, 01 Mar 2026 06:00:00 -0800")
        self.assertEqual(item.find("itunes:duration", NS).text, "61")
        enc = item.find("enclosure")
        path = site.audio_dir() / enc.get("url").rsplit("/", 1)[1]
        self.assertEqual(int(enc.get("length")), path.stat().st_size)
        self.assertEqual(enc.get("type"), "audio/mpeg")
        body = item.find("content:encoded", NS).text
        self.assertIn('src="https://example.com/assets/art.png"', body)
        self.assertIn('href="https://example.com/about.html"', body)
        self.assertIn("]]&gt;", body)
        outside = re.sub(r"<!\[CDATA\[.*?\]\]>", "", text, flags=re.S)
        self.assertIsNone(re.search(r"&(?!amp;|lt;|gt;|quot;|apos;|#)[A-Za-z]+;", outside))
        self.assertEqual(ET.fromstring("<x>%s</x>" % mod.cdata("a]]>b")).text, "a]]>b")
        chapters = json.loads((site.root / site.chapters_rel(slug)).read_text(encoding="utf-8"))
        self.assertEqual(chapters, {"version": "1.2.0", "chapters": [{"startTime": 0, "title": "Intro"},
                                                                     {"startTime": 62, "title": "Main"}]})
        self.assertEqual([x for x in site._pod_check if x["level"] == "fail"], [])

    def test_drafts_future_and_every_published_episode(self):
        _, site = self.show()
        folder = site.audio_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(60):
            name = "ep%03d.mp3" % (i + 1)
            (folder / name).write_bytes(FAKE_MP3)
            site.cfg["podcast"]["episodes"].append({
                "slug": "show-%02d" % i, "title": "Show %02d" % i, "number": i + 1, "status": "published",
                "date": "2025-01-%02dT06:00:00+00:00" % (i % 28 + 1), "duration": 60,
                "audio": {"file": name, "bytes": len(FAKE_MP3), "type": "audio/mpeg", "probed": True}})
        site.normalize_podcast()
        site.build()
        self.episode(site, "A draft", "2025-02-01T06:00:00+00:00", publish=False)
        self.episode(site, "From the future", "2099-01-01T06:00:00+00:00")
        items = ET.fromstring(self.feed(site).encode("utf-8")).findall("channel/item")
        titles = {i.find("title").text for i in items}
        self.assertEqual(len(items), 60)
        self.assertNotIn("A draft", titles)
        self.assertNotIn("From the future", titles)
        self.assertTrue(any(x["level"] == "info" and "scheduled" in x["text"] for x in site._pod_check))

    def test_two_builds_are_identical(self):
        _, site = self.show()
        self.episode(site, "One", "2026-01-01T06:00:00+00:00")
        first = self.feed(site)
        time.sleep(1.1)
        site.build()
        self.assertEqual(self.feed(site), first)

    def test_guid_survives_changes(self):
        _, site = self.show()
        slug = self.episode(site, "Original", "2026-01-01T06:00:00+00:00", number=1)
        guid = site.find_episode(slug)["guid"]
        r = site.save_episode({"orig_slug": slug, "new_slug": "renamed", "title": "Renamed", "number": 7,
                               "date": "2026-02-02T07:00:00+00:00"})
        self.give_audio(site, r["slug"], FAKE_MP3 + b"new recording")
        e = site.find_episode("renamed")
        self.assertEqual(e["guid"], guid)
        item = ET.fromstring(self.feed(site).encode("utf-8")).find("channel/item")
        self.assertEqual(item.find("guid").text, guid)
        self.assertTrue(item.find("enclosure").get("url").endswith("ep007-renamed.mp3"))

    def test_podcast_guid_matches_the_spec_example(self):
        for tool in PODCAST_TOOLS:
            self.assertEqual(load(tool).podcast_guid("https://mp3s.nashownotes.com/pc20rss.xml"),
                             "917393e3-1b1e-5cef-ace4-edaa54e1f810")

    def test_descriptions_are_cut_at_apple_limits(self):
        mod, site = self.show()
        long = "\n\n".join("Paragraph %d with \u00e9 accents and [a link](https://example.com/%d)." % (i, i) for i in range(300))
        site.save_show({"markdown": long})
        self.episode(site, "Long notes", "2026-01-01T06:00:00+00:00", notes=long)
        ch = ET.fromstring(self.feed(site).encode("utf-8")).find("channel")
        self.assertLessEqual(len(ch.find("description").text.encode("utf-8")), 4000)
        item = ch.find("item")
        self.assertLessEqual(len(item.find("description").text), 4000)
        self.assertGreater(len(item.find("content:encoded", NS).text), 4000)
        ET.fromstring("<x>%s</x>" % item.find("description").text)  # cut at a tag boundary, tags closed

    def test_feed_lock_asks_and_leaves_a_redirecting_feed(self):
        _, site = self.show(home="page")
        slug = self.episode(site, "One", "2026-01-01T06:00:00+00:00")
        audio = site.find_episode(slug)["audio"]["file"]
        old = "https://example.com/podcast/feed.xml"
        self.assertEqual(site.feed_url(), old)
        site.cfg["podcast"]["show"]["feed_lock"] = old
        r = site.save_structure({"home": "podcast", "root_mount": True})
        self.assertTrue(r.get("needs_confirm"))
        self.assertEqual(site.cfg["home"], "page")
        site.save_structure({"home": "podcast", "root_mount": True, "confirm_feed_move": True})
        self.assertEqual(site.feed_url(), "https://example.com/feed.xml")
        self.assertEqual(site.cfg["podcast"]["show"]["feed_lock"], "https://example.com/feed.xml")
        legacy = ET.fromstring((site.root / "podcast" / "feed.xml").read_bytes()).find("channel")
        self.assertEqual(legacy.find("itunes:new-feed-url", NS).text, "https://example.com/feed.xml")
        self.assertTrue((site.root / "audio" / audio).is_file())
        self.assertIn("podcast/feed.xml", site.publish_plan({}, False)["last"])
        r = site.save_settings({"url": "https://other.example.org"})
        self.assertTrue(r.get("needs_confirm"))
        self.assertIn("another domain", r["message"])

    def test_podcastgen_serves_from_the_root(self):
        if "podcastgen.py" not in TOOLS:
            self.skipTest("no podcastgen.py")
        _, site = self.show(tool="podcastgen.py", home="page")
        self.assertEqual((site.cfg["home"], site.cfg["modules"]["podcast"]["mount"]), ("podcast", ""))
        self.assertFalse(any(site.enabled(m) for m in ("blog", "wiki", "library")))
        slug = self.episode(site, "Root episode", "2026-01-01T06:00:00+00:00")
        self.assertEqual(site.feed_url(), "https://example.com/feed.xml")
        for rel in ("index.html", "feed.xml", "episodes/%s.html" % slug):
            self.assertTrue((site.root / rel).is_file(), rel)
        self.assertTrue((site.root / ".podcastgen" / "config.json").is_file())


class PodcastServerTests(PodcastCase):
    def start(self, tool="siteforge.py"):
        app = Running(tool, self.scratch)
        self.addCleanup(app.close)
        site = self.scratch / "site"
        app.api("POST", "open", {"path": str(site), "confirm": True,
                                 "setup": {"title": "Test", "home": "podcast", "modules": ["podcast"]}})
        slug = app.api("POST", "podcast/episodes", {"title": "Upload test"})["slug"]
        return app, site, slug

    def post_raw(self, app, path, length, headers, body=None, chunk=1 << 20):
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=120)
        conn.putrequest("POST", path, skip_host=True)
        conn.putheader("Host", headers.pop("Host", "127.0.0.1:%d" % app.port))
        conn.putheader("Content-Length", str(length))
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        try:
            if body is not None:
                left = length
                block = b"\0" * chunk
                while left > 0:
                    conn.send(block[:min(chunk, left)])
                    left -= min(chunk, left)
            r = conn.getresponse()
            return r.status, r.read()
        except (BrokenPipeError, ConnectionResetError):
            return 0, b""
        finally:
            conn.close()

    def test_upload_limits_token_and_host(self):
        app, site, slug = self.start()
        app.api("PUT", "podcast/show", {"upload_limit_mb": 1})
        path = "/api/podcast/audio?episode=%s&name=x.mp3" % slug
        status, body = self.post_raw(app, path, 5 * 1024 * 1024, {"X-Token": app.token})
        self.assertEqual(status, 413, body)
        self.assertIn(b"upload limit", body)
        status, _ = self.post_raw(app, path, 10, {})
        self.assertEqual(status, 401)
        status, _ = self.post_raw(app, path, 10, {"X-Token": app.token, "Host": "evil.example"})
        self.assertEqual(status, 403)
        incoming = site / ".siteforge" / "podcast" / "incoming"
        self.assertEqual(list(incoming.glob("*.part")) if incoming.exists() else [], [])

    def test_big_upload_streams_to_disk(self):
        status_file = Path("/proc/self/status")
        if not status_file.exists():
            self.skipTest("needs /proc to read peak memory")
        app, site, slug = self.start()

        def peak():
            text = Path("/proc/%d/status" % app.proc.pid).read_text()
            return int(re.search(r"VmHWM:\s+(\d+) kB", text).group(1)) * 1024

        before = peak()
        size = 300 * 1024 * 1024
        status, body = self.post_raw(app, "/api/podcast/audio?episode=%s&name=big.mp3" % slug, size,
                                     {"X-Token": app.token}, body=True)
        self.assertEqual(status, 200, body)
        self.assertLess(peak() - before, 32 * 1024 * 1024)
        app.api("POST", "jobs/media/cancel")


class PodcastPublishTests(PodcastCase):
    def capture(self, mod, site, method):
        """Run a publish with the transfer replaced by a recorder; returns the send order."""
        seen = {}

        def fake(*args):
            seen["send"] = args[-1]

        name = "deploy_sftp" if method == "sftp" else "deploy_ftp"
        real = getattr(mod, name)
        setattr(mod, name, fake)
        try:
            dep = {"method": method, "host": "example.com", "port": "", "user": "u", "path": "/srv/www", "key": "",
                   "remote": "", "branch": "main", "mirror": False, "full_upload": True, "passive": True,
                   "insecure_tls": False}
            plan = site.publish_plan(dep, False)
            plan.pop("after", None)
            site.publish_plan = lambda dep, dry, plan=plan: plan
            mod.run_deploy(site, dep, "", False)
        finally:
            setattr(mod, name, real)
            del site.publish_plan
        return seen["send"]

    def test_feed_goes_last(self):
        mod, site = self.show()
        self.episode(site, "One", "2026-01-01T06:00:00+00:00")
        for method in ("sftp", "ftp"):
            send = self.capture(mod, site, method)
            self.assertEqual(send[-1], "podcast/feed.xml", method)
            self.assertIn("podcast/audio/ep001-one.mp3", send)
        cmds = mod.rsync_commands({"method": "rsync", "host": "h", "user": "u", "path": "/srv/www", "port": "",
                                   "key": "", "mirror": True}, "", Path("/tmp/x"), False,
                                  ["podcast/feed.xml"], ["podcast/audio/"])
        self.assertEqual(len(cmds), 2)
        self.assertIn("--delete", cmds[0])
        self.assertIn("--exclude=/podcast/feed.xml", cmds[0])
        self.assertIn("--exclude=/podcast/audio/", cmds[0])
        self.assertNotIn("--delete", cmds[1])
        i = cmds[1].index("--include=/podcast/")
        self.assertEqual(cmds[1][i:i + 3], ["--include=/podcast/", "--include=/podcast/feed.xml", "--exclude=*"])

    def test_folder_publish_passes_the_live_check(self):
        www = self.scratch / "www"
        base = self.web(www)
        mod, site = self.show(url=base + "/site")
        self.episode(site, "Live one", "2026-01-01T06:00:00+00:00")
        dep = mod.validate_deploy(mod.clean_deploy({"method": "folder", "path": str(www / "site")}, None))
        mod.run_deploy(site, dep, "", False)
        log = "\n".join(mod.JOB["lines"])
        self.assertIn("Live check passed", log)
        self.assertIn("The live feed matches", log)
        show = site.cfg["podcast"]["show"]
        self.assertEqual(show["feed_lock"], base + "/site/podcast/feed.xml")
        self.assertEqual(show["guid"], mod.podcast_guid(show["feed_lock"]))
        self.assertTrue(show["live_seen"])
        self.assertTrue((www / "site" / "podcast" / "feed.xml").is_file())

    def test_media_host_goes_first(self):
        www = self.scratch / "www"
        base = self.web(www)
        mod, site = self.show(url=base + "/site")
        site.save_show({"media_mode": "host", "media_url": base + "/media/",
                        "media_target": {"method": "folder", "path": str(www / "media")}})
        slug = self.episode(site, "Hosted", "2026-01-01T06:00:00+00:00")
        name = site.find_episode(slug)["audio"]["file"]
        dep = mod.validate_deploy(mod.clean_deploy({"method": "folder", "path": str(www / "site")}, None))
        mod.run_deploy(site, dep, "", False)
        log = "\n".join(mod.JOB["lines"])
        self.assertIn("Stage 1 of 2", log)
        self.assertTrue((www / "media" / name).is_file())
        self.assertFalse((www / "site" / "podcast" / "audio").exists())
        enc = ET.fromstring(self.feed(site).encode("utf-8")).find("channel/item/enclosure").get("url")
        self.assertEqual(enc, base + "/media/" + name)
        newer = self.give_audio(site, slug, FAKE_MP3 + b"second take")
        mod.run_deploy(site, dep, "", False)
        self.assertTrue((www / "media" / newer).is_file())
        sent = mod.load_published(mod.MediaRecord(site), site.media_target())
        self.assertEqual(sorted(sent), sorted([name, newer]))  # everything ever sent, until cleaned up
        site.media_clean(site.media_target(), "", [name])
        self.assertFalse((www / "media" / name).exists())
        self.assertTrue((www / "media" / newer).is_file())
        (www / "media" / newer).unlink()
        with self.assertRaises(mod.ApiError):
            mod.run_deploy(site, dep, "", False)

    def test_headless_publish(self):
        www = self.scratch / "www"
        mod, site = self.show(url=self.web(www) + "/out")
        self.episode(site, "Cron", "2026-01-01T06:00:00+00:00")
        out = www / "out"
        site.cfg["deploy"] = mod.validate_deploy(mod.clean_deploy({"method": "folder", "path": str(out)}, None))
        site.save_cfg()
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.scratch / "config"), HOME=str(self.scratch / "home"))
        r = subprocess.run([sys.executable, str(REPO / "siteforge.py"), str(site.root), "--publish"],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((out / "podcast" / "feed.xml").is_file())
        self.assertIn("Done.", r.stdout)


class PodcastMediaTests(PodcastCase):
    def upload(self, site, slug, data_path):
        """Put a file where an upload lands and process it the way the media job does."""
        inc = site.incoming_dir()
        inc.mkdir(parents=True, exist_ok=True)
        orig = inc / ("test.orig" + data_path.suffix)
        shutil.copy(data_path, orig)
        e = site.find_episode(slug)
        e["media"] = {"state": "queued", "message": "", "log": [], "original": orig.name, "name": data_path.name}
        site.pod_process(slug)
        return site.find_episode(slug)

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg is not installed")
    def test_wav_is_normalized_to_mp3_with_tags(self):
        mod, site = self.show()
        wav = self.scratch / "tone.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-y", str(wav)],
                       check=True)
        slug = site.save_episode({"title": "Tone test", "number": 4, "summary": "A test tone."})["slug"]
        e = self.upload(site, slug, wav)
        self.assertEqual(e["media"]["state"], "ready", e["media"])
        a = e["audio"]
        self.assertTrue(a["file"].startswith("ep004-tone-test") and a["file"].endswith(".mp3"))
        self.assertLessEqual(abs(a["lufs"] + 16), 1)
        out = site.audio_dir() / a["file"]
        self.assertEqual(out.stat().st_size, a["bytes"])
        self.assertEqual(e["duration"], 20)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(out), "-af",
                            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True)
        self.assertLessEqual(abs(float(mod.loudnorm_json(r.stderr.splitlines())["input_i"]) + 16), 1)
        tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(out)],
                                         capture_output=True, text=True).stdout)["format"]["tags"]
        self.assertEqual(tags.get("title"), "Tone test")
        self.assertEqual(tags.get("album"), "Example Show")
        self.assertEqual(tags.get("track"), "4")

    def test_without_ffmpeg_mp3_is_kept_and_wav_refused(self):
        mod, site = self.show()
        mp3 = self.scratch / "talk.mp3"
        mp3.write_bytes(FAKE_MP3)
        wav = self.scratch / "talk.wav"
        wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 64)
        slug = site.save_episode({"title": "No tools"})["slug"]
        with NoPath():
            e = self.upload(site, slug, mp3)
            self.assertEqual((site.audio_dir() / e["audio"]["file"]).read_bytes(), FAKE_MP3)
            self.assertFalse(e["audio"]["probed"])
            slug2 = site.save_episode({"title": "Wave file"})["slug"]
            with self.assertRaises(mod.ApiError) as cm:
                self.upload(site, slug2, wav)
        self.assertIn("Install", str(cm.exception))
        self.assertEqual(site.find_episode(slug2)["media"]["state"], "failed")

    def test_artwork_checks(self):
        mod, site = self.show()
        cases = {"good.png": (1400, 1400, False, []), "small.png": (1000, 1000, False, ["fail"]),
                 "wide.png": (3000, 2000, False, ["fail"]), "alpha.png": (1400, 1400, True, ["warn"])}
        for name, (w, h, alpha, _) in cases.items():
            make_png(site.root / "assets" / name, w, h, alpha)
        runs = [("ffprobe", False), ("header reader", True)] if HAS_FFMPEG else [("header reader", True)]
        for label, hide in runs:
            for name, (w, h, alpha, want) in cases.items():
                with self.subTest(reader=label, image=name):
                    if hide:
                        with NoPath():
                            r = site.art_check({"path": "assets/" + name})
                    else:
                        r = site.art_check({"path": "assets/" + name})
                    self.assertEqual((r["info"]["width"], r["info"]["height"], r["info"]["alpha"]), (w, h, alpha))
                    self.assertEqual(sorted({p["level"] for p in r["problems"]}), want)

    def test_transcripts(self):
        mod, site = self.show()
        slug = self.episode(site, "Talk", "2026-01-01T06:00:00+00:00")
        vtt = "WEBVTT\n\n00:00.000 --> 00:02.000\n<v Host>Hello &amp; welcome.\n\n00:02.000 --> 00:04.000\n<v Guest>Thanks.\n"
        r = site.upload_transcript(slug, "talk.vtt", vtt.encode("utf-8"))
        self.assertEqual(r["episode"]["transcript"]["cues"], 2)
        item = ET.fromstring(self.feed(site).encode("utf-8")).find("channel/item")
        t = item.find("podcast:transcript", NS)
        self.assertEqual((t.get("type"), t.get("rel")), ("text/vtt", "captions"))
        page = (site.root / site.episode_rel(slug)).read_text(encoding="utf-8")
        self.assertIn("<b>Host:</b> Hello &amp; welcome.", page)
        self.assertEqual(mod.parse_transcript("1\n00:00:00,000 --> 00:00:01,000\nHi there\n", ".srt"), [("", "Hi there")])
        with self.assertRaises(mod.ApiError):
            site.upload_transcript(slug, "bad.vtt", b"not a transcript")


class PodcastImportTests(PodcastCase):
    def test_podcastgen_site_keeps_every_url(self):
        if "podcastgen.py" not in TOOLS:
            self.skipTest("no podcastgen.py")
        _, old = self.show(tool="podcastgen.py", name="old")
        a = self.episode(old, "First", "2026-01-01T06:00:00+00:00", notes="One.")
        b = self.episode(old, "Second", "2026-02-01T06:00:00+00:00", notes="Two.")
        old.cfg["podcast"]["show"]["feed_lock"] = old.feed_url()
        old.save_cfg()
        old.build()
        before = ET.fromstring(self.feed(old).encode("utf-8")).find("channel")
        mod = load("siteforge.py")
        new_root = self.scratch / "new"
        new_root.mkdir()
        site = mod.Site(new_root)
        site.create({"title": "New", "home": "page", "modules": []})
        r = site.import_site({"path": str(old.root), "settings": True, "home": "podcast", "root_mount": True})
        deadline = time.time() + 30
        while mod.media_busy() and time.time() < deadline:
            time.sleep(0.1)
        site.build()
        self.assertEqual(site.feed_url(), old.feed_url())
        after = ET.fromstring(self.feed(site).encode("utf-8")).find("channel")
        self.assertEqual([i.find("guid").text for i in after.findall("item")],
                         [i.find("guid").text for i in before.findall("item")])
        self.assertEqual([i.find("enclosure").get("url") for i in after.findall("item")],
                         [i.find("enclosure").get("url") for i in before.findall("item")])
        for slug in (a, b):
            f = site.find_episode(slug)["audio"]["file"]
            self.assertEqual((site.audio_dir() / f).read_bytes(), (old.audio_dir() / f).read_bytes())
        self.assertIn("PodcastGen", r["notes"][0])


class GeneratedFileTests(unittest.TestCase):
    def test_podcastgen_is_up_to_date(self):
        if "podcastgen.py" not in TOOLS:
            self.skipTest("no podcastgen.py")
        r = subprocess.run([sys.executable, str(REPO / "tools" / "make_podcastgen.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_editor_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for tool in TOOLS:
            with self.subTest(tool=tool):
                src = (REPO / tool).read_text(encoding="utf-8")
                ui = src[src.index("UI_HTML = r'''"):]
                ui = ui[:ui.index("'''", 20)]
                script = max(re.findall(r"<script>(.*?)</script>", ui, re.S), key=len)
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(script)
                try:
                    r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
                finally:
                    os.unlink(f.name)
                self.assertEqual(r.returncode, 0, r.stderr)


class SharedCodeTest(unittest.TestCase):
    def test_shared_code_matches(self):
        r = subprocess.run([sys.executable, str(REPO / "tools" / "check_shared.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
