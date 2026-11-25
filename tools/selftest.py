#!/usr/bin/env python3
"""Self-test for the generators. Standard library only, no network, no browser.

It starts each tool on a free port in a throwaway folder (with its own config folder, so your
real settings are never touched), talks to it over HTTP the way the editor does, and also
imports the tools as modules to test the publish code directly.

Run from anywhere: python3 tools/selftest.py
Add -v for one line per test.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
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


class SharedCodeTest(unittest.TestCase):
    def test_shared_code_matches(self):
        r = subprocess.run([sys.executable, str(REPO / "tools" / "check_shared.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
