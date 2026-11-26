# Maintenance

A check to run every couple of months. The five tools are single Python files with no dependencies, so there is no build and nothing to bump. What can drift is Python itself, browsers, the layouts after edits, and for the podcast module, ffmpeg and the directories' rules.

## 1. Check Python still runs them

1. `python3 --version` and make sure it is 3.8 or newer.
2. `python3 -W error::DeprecationWarning -m py_compile siteforge.py sitegen.py wikigen.py storygen.py podcastgen.py`, since new Python releases deprecate things before they remove them. Any warning here is the early notice.
3. `python3 wikigen.py --help` (and the same for the other four) to make sure each one starts.
4. `python3 tools/make_podcastgen.py --check`. PodcastGen is generated from SiteForge, and this fails if `siteforge.py` changed without `python3 tools/make_podcastgen.py` being run afterwards. Run that, then commit both files.
5. `python3 tools/check_shared.py`. The tools carry copies of the same helpers and publish code, and this fails if a fix went into one copy and not the others.
6. `python3 tools/selftest.py`. It starts each tool in a throwaway folder and checks the preview server, the feeds, the podcast feed and media pipeline, and the publish code. Install ffmpeg first, or the media tests are skipped. GitHub Actions runs all of this on every push to `main` and every pull request.

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
2. With SFTP, FTP or the folder method, publish a second time without changes. The log should say 0 of the files changed and send nothing. Then edit one page, publish, and check that one file went.
3. If you add a Content-Security-Policy header on the server, it has to allow inline scripts (`script-src 'self' 'unsafe-inline'`) or the menu helper is blocked. The menus still work without it, they just do not remember or auto-close.

## 6. Podcast

1. `python3 tools/selftest.py`, with ffmpeg installed so the loudness and encoding tests run.
2. Run the live feed through Apple's validation in Podcasts Connect, and open the show in one podcast app to check the art, the episode list and that scrubbing to the middle of an episode works.
3. Compare `APPLE_CATEGORIES` in `siteforge.py` with Apple's current list (podcasters.apple.com, Apple Podcasts categories). Apple adds and renames subcategories now and then. Change the constant, run `python3 tools/make_podcastgen.py`, and add an entry here.
4. Read the end of the last publish log. It should say Pinged Podcast Index with a success message. A run of failures means the endpoint or its rules changed.
5. `ffmpeg -hide_banner -h filter=loudnorm` should still list `measured_I`, `measured_TP`, `measured_LRA`, `measured_thresh`, `offset` and `linear`. The two-pass loudness step depends on all of them.
6. `rclone version`, then **Test connection** under Media in show settings, if the audio lives on a media host.
7. Open each **Open** link in the Directories view and make sure it still lands on that directory's submission page. Fix any that moved in `DIRECTORIES` in `siteforge.py`, then regenerate PodcastGen.
8. Skim Apple's, Spotify's and YouTube's podcaster pages for new feed rules, such as new required tags or changed artwork limits.

## 7. Record it

Add an entry to changes.md for anything you changed, in the same format as the others.
