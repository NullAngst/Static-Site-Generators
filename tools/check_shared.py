#!/usr/bin/env python3
"""Check that code shared between the tools has not drifted apart.

The tools are single files on purpose, so helpers, the Markdown sanitizer and the publish code
are copied into each one. This script compares every top-level function, class and constant
that appears in more than one tool. A name that is identical everywhere today must stay that
way; a fix made in one copy has to be made in all of them. Names listed in ALLOWED_TO_DIFFER
are the ones each tool is meant to have its own version of.

Run from anywhere: python3 tools/check_shared.py
Exits 0 when everything matches, 1 when something drifted.
"""
import ast
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = ["siteforge.py", "sitegen.py", "wikigen.py", "storygen.py", "podcastgen.py"]

# Each tool has its own version of these: its name and folders, its page types, its editor,
# its API, its site class, and a few helpers that grew tool-specific behavior.
ALLOWED_TO_DIFFER = {
    "ABOUT_MD", "APP", "BASE_CSS", "CONFIG_HOME", "CSS_HEADER", "HOME_MD", "Handler", "LAYOUTS",
    "LAYOUT_SHARED_CSS", "LOGO_CSS", "META_DIR", "Markdown", "OWN_GROUP", "PAGE_HELP", "PAGE_MARK",
    "PRIVACY_MD", "RESERVED_SLUGS", "SECURE_TOOL_CSS", "SITE_BOOLS", "STATUSES", "Site", "THEMES",
    "UI_HTML", "VAR_CONSTS", "VAR_ORDER", "_layout_css_base", "_theme_id", "api", "app_state",
    "card_image", "clean_tags", "count_words", "css_stub", "default_cfg", "default_css", "layout_css",
    "layout_list", "main", "nice_words", "open_site", "page_scripts", "plain_text", "rename_path",
    "root_block", "strip_title_h1", "title_from_name",
}


def top_level(path):
    """Name to (hash, line) for every top-level function, class and single-name assignment."""
    src = path.read_text(encoding="utf-8")
    out = {}
    for node in ast.parse(src, filename=str(path)).body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name:
            seg = ast.get_source_segment(src, node) or ""
            out[name] = (hashlib.sha256(seg.encode("utf-8")).hexdigest(), node.lineno)
    return out


def main():
    present = [t for t in TOOLS if (REPO / t).is_file()]
    if len(present) < 2:
        print("Fewer than two tools found next to tools/; nothing to compare.")
        return 0
    tables = {t: top_level(REPO / t) for t in present}
    names = sorted({n for table in tables.values() for n in table})
    drifted, compared = [], 0
    for name in names:
        where = {t: tables[t][name] for t in present if name in tables[t]}
        if len(where) < 2 or name in ALLOWED_TO_DIFFER:
            continue
        compared += 1
        groups = {}
        for t, (digest, line) in where.items():
            groups.setdefault(digest, []).append("%s:%d" % (t, line))
        if len(groups) > 1:
            drifted.append((name, sorted(groups.values(), key=len, reverse=True)))
    if drifted:
        print("Shared code has drifted. Each line is one version of the definition:\n")
        for name, versions in drifted:
            print(name)
            for v in versions:
                print("    " + ", ".join(v))
        print("\nMake the copies match again. If a tool really needs its own version, add the name "
              "to ALLOWED_TO_DIFFER in tools/check_shared.py with that reason in mind.")
        return 1
    print("OK: %d shared definitions match across %s." % (compared, ", ".join(present)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
