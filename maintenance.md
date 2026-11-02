# Maintenance

A check to run every couple of months. The four tools are single Python files with no dependencies, so there is no build and nothing to bump. What can drift is Python itself, browsers, and the layouts after edits.

## 1. Check Python still runs them

1. `python3 --version` and make sure it is 3.8 or newer.
2. `python3 -W error::DeprecationWarning -m py_compile siteforge.py sitegen.py wikigen.py storygen.py`, since new Python releases deprecate things before they remove them. Any warning here is the early notice.
3. `python3 wikigen.py --help` (and the same for the other three) to make sure each one starts.

## 2. Build a test site with each tool

1. `python3 siteforge.py /tmp/sf-test` or whichever folder you like, then pick a homepage in the browser and switch on all three modules.
2. Add a page, a blog post, a wiki section with two articles, and a story with three chapters.
3. Click through every layout under Layout and CSS, with one light theme and one dark theme. Look for headers out of line, text you cannot read, and menus that clash with the theme.
4. Do the same for WikiGen, SiteGen and StoryGen with their own sample content.

## 3. Check the menu behavior

1. In a layout with a side menu, press Hide menu. The menu should go, the content should center, and the button should read Show menu.
2. Open another page. It should stay hidden (this needs the site served over http, or a browser that keeps localStorage for file:// pages).
3. Turn JavaScript off in the browser and press it again. It should still hide and show; it just forgets between pages.
4. In the Handbook wiki layout, open one section, then another. Only one should stay open. Click on the page background and press Escape; both should close the dropdown.
5. Shrink the window under 820px. The Hide menu button should vanish and the Menu button should take over.

## 4. Check browser support

The Hide menu button relies on the CSS `:has()` selector, and the header alignment on `align-items: last baseline`. Both are in every current browser. If a browser you care about drops or breaks either, the fallback is: the button stops hiding the menu, and the tagline sits at the logo's level. Nothing else breaks.

## 5. Publishing

1. Run a publish to your server from the Publish panel and load the live site.
2. If you add a Content-Security-Policy header on the server, it has to allow inline scripts (`script-src 'self' 'unsafe-inline'`) or the menu helper is blocked. The menus still work without it, they just do not remember or auto-close.

## 6. Record it

Add an entry to changes.md for anything you changed, in the same format as the others.
