# Static Site Generators

Four single-file static site generators written in Python. Each has a local editor that runs in your browser, with Markdown on one side and a live preview on the other, and each builds a static website you can host anywhere or publish straight to a server.

- **SiteForge** (`siteforge.py`) is the main tool. One site with pages plus any combination of a blog, a wiki and a story library, all of which can be added or removed later.
- **SiteGen** (`sitegen.py`) builds general sites and blogs.
- **WikiGen** (`wikigen.py`) builds wiki-style sites.
- **StoryGen** (`storygen.py`) builds sites for short stories and serials.

SiteForge does everything the other three do, keeps all of their themes, and can import sites made with them. The three standalone tools remain in the repository so existing sites keep working and for anyone who only needs one kind of site.

None of them needs anything beyond Python itself. Each file is the whole program.

## Which one to use

| Program | Builds | JavaScript in the built site | Default port | Sources kept in |
|---|---|---|---|---|
| SiteForge | Pages plus any mix of blog, wiki and library | None, unless the wiki's built-in search is on | 8768 | `.siteforge/` |
| SiteGen | Pages and a blog | None | 8765 | `.sitegen/` |
| WikiGen | A wiki | None, unless built-in search is on | 8766 | `.wikigen/` |
| StoryGen | A story library | None | 8767 | `.storygen/` |

For a new site, use SiteForge. The different default ports let every tool run at the same time.

## Requirements

- Python 3.8 or newer
- A web browser

They run on Linux, macOS and Windows. Publishing to a server over SSH uses programs you may already have, covered under [Publishing to a server](#publishing-to-a-server).

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
```

Each command creates the folder if needed, starts a local server on `127.0.0.1`, and opens the editor in your browser. Options, the same for all four:

```sh
python3 siteforge.py                    # reopen the last site you used
python3 siteforge.py ~/my-site --port 9000
python3 siteforge.py ~/my-site --no-browser
python3 siteforge.py --help
```

When you are done, the site folder holds the finished website. Upload everything except the hidden sources folder (`.siteforge/`, `.sitegen/`, `.wikigen/` or `.storygen/`), or use the built-in **Publish** button.

---

# SiteForge

## First run

A new folder opens a setup screen, where you choose:

- **What the homepage is:** a page you write, a blog feed, a wiki, or a book list.
- **What else to switch on:** any of Blog, Wiki and Library.
- **A theme**, from 26 in three groups.
- **Optionally, an existing SiteGen, WikiGen or StoryGen site to import.**

All of it can be changed later in **Site structure**.

## How a site is put together

Every site has **pages**. On top of that, three **modules** can be switched on:

| Module | What it gives you | Where it lives |
|---|---|---|
| Blog | Posts with drafts, dates, tags, tag pages, a feed page and RSS | `blog/` |
| Wiki | Sections of articles, a left sidebar with dropdown sections, search, `[[wiki links]]`, callouts, tables of contents | `wiki/` |
| Library | Short stories and serials, chapters listed down the left, covers, blurbs, genres, word counts, reading typography | `stories/` |

The **homepage** is either a normal page or the landing page of one module: the blog feed, the wiki home, or the book list.

The top menu lists pages and modules automatically. The **Menu** view reorders them, hides any without deleting it, and adds your own links. Each module's name in the menu and headings can be changed, so the Library can appear as Fiction and the Wiki as Docs.

**Switching a module off** removes its pages from the built site but keeps everything you wrote, so switching it back on restores it. Site structure also has a button that moves a switched-off module's content to the trash folder.

### Serving a module from the site root

When the wiki or the library is the homepage, it can be served from the root of the site instead of its folder. A wiki article then lives at `/linux/portainer.html` instead of `/wiki/linux/portainer.html`. That is the layout WikiGen and StoryGen produce, so an imported site keeps its addresses. The blog always lives in `blog/`, which is where SiteGen puts it.

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

## Importing SiteGen, WikiGen and StoryGen sites

Use the import box on the setup screen to start a new site from an old one, or **Site structure** to add an old site into an existing one. Point it at the folder that contains the hidden `.sitegen`, `.wikigen` or `.storygen` folder. Several old sites can be imported into one SiteForge site.

| From | What is imported |
|---|---|
| SiteGen | Pages with their SEO fields, the blog feed page's text and name, posts and drafts with tags, dates, summaries and SEO fields, custom menu links, and the RSS setting |
| WikiGen | Sections, articles and drafts, the wiki home, and the search, contents and sidebar settings |
| StoryGen | Stories, chapters and drafts, the library page, extra pages, and the reading settings |

From all three, images in `assets/` are copied. The site title, URL and similar settings also come across when importing into a new site, or when you tick that option in Site structure.

**URLs.** Imported into a new site with **Serve it from the site root** ticked, a WikiGen or StoryGen site keeps every URL it had. A SiteGen site keeps its URLs without that option, including the `/rss` and `/rss.xml` feed addresses.

**What does not come across as-is:**

- **The source folder is only read,** never changed.
- **Your old stylesheet** is saved into `.siteforge/imported/` for reference but not applied, because class names changed. Pick one of the retained themes instead.
- **Clashing names:** anything whose name is already taken gets a new address, and the import summary lists each rename.
- **Symlinks** in the source are ignored.

## Themes

All 26 themes from the standalone tools are included, in three groups:

- **Site themes:** Clean light, Midnight, Terminal, Paper, Nord, Neon night, Solarized light, Minimal serif.
- **Wiki themes:** Clean light, Classic wiki, Slate dark, Nord, Gruvbox dark, Dracula, Solarized light, Paper, Terminal.
- **Reading themes:** Paperback, Night reading, Parchment, Manuscript, Ink on white, Midnight blue, Sepia, Noir, Pulp.

The standalone tools each used their own set of CSS variables, so SiteForge converts every theme to one shared set. Each theme's colours and fonts carry over exactly, and anything a theme never defined is derived from its own palette, such as a sidebar colour for a blog theme. Any theme therefore styles every module.

## Output

```
my-site/
  index.html                 the homepage
  about.html, ...            pages
  blog/                      feed, posts/, tag/, rss.xml
  wiki/                      home, one folder per section, search.js
  stories/                   book list, one folder per story, genre/, rss.xml
  rss, rss.xml               site-wide feed of new posts and chapters
  sitemap.xml, robots.txt    when enabled
  style.css                  your stylesheet
  assets/                    images
  .siteforge/                markdown sources, settings, trash
```

The feeds and the sitemap need the site URL set in Settings. SiteForge keeps a list of the files it generated and removes the ones it no longer produces; files you put in the folder yourself are never touched.

---

# The standalone tools

These are the programs SiteForge grew out of. They work on their own, and each has its own publishing panel.

## SiteGen

Pages and a blog, with no JavaScript in the output.

- **Pages:** home, about and privacy templates, and custom pages.
- **Blog:** a feed page, posts with drafts and publishing, tags with tag pages, and an RSS feed at `/rss` and `/rss.xml`.
- **SEO:** per-page and per-post fields with a live search-result preview, site-wide defaults, `sitemap.xml` and `robots.txt`.
- **Menu:** an editor that hides pages without deleting them and adds custom links.
- **Themes:** eight (the Site group above) and a CSS editor.

Output: pages at the root, and posts and tag pages under `blog/`. Sources are in `.sitegen/`.

## WikiGen

A wiki: the site title across the top, a left sidebar with search and one dropdown per section, and content on the right.

- **Pages:** sections of articles, section overview pages, and a home page with section cards and recent changes.
- **Articles:** breadcrumbs, an automatic table of contents, and previous and next links.
- **Markdown extras:** `[[wiki links]]` between articles and callout boxes.
- **Workflow:** drafts, plus rescanning Markdown written in another editor.
- **Search:** three modes, set in Settings. **Built-in** is instant, via one generated `search.js`. **Web** is a DuckDuckGo form limited to your domain, with no JavaScript. **Off** means the site contains no JavaScript at all. Built-in search indexes the first 8,000 characters of each article.
- **Themes:** nine (the Wiki group above).

Output: `index.html`, then `<section>/index.html` and `<section>/<article>.html`. Sources are in `.wikigen/`.

## StoryGen

Fiction sites, with no JavaScript in the output.

- **Library page:** covers, blurbs, status, genres and word counts.
- **Story pages:** a chapter list, or the full text for a single piece.
- **Chapter pages:** the chapter list in a sidebar, numbering, previous and next links, and author's notes.
- **Typography:** indented or spaced paragraphs, optional drop capitals, `* * *` scene breaks, and reading time estimates.
- **Also:** genre pages, drafts, extra pages, and an RSS feed of new chapters.
- **Themes:** nine (the Reading group above).

Output: `index.html`, then `<story>/index.html` and `<story>/<chapter>.html`, plus `genre/`. Sources are in `.storygen/`.

---

# Shared details

## Markdown

All four use the same Markdown renderer. It covers a practical subset of CommonMark:

- **Text:** headings, bold, italic, strikethrough, inline code, links and images.
- **Blocks:** ordered and unordered lists, task lists, blockquotes, tables and fenced code blocks.

WikiGen and SiteForge add `[[wiki links]]` and callouts (`> [!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]`, `[!CAUTION]`).

Links and images use paths from the site root, such as `about.html` or `assets/photo.png`, and are adjusted automatically for pages in subfolders. Raw HTML passes through an allowlist sanitizer that keeps common layout and formatting tags and removes anything unsafe (see [Security](#security)).

## Themes and CSS

**Edit CSS** shows the theme variables at the top of `style.css` and your own rules at the bottom, with a list of the body classes you can target to style one page or one section. None of the tools overwrites `style.css`; **Apply theme** only replaces the variables block.

## Publishing to a server

Every tool has a **Publish** button that builds the site and uploads it in one step. The panel takes User, Password, Host, Port and Path, and has **Test connection**, **Dry run** (reports what would be sent without sending it), **Build and publish**, and **Stop** while a transfer runs. Progress streams into a log in the panel. Settings are stored per site.

### Methods

- **rsync over SSH** (default, recommended). Only sends what changed, so repeat publishes take a second or two. Needs `rsync` and `ssh` on your machine.
- **SFTP**, using the OpenSSH `sftp` client. Uploads every file each time.
- **FTPS and FTP**, handled by Python itself. FTPS checks the server certificate by default, with an option to accept a self-signed one. Plain FTP sends your password and files in the clear.
- **git push**, for GitHub Pages, GitLab Pages, or a bare repository on your server with a `post-receive` hook that checks the files out into the web root.
- **Copy to a local folder**, for a path on this machine or a mounted share.

### What gets published

Everything in the site folder except hidden files and folders, which covers the sources and any `.git` folder. `.htaccess` and `.well-known/` are published if you have them. **Symlinks are never published**; they are skipped and listed in the log.

Files are copied to a private temporary folder before the transfer, so saving in the editor during an upload cannot change what is sent.

### Passwords

Password logins for rsync and SFTP are handed to `ssh` by the `sshpass` program (openSUSE: `sudo zypper install sshpass`), because OpenSSH will not read a password from anywhere else. An **SSH key** needs no extra program and is the better option: leave Password blank and point the key file box at your private key, or let your agent handle it.

Passwords and tokens never appear on a command line or in the log. There are three ways to supply one:

- **Type it into the panel.** It is used for that run and not kept.
- **Tick Remember the password.** This writes it to `secret.json` in the sources folder, created with permissions `600`. The file is plain text, readable by your user, and kept out of git pushes. A saved password is tied to the method, host, port and user it was saved for, and is never offered to another server.
- **Set an environment variable** before starting the tool: `SITEFORGE_PASSWORD`, `SITEGEN_PASSWORD`, `WIKIGEN_PASSWORD` or `STORYGEN_PASSWORD`.

For git over HTTPS, put the access token in the Password box. URLs with a password in them are refused.

### Removing old files

**Remove files on the server that are no longer part of the site** works differently per method:

- **rsync** mirrors the folder with `--delete`. Hidden files on the server, such as `.htaccess` and `.well-known/` (which certbot uses), are always protected. The first publish to a new target with this turned on asks for confirmation first. Mirroring into `/`, a top-level system folder, `/var/www` itself, or a bare home folder is refused.
- **SFTP, FTP and folder copies** delete only files that the previous publish to the same target uploaded, so they never touch anything else. An emptied folder can be left behind on the server.

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

## License

Released under the GNU General Public License version 3. See the [LICENSE](LICENSE) file for the full text. You may use, study, share and modify these programs, and any distributed derivative work must also be licensed under the GPLv3.
