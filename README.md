# Static Site Generators

Five single-file static site generators written in Python. Each has a local editor that runs in your browser, with Markdown on one side and a live preview on the other, and each builds a static website you can host anywhere or publish straight to a server.

- **SiteForge** (`siteforge.py`) is the main tool. One site with pages plus any combination of a blog, a wiki, a story library and a podcast, all of which can be added or removed later.
- **SiteGen** (`sitegen.py`) builds general sites and blogs.
- **WikiGen** (`wikigen.py`) builds wiki-style sites.
- **StoryGen** (`storygen.py`) builds sites for short stories and serials.
- **PodcastGen** (`podcastgen.py`) builds a podcast site: a show page, episode pages with a player, and the feed podcast apps read.

SiteForge does everything the other four do, keeps all of their themes, and can import sites made with them. The standalone tools remain in the repository so existing sites keep working and for anyone who only needs one kind of site. PodcastGen is SiteForge with only the podcast switched on, generated from `siteforge.py`, so the two never drift apart.

None of them needs anything beyond Python itself. Each file is the whole program.

## Which one to use

| Program | Builds | JavaScript in the built site | Default port | Sources kept in |
|---|---|---|---|---|
| SiteForge | Pages plus any mix of blog, wiki, library and podcast | Menu helper on pages with a side or dropdown menu, plus the wiki's built-in search if on | 8768 | `.siteforge/` |
| SiteGen | Pages and a blog | Menu helper in the Sidebar, Secure portal and Framed layouts | 8765 | `.sitegen/` |
| WikiGen | A wiki | Menu helper, plus built-in search if on | 8766 | `.wikigen/` |
| StoryGen | A story library | Menu helper on pages with a chapter sidebar or the Book Contents dropdown | 8767 | `.storygen/` |
| PodcastGen | A podcast: show page, episode pages and feed | Menu helper on pages with a side or dropdown menu | 8769 | `.podcastgen/` |

The menu helper is a few lines of inline script. It remembers whether you hid the side menu, and it closes an open dropdown menu when you open another, click elsewhere or press Escape. The menus themselves are plain HTML and CSS, so they still work with JavaScript off; they just forget the hidden state on each page and stay open until clicked again.

Every layout with a menu down the side has a **Hide menu** button above the content on wide screens (narrow screens keep the Menu button). In SiteGen's and SiteForge's Sidebar layout it reads **Collapse sidebar** and moves the menu into the Classic header on top. In Secure portal and Framed it hides the menu column at any width.

For a new site, use SiteForge, or PodcastGen for a site that is only a podcast. The different default ports let every tool run at the same time.

## Requirements

- Python 3.8 or newer
- A web browser

They run on Linux, macOS and Windows. Publishing to a server over SSH uses programs you may already have, covered under [Publishing to a server](#publishing-to-a-server).

The podcast module in SiteForge and PodcastGen can use three more programs, all optional:

- **ffmpeg and ffprobe** measure, normalize and encode audio, write tags, and fix artwork. Without them, MP3 and M4A files are used exactly as uploaded and the episode length is typed in by hand.
- **rclone** uploads audio to a media host on Cloudflare R2, Backblaze B2, S3 and the like.

I use apt, so in my case that is `sudo apt install ffmpeg rclone`. Use whichever package manager your system has: `dnf`, `zypper`, `pacman`, `apk`, `emerge` (`media-video/ffmpeg`, `net-misc/rclone`) or Homebrew on macOS. Show settings lists which of them it found.

## Getting started

```sh
git clone https://github.com/NullAngst/Static-Site-Generators.git
cd Static-Site-Generators
python3 siteforge.py ~/my-site
```

The standalone tools start the same way:

```sh
python3 sitegen.py ~/my-blog
python3 wikigen.py ~/my-wiki
python3 storygen.py ~/my-stories
python3 podcastgen.py ~/my-show
```

Each command creates the folder if needed, starts a local server on `127.0.0.1`, and opens the editor in your browser. Options, the same for all five:

```sh
python3 siteforge.py                    # reopen the last site you used
python3 siteforge.py ~/my-site --port 9000
python3 siteforge.py ~/my-site --no-browser
python3 siteforge.py --help
```

SiteForge and PodcastGen also take `--publish`, which builds a site and publishes it with its saved settings without opening the editor, for a cron job. See [Podcast](#podcast).

When you are done, the site folder holds the finished website. Upload everything except the hidden sources folder (`.siteforge/`, `.sitegen/`, `.wikigen/`, `.storygen/` or `.podcastgen/`), or use the built-in **Publish** button.

---

# SiteForge

## First run

A new folder opens a setup screen, where you choose:

- **What the homepage is:** a page you write, a blog feed, a wiki, a book list or a podcast.
- **What else to switch on:** any of Blog, Wiki, Library and Podcast.
- **A theme**, from 27 in four groups.
- **Optionally, an existing SiteGen, WikiGen, StoryGen or PodcastGen site to import.**

All of it can be changed later in **Site structure**.

## How a site is put together

Every site has **pages**. On top of that, four **modules** can be switched on:

| Module | What it gives you | Where it lives |
|---|---|---|
| Blog | Posts with drafts, dates, tags, tag pages, a feed page and RSS | `blog/` |
| Wiki | Sections of articles, a left sidebar with dropdown sections, search, `[[wiki links]]`, callouts, tables of contents | `wiki/` |
| Library | Short stories and serials, chapters listed down the left, covers, blurbs, genres, word counts, reading typography | `stories/` |
| Podcast | A show page, episode pages with a player, chapters and transcripts, and the RSS feed podcast apps read | `podcast/` |

The **homepage** is either a normal page or the landing page of one module: the blog feed, the wiki home, the book list or the show page.

The top menu lists pages and modules automatically. The **Menu** view reorders them, hides any without deleting it, and adds your own links. Each module's name in the menu and headings can be changed, so the Library can appear as Fiction and the Wiki as Docs.

**Switching a module off** removes its pages from the built site but keeps everything you wrote, so switching it back on restores it. Site structure also has a button that moves a switched-off module's content to the trash folder.

### Serving a module from the site root

When the wiki, the library or the podcast is the homepage, it can be served from the root of the site instead of its folder. A wiki article then lives at `/linux/portainer.html` instead of `/wiki/linux/portainer.html`, and the podcast feed at `/feed.xml` instead of `/podcast/feed.xml`. That is the layout WikiGen, StoryGen and PodcastGen produce, so an imported site keeps its addresses. The blog always lives in `blog/`, which is where SiteGen puts it.

Changing the homepage or where a module is served from changes addresses. SiteForge removes pages it no longer generates, so links to the old addresses from elsewhere will break.

## Writing

The preview can be shown at desktop width, phone width, or the width of the editor pane. Everything supports the shared Markdown described under [Markdown](#markdown), plus callouts and `* * *` scene breaks.

When the wiki is on, `[[Title]]` links to anything on the site by its title. Wiki articles are matched first, then wiki sections, then pages, posts and stories. `[[Section/Article]]` picks between two articles with the same name, `[[Title|text]]` changes the link text, and `[[Title#heading]]` jumps to a heading. A link to something that does not exist yet shows in red, and the editor lists it.

**Pages.** Add them from the Pages heading in the sidebar. Typing About or Privacy policy as the title starts from a template. Each page and each post has an SEO panel: title tag, meta description, social image, canonical URL and noindex.

**Blog.** **Save draft** keeps a post off the site, **Publish** adds it to the feed (sorted by date), and **Unpublish** moves it back to drafts. Tags are comma separated, existing tags show as one-click chips, and each tag gets a page listing its posts.

**Wiki.** Sections contain articles. Articles can move between sections, be reordered, or be marked as drafts. Each gets breadcrumbs, a table of contents when it has three or more headings, a last-updated date, and previous and next links. The wiki home shows your introduction, section cards and recently updated articles, and Settings can switch the last two off.

**Library.** A story is either a **single piece** (the story page holds the text) or has **chapters** (the story page lists them, and each chapter keeps the list in a sidebar). Stories have:

- a subtitle, author, cover and blurb
- genres, each with its own listing page
- content notes
- a series name and number
- start and completion dates
- a status: Draft, Ongoing, On hiatus or Complete

Chapters have a date and an optional author's note; word counts and reading time are worked out at build time. Settings chooses indented or spaced paragraphs and whether chapters open with a drop capital.

**Writing outside the editor.** Wiki articles and chapters are plain Markdown files under `.siteforge/wiki/sections/` and `.siteforge/library/stories/`. Add `.md` files or folders there and press **Rescan files**: new folders become sections or stories, new files become articles or chapters, and a leading `# Title` becomes the title. The article, chapter and post editors also have an Import button for a single file.

## Podcast

The podcast module makes a show page, a page per episode with a player, and the RSS feed that Apple Podcasts, Spotify, Amazon Music, YouTube Music, Pocket Casts and every other podcast app read. Directories pull that feed; nothing is uploaded to them, so the job ends at a correct feed on a public address. Video is out of scope: post video to YouTube by hand.

**Show settings** holds everything about the show: title, author, About the show (Markdown, also the intro on the show page), language, a primary and a secondary category from Apple's list, explicit, episodic or serial, copyright, the owner's name and email, and the artwork. The owner email is public in the feed, and Spotify, Amazon and YouTube send their verification codes to it, so an address just for the show is the better choice. The same page has the feed check, the feed address, the audio settings, where the audio lives, and a list of unused audio files.

**Episodes.** **+ Episode** starts one. The editor has the title (keep the episode number out of it; there is a field for that), the address, episode and season numbers, the type (full, trailer or bonus), the date and time, an explicit override, a summary, episode art, chapters, a transcript, the SEO panel and the show notes in Markdown. **Save draft** keeps it off the site, **Publish** puts it in the feed, and an episode dated in the future waits as **Scheduled** until a build after that time. Every episode gets a permanent ID (its GUID) when it is first saved, and renaming, re-dating, renumbering or replacing the audio never changes it, so podcast apps never show an episode twice.

**Audio.** Drop the recording on the audio panel or press **Choose audio**. The upload streams to disk with a progress bar, then the media job takes over:

- With **ffmpeg** installed it measures the loudness and normalizes it to -16 LUFS in two passes, which is Apple's target, then encodes to MP3 (the default, since every app plays it) or AAC in M4A, at 64 kbps mono or 128 kbps stereo unless you change it. WAV, FLAC, AIFF, Ogg and Opus all work.
- **Write tags** puts the title, show, author, episode number, cover art and chapters into the file, for people who download it and play it outside a podcast app.
- Without ffmpeg, MP3 and M4A files are used exactly as uploaded and other formats are refused, with the install line for your system. Without ffprobe the length cannot be measured, so the editor asks you to type it in.
- The file lands in `podcast/audio/` as `ep012-the-flood.mp3`. A replaced recording gets a new name (`-r2`) instead of overwriting the old one, so no app is ever served old audio under a new episode. The old file shows under **Unused audio** in show settings until you move it to the trash.
- If ffmpeg fails, an MP3 or M4A upload is used as it is, the log shows why, and **Retry processing** runs it again.

The upload limit is 2 GB by default and can be changed under **Audio**. The upload needs twice the file size free on disk while it is processed.

**Artwork.** Apple wants a square JPG or PNG, 1400 to 3000 pixels, RGB with no transparency. Show art and episode art are checked against that as soon as you pick them. With ffmpeg, **Fix artwork** writes a 3000 pixel square JPG next to the original, cropped to the center and flattened onto the theme's background, and never touches the original.

**Chapters** are rows of start time, title and an optional link, or paste lines like `04:10 The news`. They go into a Podcasting 2.0 chapters file the feed points at, a list on the episode page, the audio file's tags, and **Copy for YouTube**.

**Transcripts.** Upload a `.vtt` or `.srt` file, such as Whisper output. The feed points at it, and the episode page gets the words under a closed **Transcript** section, readable without a player and visible to search engines.

**Copy for YouTube** puts the title, a plain-text description and the chapter times on the clipboard, ready for YouTube Studio.

**The feed** lives at `podcast/feed.xml`, or `feed.xml` when the podcast is served from the site root. It needs the site URL set in Settings. It carries every published episode (podcast apps would drop older ones from a feed that stopped at 50), and an unchanged site builds a byte-identical feed, so nothing is re-sent or re-pinged for nothing. The **feed check** runs on every build: missing owner email, art that breaks Apple's rules, no category, an audio file that is missing or changed size, and so on. A failure stops **Build and publish** unless **Publish anyway** is ticked.

**The feed address is locked** after the first successful publish, because directories know the show by it. A later change that would move it (the site URL, the homepage, or serving from the root) asks first. Confirm, and the old address keeps getting a copy of the feed that tells apps to move (`itunes:new-feed-url`) on every build. When the domain itself changes, the old server has to keep serving the old feed or redirect it, since this app cannot write there.

**Directories** is a checklist for the one-time submissions, in the order that works best: Apple Podcasts first (Overcast and other apps take shows from Apple's directory), then Spotify, Amazon Music, YouTube (optional, for an audio-only show), iHeartRadio, Pocket Casts and Podcast Index. Each row has the submission link, what it needs and a box for the listing address once the show is live; those addresses become the subscribe links on the show page. The rows stay greyed out until the feed check passes and a publish has seen the feed online. Before the first submission, run the feed through Apple's validation in Podcasts Connect and add the show by its feed URL in the Apple Podcasts app as a test.

**Scheduled episodes and cron.** A static site cannot publish itself at a set time. `python3 siteforge.py ~/my-site --publish` builds and publishes with the saved settings, prints the log and exits non-zero on failure, so a cron job or systemd timer can put scheduled episodes out after their date. It needs an SSH key or a saved password, which is the tradeoff to weigh before turning it on.

## Importing SiteGen, WikiGen, StoryGen and PodcastGen sites

Use the import box on the setup screen to start a new site from an old one, or **Site structure** to add an old site into an existing one. Point it at the folder that contains the hidden `.sitegen`, `.wikigen`, `.storygen` or `.podcastgen` folder. Several old sites can be imported into one SiteForge site, as long as only one of them is a podcast.

| From | What is imported |
|---|---|
| SiteGen | Pages with their SEO fields, the blog feed page's text and name, posts and drafts with tags, dates, summaries and SEO fields, custom menu links, and the RSS setting |
| WikiGen | Sections, articles and drafts, the wiki home, and the search, contents and sidebar settings |
| StoryGen | Stories, chapters and drafts, the library page, extra pages, and the reading settings |
| PodcastGen | Show settings, episodes with their GUIDs, show notes, chapters, transcripts, audio, the directories checklist and listing addresses, the locked feed address, and extra pages |

From all four, images in `assets/` and fonts in `assets/fonts/` are copied. The site title, URL, logo and font settings and similar also come across when importing into a new site, or when you tick that option in Site structure.

**URLs.** Imported into a new site with **Serve it from the site root** ticked, a WikiGen, StoryGen or PodcastGen site keeps every URL it had, the podcast feed included. A SiteGen site keeps its URLs without that option, including the `/rss` and `/rss.xml` feed addresses. A PodcastGen site imported without it moves under `/podcast/`; its old feed address keeps getting a copy of the feed that tells apps to move. Audio is hard-linked when both folders are on the same filesystem and copied otherwise, in the background, since it can be gigabytes.

**What does not come across as-is:**

- **The source folder is only read,** never changed.
- **The layout** the old tool used is matched to the nearest SiteForge layout when you import settings.
- **Your old stylesheet** is saved into `.siteforge/imported/` for reference but not applied, because class names changed. Pick one of the retained themes instead.
- **Clashing names:** anything whose name is already taken gets a new address, and the import summary lists each rename.
- **Symlinks** in the source are ignored.

## Themes

All 26 themes from the standalone tools are included, plus one more, in four groups:

- **Site themes:** Clean light, Midnight, Terminal, Paper, Nord, Neon night, Solarized light, Minimal serif.
- **Wiki themes:** Clean light, Classic wiki, Slate dark, Nord, Gruvbox dark, Dracula, Solarized light, Paper, Terminal.
- **Reading themes:** Paperback, Night reading, Parchment, Manuscript, Ink on white, Midnight blue, Sepia, Noir, Pulp.
- **Retro themes:** Secure portal, the dark gray, red and phosphor palette the Secure portal layout was made with.

The standalone tools each used their own set of CSS variables, so SiteForge converts every theme to one shared set. Each theme's colours and fonts carry over exactly, and anything a theme never defined is derived from its own palette, such as a sidebar colour for a blog theme. Any theme therefore styles every module.

SiteGen, WikiGen and StoryGen now ship the same 27 themes too, with the same values, so a palette looks the same whichever tool you use. Each tool lists its own group first and keeps the short theme names it always had.

## Layouts

A theme sets colours and fonts. A layout changes how the site is put together: where the title and menu sit, whether pages sit in a panel, how the blog feed, the wiki sidebar and the story library are laid out. Any theme works with any layout.

Pick layouts under **Layout and CSS**. The preview updates as you change the dropdowns, and **Use these layouts** saves them. SiteForge has one choice for the site and one per module:

| Choice | Layouts |
|---|---|
| Site | **Classic**, **Sidebar** (title and menu in a fixed left column), **Magazine** (big centered masthead over a ruled menu bar), **Journal** (no panels, narrow column, small header), **Landing** (tinted hero with the menu on top, raised panels), **Secure portal** (a 1998 secure-system site, see below), **Framed** (the Secure portal's page structure with an ordinary look, see below) |
| Blog | **List**, **Cards** (grid with each post's image), **Magazine** (cards with the newest post featured, post pages open with their image), **Archive** (one line per post, grouped by year) |
| Wiki | **Classic**, **Docs** (quiet sidebar, open articles, table of contents in a right-hand rail), **Handbook** (search and sections as a menu bar of dropdowns), **Right sidebar** |
| Stories | **Classic**, **Book** (paper pages with a Contents dropdown, no sidebar), **Bookshelf** (cover wall, chapters as tiles), **Serial** (latest chapters first, navigation above and below each chapter), **Spotlight** (first story featured, story pages open on a blurred cover) |
| Podcast | **List** (rows with number, date, length and summary), **Cards** (a grid with each episode's art, or the show art with the episode number over it), **Spotlight** (the newest episode in a raised panel with large art and a player), **Archive** (one line per episode, grouped by season or year) |

The post image for cards and the post hero is the post's social image field, so set it in the SEO panel. Posts without one get an accent stripe instead.

How it works: the layout's rules are written to `layout.css` on every build, and every page loads it before `style.css`. Layout rules start with a body class such as `body.layout-magazine` or `body.wiki-layout-docs`, so they outrank the base styles in your `style.css`, while a rule you write in `style.css` with the same weight still wins because it loads later. Do not edit `layout.css`; it is rewritten every build.

Classic is the default, and an existing site keeps its old look until you pick something else. The only change to its pages is the extra `layout.css` link and a few body classes.

### Secure portal

Secure portal turns the site into a late-90s page for a secret government office you weren't supposed to find. Red classification banners sit at the top and bottom, a box of warning lines sits over a framed page, the title gets a chrome gradient between a waving flag and a seal, the menu runs down a column on the left, and the bottom has a webring, a hit counter and 88x31 badges. Headings go red, code blocks go phosphor green and blockquotes become dashed alert boxes, so a trace log is just a fenced code block. It is HTML and CSS only, like every other layout.

It is a site layout, so it exists in all four tools. Pick it under **Layout and CSS**, and pair it with the **Secure portal** theme for the look it was made with. Any other theme works too: the theme still sets the page, panel, text, link and font colors.

Everything it shows is set in **Settings** under **Secure portal layout**, and the preview updates as you type:

- **Classification banner**, and whether it repeats at the bottom.
- **Warning box:** each line in the box is its own line on the page, so the line breaks are yours. The first line can be larger and can blink.
- **Title row:** chrome gradient on or off, the flag, the seal, up to three lines of seal text and the caption under it. Empty seal text uses SEAL OF, a star and the site's initials.
- **Menu column:** its heading and the note under it.
- **Under construction strip**, **webring name**, **hit counter** (label, digits and the text after them) and **badges**, one per line. Leave any of these empty to hide it.
- **Frame width** in pixels.
- **Colors** for the banner background, edges and text, the warning text, headings and bullets, highlights, visited links, code text and frame lines.

**Reset these settings** puts every value back to the default. The webring links all point to `#`, the same as half the links on any real 1998 site.

Its settings sit in a section that opens by itself when Secure portal is the layout in use, and stays folded otherwise.

In the wiki and the library, the wiki sidebar and the chapter list move into a box above the article, since there is no room for two columns inside the frame. WikiGen puts its sidebar, search included, in the menu column instead. WikiGen's older **Portal** layout, the one with the big search box, is a different layout and is unchanged.

### Framed

Framed keeps where everything sits in Secure portal and drops the costume. From top to bottom: an optional bar across the page, an optional notice box, then one framed page with the title row on top, the menu in a column on the left, your content on the right and the footer along the bottom. There are no banners, flag, seal, blinking, webring, counter or badges, and every color and font comes from the theme, so it works as an ordinary site with any of the 27 themes.

Set it in **Settings** under **Framed layout**:

- **Bar across the top**, and whether it repeats at the bottom. Empty hides it.
- **Notice box:** each line is its own line, and the first one is bold. Empty hides it.
- **Images left and right of the title**, as `assets/...` paths or full web addresses. They show at up to 160 by 96 pixels and are hidden on phones.
- **Menu heading** and a **note under the menu**.
- **Frame width** in pixels.

It is in all four tools, handles wiki sidebars and chapter lists the same way Secure portal does, and SiteForge keeps it and its settings when importing a site from one of the other three.

## Output

```
my-site/
  index.html                 the homepage
  about.html, ...            pages
  blog/                      feed, posts/, tag/, rss.xml
  wiki/                      home, one folder per section, search.js
  stories/                   book list, one folder per story, genre/, rss.xml
  podcast/                   show page, episodes/, feed.xml, audio/, transcripts/, chapters/
  rss, rss.xml               site-wide feed of new posts and chapters
  sitemap.xml, robots.txt    when enabled
  style.css                  your stylesheet
  layout.css                 the chosen layouts, rewritten on every build
  text.css                   text size and smallest text, only while either is on
  assets/                    images, and fonts/ for uploaded fonts
  .siteforge/                markdown sources, settings, trash
```

The feeds and the sitemap need the site URL set in Settings. SiteForge keeps a list of the files it generated and removes the ones it no longer produces; files you put in the folder yourself are never touched. Podcast audio and transcripts are uploads, so a build never removes them either.

---

# The standalone tools

These are the programs SiteForge grew out of. They work on their own, and each has its own publishing panel.

## SiteGen

Pages and a blog, with no JavaScript in the output apart from the menu helper in the Sidebar, Secure portal and Framed layouts.

- **Pages:** home, about and privacy templates, and custom pages.
- **Blog:** a feed page, posts with drafts and publishing, tags with tag pages, and an RSS feed at `/rss` and `/rss.xml`.
- **SEO:** per-page and per-post fields with a live search-result preview, site-wide defaults, `sitemap.xml` and `robots.txt`.
- **Menu:** an editor that hides pages without deleting them and adds custom links.
- **Themes:** all 27 (Site group first) and a CSS editor.
- **Layouts:** Classic, Sidebar, Magazine (card feed with a featured post and post images), Journal (dated archive by year), Landing (hero band, raised panels, card feed), Secure portal and Framed.

Output: pages at the root, and posts and tag pages under `blog/`. Sources are in `.sitegen/`.

## WikiGen

A wiki: the site title across the top, a left sidebar with search and one dropdown per section, and content on the right.

- **Pages:** sections of articles, section overview pages, and a home page with section cards and recent changes.
- **Articles:** breadcrumbs, an automatic table of contents, and previous and next links.
- **Markdown extras:** `[[wiki links]]` between articles and callout boxes.
- **Workflow:** drafts, plus rescanning Markdown written in another editor.
- **Search:** three modes, set in Settings. **Built-in** is instant, via one generated `search.js`. **Web** is a DuckDuckGo form limited to your domain, with no JavaScript. **Off** means the site contains no JavaScript at all. Built-in search indexes the first 8,000 characters of each article.
- **Themes:** all 27 (Wiki group first).
- **Layouts:** Classic, Docs (search in the top bar, contents in a right rail), Handbook (sections as a dropdown menu bar, centered column), Rail (no top bar, a full-height rail in the theme's sidebar colors with an accent edge), Portal (home page hero with a big search box, sections on the right), Secure portal and Framed (in both, the sidebar becomes the menu column).

Output: `index.html`, then `<section>/index.html` and `<section>/<article>.html`. Sources are in `.wikigen/`.

## StoryGen

Fiction sites, with no JavaScript in the output apart from the menu helper.

- **Library page:** covers, blurbs, status, genres and word counts.
- **Story pages:** a chapter list, or the full text for a single piece.
- **Chapter pages:** the chapter list in a sidebar, numbering, previous and next links, and author's notes.
- **Typography:** indented or spaced paragraphs, optional drop capitals, `* * *` scene breaks, and reading time estimates.
- **Also:** genre pages, drafts, extra pages, and an RSS feed of new chapters.
- **Themes:** all 27 (Reading group first).
- **Layouts:** Classic, Book, Bookshelf, Serial and Spotlight, the same set as SiteForge's Stories layouts, plus Secure portal and Framed.

Output: `index.html`, then `<story>/index.html` and `<story>/<chapter>.html`, plus `genre/`. Sources are in `.storygen/`.

## PodcastGen

SiteForge's podcast module on its own, for a site that is only a show. Everything under [Podcast](#podcast) applies: episodes, the audio panel, artwork checks, chapters, transcripts, the feed check and its locked address, Directories, the media host and `--publish`.

- **Pages:** the show page is the homepage, and extra pages such as about.html sit beside it, with the menu editor.
- **Themes:** all 27 (Site group first).
- **Layouts:** the seven site layouts and the four podcast layouts.

Output: `index.html` (the show page), `episodes/<slug>.html`, `feed.xml`, `audio/`, `transcripts/` and `chapters/`. Sources are in `.podcastgen/`. A PodcastGen folder imports into SiteForge with every address unchanged.

`podcastgen.py` is written by `tools/make_podcastgen.py` from `siteforge.py`. Change `siteforge.py` and run that script; an edit made to `podcastgen.py` directly is lost the next time it runs.

---

# Shared details

## Markdown

All four use the same Markdown renderer. It covers a practical subset of CommonMark:

- **Text:** headings, bold, italic, strikethrough, inline code, links and images.
- **Blocks:** ordered and unordered lists, task lists, blockquotes, tables and fenced code blocks.

WikiGen and SiteForge add `[[wiki links]]` and callouts (`> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]`).

Links and images use paths from the site root, such as `about.html` or `assets/photo.png`, and are adjusted automatically for pages in subfolders. Raw HTML passes through an allowlist sanitizer that keeps common layout and formatting tags and removes anything unsafe (see [Security](#security)).

## Images, logo and fonts

All four tools handle these the same way.

**Images in the text.** The editor toolbar has an **Upload image** button. Put the cursor where the image should go, press it, and pick one or more files. Each one is copied to `assets/` and the Markdown for it, `![name](assets/name.png)`, goes in at the cursor. You can also paste an image from the clipboard or drag image files onto the text. If the cursor sits inside a line of text the image goes in inline, right there. On an empty line, or at the start or end of a line, it becomes its own paragraph so it does not run into the text around it. The alt text starts as the file name; change it between the square brackets.

**Logo.** **Settings > Logo and fonts** has an **Upload logo** button, or you can type an `assets/...` path or a full web address. Then pick where it goes in the header:

- left or right of the site title
- above the site title
- in place of the site title, with the alt text read out instead
- as a banner across the top of the header, with the title still shown below it
- not in the header

Separately you can show it in the footer and use it as the browser tab icon. The header and footer heights are set in pixels. It works with every layout, including Secure portal and Framed, and the preview updates as you change it. Next to the title the logo is treated as decoration so screen readers do not read the name twice.

**Fonts.** **Upload font** takes woff2, woff, ttf or otf files and saves them to `assets/fonts/`. The family name, weight and style are guessed from the file name (`OpenSans-SemiBoldItalic.woff2` becomes Open Sans, 600, italic) and you can change all three. Upload one file per weight or style and give them the same family name, and the browser picks the right file for bold and italic text. Then choose which family to use for body text, headings and the site title, menus and small text, and code. Theme font leaves that part alone.

The picks go into `layout.css` as `@font-face` rules plus an override of the theme's font variables, so they survive a theme change. A `font-family` rule you write in `style.css` still wins.

Things to know:

- **Use woff2 where you can.** It is a fraction of the size of ttf or otf. Font files over 10 MB are refused.
- **Check the license.** Self-hosting a font on a website needs a license that allows web embedding. Fonts from Google Fonts and most other open font projects allow it; many desktop font licenses do not.
- **Removing is not deleting.** Removing a font from the list, or clearing the logo, leaves the file in `assets/`. Delete it there if you want it gone from the published site.

## Text size

Every tool has a **Text size** section in **Settings**. It works the same with every theme and layout, and the preview updates as you change it.

- **Text size** grows or shrinks all text by a percentage, from 70% to 200%. 100% is the theme's own size. Use it when a theme's font reads small.
- **Smallest text** is a floor, from 10 to 24 px. Nothing on the site is drawn smaller than it, including small print like dates, captions, menu notes and badges. Text that is already bigger is left alone.

Most text in the themes and layouts is sized relative to the page's base size, so the percentage mostly works by scaling that. The rest, and the floor, are handled by `text.css`, which the tool writes on every build and every CSS save while either setting is on. It repeats each rule from `layout.css` and `style.css` that sets a font size, with the size wrapped in `max(floor, size)` and pixel sizes multiplied by the percentage. It loads after `style.css`, so it wins. Turn both settings back to 100% and no minimum and the file is removed and no longer linked.

The floor applies to decorative text too. In Secure portal the seal and the 88x31 badges grow to fit bigger text, so a high floor makes them larger than the originals.

## Layout and CSS

**Layout and CSS** (formerly Edit CSS) has the layout picker at the top, then the theme variables at the top of `style.css` and your own rules at the bottom, with a list of the body classes you can target to style one page or one section. None of the tools overwrites `style.css`; **Apply theme** only replaces the variables block. Picking a theme in the dropdown previews it straight away without touching your CSS, and **Apply theme** is what writes it in. The dropdown starts on the theme your stylesheet already uses. `layout.css` belongs to the tool and is rewritten on every build, see [Layouts](#layouts).

## Publishing to a server

Every tool has a **Publish** button that builds the site and uploads it in one step. The panel takes User, Password, Host, Port and Path, and has **Test connection**, **Dry run** (reports what would be sent without sending it), **Build and publish**, and **Stop** while a transfer runs. Progress streams into a log in the panel. Settings are stored per site.

### Methods

- **rsync over SSH** (default, recommended). Only sends what changed, so repeat publishes take a second or two. Needs `rsync` and `ssh` on your machine.
- **SFTP**, using the OpenSSH `sftp` client.
- **FTPS and FTP**, handled by Python itself. FTPS checks the server certificate by default, with an option to accept a self-signed one. Plain FTP sends your password and files in the clear.
- **git push**, for GitHub Pages, GitLab Pages, or a bare repository on your server with a `post-receive` hook that checks the files out into the web root.
- **Copy to a local folder**, for a path on this machine or a mounted share.

### What gets published

Everything in the site folder except hidden files and folders, which covers the sources and any `.git` folder. `.htaccess` and `.well-known/` are published if you have them. **Symlinks are never published**; they are skipped and listed in the log.

Before the transfer the files are hard-linked into a private `staging` folder inside the sources folder, or copied where a link is not possible, so saving in the editor during an upload cannot change what is sent. A link takes no time or space, and it is a true snapshot because the tools always replace a file instead of changing it in place.

SFTP, FTP, FTPS and the local folder method send only the files whose size or modification time changed since the last publish to the same target, so a repeat publish only sends what you edited. The first publish to a target sends everything, and so does ticking **Upload every file**, which is the thing to do if files on the server were changed or removed by hand. rsync makes the same comparison on its own.

### Passwords

Password logins for rsync and SFTP are handed to `ssh` by the `sshpass` program (openSUSE: `sudo zypper install sshpass`), because OpenSSH will not read a password from anywhere else. An **SSH key** needs no extra program and is the better option: leave Password blank and point the key file box at your private key, or let your agent handle it.

Passwords and tokens never appear on a command line or in the log. There are three ways to supply one:

- **Type it into the panel.** It is used for that run and not kept.
- **Tick Remember the password.** This writes it to `secret.json` in the sources folder, created with permissions `600`. The file is plain text, readable by your user, and kept out of git pushes. A saved password is tied to the method, host, port and user it was saved for, and is never offered to another server.
- **Set an environment variable** before starting the tool: `SITEFORGE_PASSWORD`, `SITEGEN_PASSWORD`, `WIKIGEN_PASSWORD`, `STORYGEN_PASSWORD` or `PODCASTGEN_PASSWORD`.

For git over HTTPS, put the access token in the Password box. URLs with a password in them are refused.

### Removing old files

**Remove files on the server that are no longer part of the site** works differently per method:

- **rsync** mirrors the folder with `--delete`. Hidden files on the server, such as `.htaccess` and `.well-known/` (which certbot uses), are always protected. The first publish to a new target with this turned on asks for confirmation first. Mirroring into `/`, a top-level system folder, `/var/www` itself, or a bare home folder is refused.
- **SFTP, FTP and folder copies** delete only files that the previous publish to the same target uploaded, so they never touch anything else. An emptied folder can be left behind on the server.

### Podcasts

With the podcast module on, a publish does a few more things, in this order:

1. It refuses to start while audio is still being processed.
2. It builds and runs the feed check. A failure stops here unless **Publish anyway** is ticked.
3. When the audio lives on a separate media host, it sends the audio there first, then asks the media base URL for every episode's file. Any that does not answer with the right size stops the publish before the feed goes anywhere.
4. It sends the site with the feed files last, so the feed never points at audio that is not up yet. rsync does this in two runs (the second sends only the feed and never deletes anything), the other methods sort the feed to the end of the upload, and git sends everything in one commit.
5. The live check asks the server for each new episode's audio and the show art (status 200, the right length, and byte-range support, which Apple needs for seeking) and fetches the live feed to compare it with the one just built. Problems are warnings in the log. When the audio comes back through Cloudflare's proxy, the log points at Cloudflare's CDN terms, which let it limit sites that serve a lot of audio that way; R2 or a DNS-only subdomain for the audio avoids that.
6. When the feed changed, it pings Podcast Index (no key needed) so apps that read from it update quickly, and the WebSub hub if that is switched on. A failed ping is only a warning.
7. The first time, it locks the feed address.

**Where the audio lives** is a show setting. **With the site**, the default, the files sit in `podcast/audio/` and go out with every publish. A **separate media host** takes a media base URL, such as `https://media.example.com/podcast/`, and its own upload target: rsync, SFTP, FTPS, FTP, a local folder, or **rclone**, which covers Cloudflare R2, Backblaze B2, S3 and the rest without the app holding any cloud keys. Set the remote up once with `rclone config`, then give a target such as `r2:my-bucket/podcast`. The media host is never mirrored: old files stay until **Remove unused audio from the media host**, which lists them and asks first. Its password, if any, is saved apart from the site's, and the `SITEFORGE_MEDIA_PASSWORD` or `PODCASTGEN_MEDIA_PASSWORD` environment variable works too.

git is a poor fit for audio: it keeps every version of every recording forever, and GitHub refuses files over 100 MB. The publish panel says so when the audio lives with the site, and a separate media host fixes it.

### Checks and connections

Publish settings live in the sources folder, which can arrive inside a site someone else made, so they are validated before any program runs:

- Hosts must be a plain hostname or IP address.
- User names and paths cannot start with a dash or contain shell characters.
- Ports must be 1 to 65535.
- Paths cannot contain `..`.
- Git remotes cannot use `transport::` helpers.

SSH connections use `StrictHostKeyChecking=accept-new`: an unknown server's key is recorded on first connection, and a changed key is refused. This needs OpenSSH 7.6 or newer.

For rsync, `~/public_html` style paths work. For SFTP and FTP, a leading `~/` means your login folder. Server paths may contain letters, digits and `. _ - / ~ @ +`, but no spaces.

## Security

The editors are local authoring tools. Each binds to `127.0.0.1` only and is meant to run on the machine you are working on; do not expose its port to a network.

- **API access:** every run creates a random token that each API request must carry, and requests with an unexpected Host header are refused.
- **Previews:** file serving is confined to the site folder. Built pages opened from the editor get a Content-Security-Policy that blocks inline script, so a hostile SVG or pasted HTML cannot reach the editor's API.
- **Raw HTML in Markdown** goes through an allowlist sanitizer that removes script and style elements and event handlers, and rejects `javascript:` URLs, including entity-encoded ones.
- **The podcast audio upload** is the one request handled outside the editor's global lock, so a long upload does not freeze the editor. It still has to pass the token and Host checks, it is refused from its Content-Length before any of it is read when it is over the limit, and it streams to disk in 1 MB pieces.
- **The podcast owner email is public.** It is in the feed for anyone to read, which is how directories verify ownership. Use an address just for the show.

The built sites are static files with no server-side component.

## Scope and limitations

Stated plainly so you can decide whether these fit:

- **Single author.** No accounts, multi-user editing or page history beyond the trash folder. Keeping the site folder in git gives you history.
- **One of each module.** SiteForge has one blog, one wiki and one library per site, in the fixed folders `blog/`, `wiki/` and `stories/` (apart from the root option described above). Only the names readers see can be changed.
- **Shallow wikis.** Sections are one level deep, in both SiteForge and WikiGen.
- **Deleting is permanent in SiteGen.** SiteForge, WikiGen and StoryGen move anything you delete into a trash folder inside the sources folder. SiteGen removes deleted pages and posts outright.
- **Full rebuilds.** Every save rebuilds the whole site. SiteForge, WikiGen and StoryGen skip rewriting unchanged files, which keeps rsync and git publishes small, but very large sites rebuild more slowly.
- **Markdown subset.** The renderer does not cover everything in CommonMark.
- **SEO preview.** SiteForge does not have SiteGen's search-result preview in the SEO panel; it keeps the description character counter.
- **Word counts.** They are calculated from the rendered text, so they differ slightly from a word processor's.
- **SSH passwords need `sshpass`**, and saved passwords are plain text readable by your user.
- **No podcast download statistics.** A static site has no server code to count plays. Your web server's access logs have the raw requests, and Apple and Spotify show their own numbers to the owner of a claimed show.
- **No video hosting.** The podcast module is for audio. Post video to YouTube by hand; **Copy for YouTube** fills the description.
- **Scheduled episodes need a publish after their date.** Nothing runs on the server, so an episode dated in the future goes out with the first build and publish after that time. `--publish` from cron or a systemd timer covers it.
- **One show per site.** A site holds one podcast. Use a second site for a second show.

## License

Released under the GNU General Public License version 3. See the [LICENSE](LICENSE) file for the full text. You may use, study, share and modify these programs, and any distributed derivative work must also be licensed under the GPLv3.
