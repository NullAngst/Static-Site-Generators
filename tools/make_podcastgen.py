#!/usr/bin/env python3
"""Write podcastgen.py from siteforge.py.

PodcastGen is SiteForge with one module, the podcast, served from the site root. Rather than
keep a second copy of nine thousand lines in step by hand, this script copies siteforge.py and
changes the handful of lines that make it PodcastGen: its name, its sources folder, its config
folder, the module it is locked to and its port. Everything else, the editor included, decides
what to show from those values while it runs.

Run from anywhere:
    python3 tools/make_podcastgen.py           write podcastgen.py
    python3 tools/make_podcastgen.py --check   exit 1 when podcastgen.py is out of date (for CI)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DOC = '''"""
PodcastGen: a local podcast site generator. It makes a show page, a page per episode with a
player, and the RSS feed that Apple Podcasts, Spotify and every other podcast app read.

Run it:
    python3 podcastgen.py                       opens the last show you used
    python3 podcastgen.py ~/my-show             opens (or creates) a show in that folder
    python3 podcastgen.py ~/my-show --publish   builds and publishes with the saved settings
    python3 podcastgen.py --help

It needs Python 3.8 or newer and nothing else (standard library only). It starts a small web
server on 127.0.0.1 and opens the editor in your browser. ffmpeg and ffprobe are optional: with
them it measures, normalizes and encodes audio, and without them MP3 and M4A files are used as
they are. rclone is optional too, for a media host on Cloudflare R2, Backblaze B2 or S3.

Folder layout (inside the folder you choose):
    index.html                      the show page
    episodes/                       one page per episode
    feed.xml                        the podcast feed
    audio/                          episode audio, as uploaded or processed; never deleted by a build
    transcripts/, chapters/         transcripts and chapter files
    about.html, ...                 extra pages
    style.css                       your stylesheet (edited through "Edit CSS", never overwritten)
    assets/                         show art, episode art and other images
    .podcastgen/                    your markdown sources and settings

A PodcastGen folder imports into SiteForge with every address unchanged.

This file is generated from siteforge.py by tools/make_podcastgen.py. Change siteforge.py and
run that script; edits made here are lost the next time it runs.
"""'''

# (old, new) pairs; each old line must appear exactly once in siteforge.py.
LINES = [
    ('APP = "SiteForge"', 'APP = "PodcastGen"'),
    ('META_DIR = ".siteforge"', 'META_DIR = ".podcastgen"'),
    ('CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "siteforge"',
     'CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "podcastgen"'),
    ("ONLY_MODULE = None", 'ONLY_MODULE = "podcast"'),
    ("DEFAULT_PORT = 8768", "DEFAULT_PORT = 8769"),
]


def build():
    src = (REPO / "siteforge.py").read_text(encoding="utf-8")
    head, sep, rest = src.partition('"""\n', )
    if not head.startswith("#!") or not sep:
        sys.exit("siteforge.py does not start with a shebang and a docstring.")
    body = rest.split('"""\n', 1)
    if len(body) != 2:
        sys.exit("Could not find the end of siteforge.py's docstring.")
    out = head + DOC + "\n" + body[1]
    for old, new in LINES:
        n = out.count("\n" + old + "\n")
        if n != 1:
            sys.exit("Expected the line %r once in siteforge.py, found it %d times." % (old, n))
        out = out.replace("\n" + old + "\n", "\n" + new + "\n")
    return out


def main():
    out = build()
    dest = REPO / "podcastgen.py"
    if "--check" in sys.argv[1:]:
        current = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        if current != out:
            print("podcastgen.py is out of date. Run: python3 tools/make_podcastgen.py")
            return 1
        print("OK: podcastgen.py matches siteforge.py.")
        return 0
    dest.write_text(out, encoding="utf-8")
    print("Wrote %s (%d lines)." % (dest, out.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
