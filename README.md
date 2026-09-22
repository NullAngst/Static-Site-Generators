# Static Site Generators

Two single-file static site generators, each with a local markdown editor and live preview. You write in your browser, and the tool builds a static website you can host anywhere.

- **SiteGen** (`sitegen.py`) builds general sites and blogs: pages, a post feed, tags, RSS and SEO fields.
- **WikiGen** (`wikigen.py`) builds wiki-style sites: sections, articles, a sidebar with dropdown sections, search, and links between articles.
- **StoryGen** (`storygen.py`) builds fiction sites: short stories, novellas and serials, with the chapter list down the left side.

All three are plain Python with no third-party packages. Each is one file, and that file is the whole program.

## Requirements

- Python 3.8 or newer
- A web browser

Nothing to install beyond Python. Both tools run on Linux, macOS, and Windows.

## Getting started

```sh
git clone https://github.com/NullAngst/Static-Site-Generators.git
cd Static-Site-Generators
python3 sitegen.py ~/my-site       # a site or blog
python3 wikigen.py ~/my-wiki       # a wiki
python3 storygen.py ~/my-stories   # short stories and serials
```

Each command creates the folder if it does not exist, starts a local server on `127.0.0.1`, and opens the editor in your browser. When you are done, the folder holds the finished website. Upload its contents (everything except the hidden `.sitegen/`, `.wikigen/` or `.storygen/` folder) to any static host or web server.

Common options, the same for all three:

```sh
python3 sitegen.py                    # reopen the last site you used
python3 sitegen.py ~/my-site --port 9000
python3 sitegen.py ~/my-site --no-browser
python3 sitegen.py --help
```

The default ports are 8765 for SiteGen, 8766 for WikiGen and 8767 for StoryGen, so they can all run at the same time.

---

# SiteGen

SiteGen produces sites with no JavaScript. The editor runs in the browser and uses JavaScript, but nothing it generates does. The output is HTML, one CSS stylesheet, your images, and optional feed and sitemap files.

## Features

- Pages (home, about, privacy, and custom pages) and a blog with a post feed
- Drafts and publishing, with drafts kept separate from the built site
- Tags, tag pages, and an optional RSS feed
- Per-page and per-post SEO fields, plus site-wide defaults, sitemap.xml and robots.txt
- Eight built-in themes and a CSS editor for anything the themes do not cover
- Image uploads into an `assets/` folder
- Menu editor for showing, hiding, reordering, and adding custom links

## Using it

- **Add page / Blog feed / About page / Define privacy policy** create pages. About and privacy pages start from a template you then edit.
- **Add blog post** opens the post editor. **Save** keeps it as a draft. **Publish** writes it into the blog feed, sorted by date, and creates the feed page if you do not have one. **Unpublish** moves a post back to drafts.
- **Tags** on a post are comma separated. Existing tags appear as one-click chips. On the built site, clicking a tag opens a page listing every post with that tag.
- **Edit CSS** shows global variables at the top and page-specific rules at the bottom, with a theme picker.
- **Edit menu** lists your pages so you can hide any of them from the navigation without deleting the page, and lets you add custom links.
- **Site settings** holds the title, tagline, footer, author, language, site URL, and the RSS and SEO options.

## Output structure

```
my-site/
  index.html              home page
  about.html              and any other pages at the root
  blog/
    index.html            the post feed
    posts/<slug>.html     one file per published post
    tag/<slug>.html       one file per tag in use
  assets/                 uploaded images
  style.css               your stylesheet
  rss, rss.xml            when the RSS feed is enabled
  sitemap.xml             when the sitemap is enabled
  robots.txt              when the sitemap or noindex option is set
  .sitegen/               markdown sources, drafts, and config
```

`style.css` is treated as yours. SiteGen appends starter rules for new pages but never overwrites what is already there.

## SEO

Site-wide settings provide the defaults: a meta description, a default social image, a Twitter/X handle, a toggle to generate `sitemap.xml` and `robots.txt`, and a toggle to discourage indexing for staging.

Each page and post has its own SEO panel with a title-tag override, meta description, social image, canonical URL, and a per-item noindex checkbox. A live search-result preview shows how the entry will look.

Fields fall back so you do not have to fill in every one. A post's meta description uses its own field, then its summary, then its first paragraph. A page's uses its own field, then the site description, then the tagline.

Absolute URLs (canonical, Open Graph, sitemap entries) require the site URL to be set. Without it those tags are left out rather than emitted with a wrong value. SiteGen never overwrites a `robots.txt` or `sitemap.xml` it did not generate.

## Themes

Clean light, Midnight, Terminal, Paper, Nord, Neon night, Solarized light, and Minimal serif.

---

# WikiGen

WikiGen builds a classic wiki layout: the site title across the top, a left sidebar with a search box and one dropdown per section, and the content on the right.

## Features

- Sections (for example "Fun stuff" or "Linux - Technical") holding any number of articles
- A home page with your own text, plus optional section cards and a "Recently updated" list
- A section overview page for each section, with an optional introduction
- Article pages with breadcrumbs, an automatic table of contents, a last-updated date, and previous and next links within the section
- Links between articles: `[[Article title]]`, with missing targets shown in red
- Callout boxes for notes, tips and warnings, which suit step-by-step guides
- Search, with a choice of how it works (see below)
- Drafts, which are saved but left out of the built wiki
- Nine built-in themes and a CSS editor
- A mobile layout where the sidebar folds behind a Menu button
- Meta descriptions, canonical links, sitemap.xml and robots.txt

## Using it

- **New section** creates a section. Its editor sets the name, address, description, and introduction, and lists the section's articles with Up, Down and Sort A to Z controls.
- **New article** opens the article editor. Pick the section, write the title and body, and press **Save** (or Ctrl+S). Changing the section moves the article. Tick **Draft** to keep it out of the built wiki.
- **Import .md** in the article editor loads a Markdown file from your computer. A leading `# Title` line becomes the article title.
- **Home page** edits the front page text.
- **Edit CSS** has a theme picker and the full stylesheet.
- **Settings** holds the title, tagline, footer, URL, search mode, and page options.
- **Rescan files** picks up Markdown written outside the editor (see below).

The preview can be shown at desktop width, phone width, or the width of the pane, so you can check both layouts while you write.

## Writing in your own editor

Articles are ordinary Markdown files, so you can write them in any editor you like:

```
.wikigen/sections/
  fun-stuff/
    _index.md                      section introduction (optional)
    diablo-ii-on-linux.md
  linux-technical/
    portainer-setup.md
```

Drop `.md` files into a section folder, or create a new folder for a new section, then press **Rescan files**. New folders become sections, and new files become articles. Files with spaces or capitals in their names are renamed to web-safe addresses. An article whose file you delete disappears from the wiki on the next rescan.

## Markdown extras

Everything SiteGen supports works here too. On top of that:

```markdown
See [[Portainer setup]] for the container side.
[[Linux - Technical/Backups]]       pick an article when two share a name
[[Portainer setup|this guide]]      change the link text
[[Portainer setup#backups]]         jump to a heading

> [!NOTE]
> Useful information.

> [!WARNING]
> Something that can go wrong.
```

The supported callout types are NOTE, TIP, IMPORTANT, WARNING and CAUTION. Wiki links match on article title or address, and draft articles are not linkable. The editor lists any wiki links on the current article that do not match an article.

## Search and JavaScript

The sidebar dropdowns are plain HTML `<details>` elements, and the mobile menu is a CSS toggle, so navigation works with JavaScript turned off. A search box that searches a static site needs JavaScript, so Settings offers three modes:

- **Built-in** (default). Instant results with highlighted matches and keyboard navigation (press `/` to focus the box). It adds one generated file, `search.js`, which contains the search index. It works offline and from `file://`. With JavaScript disabled, the box hides itself.
- **Web search form.** No JavaScript. The query goes to DuckDuckGo, limited to your wiki's domain. It only finds pages the search engine has already indexed, and it needs the wiki URL set.
- **Off.** No search box, and the generated wiki contains no JavaScript at all.

Built-in search indexes the first 8,000 characters of each article, which keeps `search.js` small on large wikis.

## Output structure

```
my-wiki/
  index.html                    home page
  <section>/index.html          section overview
  <section>/<article>.html      articles
  style.css                     your stylesheet
  search.js                     built-in search only
  assets/                       uploaded images
  sitemap.xml, robots.txt       when enabled
  .wikigen/                     markdown sources, settings, trash
```

WikiGen keeps a list of the files it generated. When an article is renamed, moved, deleted or made a draft, its old page is removed on the next build. Files you placed in the folder yourself are never removed. Deleted sections and articles are moved to `.wikigen/trash` instead of being destroyed.

## Themes

Clean light, Classic wiki, Slate dark, Nord, Gruvbox dark, Dracula, Solarized light, Paper, and Terminal.

---

# StoryGen

StoryGen is built for fiction. The library page lists your stories, each story has its own page with a cover, blurb and chapter list, and every chapter page keeps the chapter list down the left so readers can move through a serial without going back and forth.

Its output has no JavaScript.

## Features

- Stories in two forms: a **single piece** for a standalone short story, or **chapters** for a novella or serial
- A library page with cover, blurb, status, genres, word count and reading time per story
- Chapter pages with the chapter list in a sidebar, chapter numbering, previous and next links, and an optional author's note
- Story details: subtitle, author, cover image, blurb, genres, content notes, series name and number, start and completion dates
- Status per story: Draft, Ongoing, On hiatus, Complete. Completed stories end with "The end"
- Genre pages, so `genre/horror.html` lists everything tagged Horror
- Drafts at both levels: a draft story or a draft chapter is saved but left out of the built site
- Word counts and reading-time estimates, calculated at build time, with an adjustable words-per-minute figure
- Book typography: indented or spaced paragraphs, optional drop capital, and `* * *` scene breaks
- Extra pages (an About page is created for you) that appear in the top navigation
- An optional RSS feed of new chapters, plus meta descriptions, canonical links, sitemap.xml and robots.txt
- Nine themes aimed at reading, and a CSS editor

## Using it

- **New story** asks for a title and whether it is a single piece or has chapters. Its editor holds everything about the story, and for a single piece the same editor holds the story text.
- **New chapter** opens the chapter editor. Pick the story, write, and press **Save** (or Ctrl+S). Changing the story moves the chapter. A live word count sits in the toolbar.
- Chapters are ordered by hand with Up and Down, in the story editor or the chapter editor, so posting order and reading order can differ.
- **Import file** loads a `.md` or `.txt` file from your computer into the chapter editor, which suits drafts written elsewhere.
- **Add page** creates an About, Contact or similar page.
- **Rescan files** picks up stories and chapters written outside the editor, the same way WikiGen does. A folder under `.storygen/stories/` becomes a story and each `.md` file in it becomes a chapter.
- **Settings** holds the site title, author name, paragraph style, drop capital, word count and reading time options, the feed, and the sitemap.

The preview can be shown at desktop width, phone width, or the width of the pane.

## Scene breaks and formatting

A line containing `* * *` (or `***`) becomes a centred scene break. Emphasis is the usual Markdown: `*italic*` and `**bold**`. A blockquote makes an epigraph. If a chapter file starts with a `# Heading` that matches the chapter title, it is dropped, since the page prints the title itself.

## Output structure

```
my-stories/
  index.html                    library page
  about.html                    extra pages
  <story>/index.html            story page: cover, blurb, chapter list
  <story>/<chapter>.html        chapters
  genre/<genre>.html            genre listings
  style.css                     your stylesheet
  assets/                       covers and images
  rss, rss.xml                  when the feed is enabled
  sitemap.xml, robots.txt       when enabled
  .storygen/                    markdown sources, settings, trash
```

Deleted stories and chapters are moved to `.storygen/trash` rather than destroyed, and pages for chapters you delete, rename or turn into drafts are removed from the output on the next build.

## Themes

Paperback, Night reading, Parchment, Manuscript, Ink on white, Midnight blue, Sepia, Noir, and Pulp.

---

# Shared details

## Markdown support

All three tools use the same renderer, covering a practical subset rather than the full CommonMark specification: headings, bold, italic, strikethrough, inline code, links, images, ordered and unordered lists, task lists, blockquotes, tables, and fenced code blocks. Links and images use paths from the site root, for example `about.html` or `assets/photo.png`, and are adjusted automatically for pages in subfolders.

Raw HTML in Markdown passes through an allowlist sanitizer. It keeps common layout and formatting tags, removes `<script>`, `<style>` and other unsafe elements, strips event handler attributes, and rejects `javascript:` and similar URLs, including entity-encoded ones. Embedded HTML outside that allowlist will not render.

## Security notes

All three editors are local authoring tools, not public web services. The server binds to `127.0.0.1` only and is meant to run on the machine you are working on.

Each run creates a random token that every API request must carry, requests with an unexpected Host header are refused, and file serving for previews is confined to the site folder. Built pages opened from the editor are served with a Content-Security-Policy that blocks inline script, so a hostile SVG or pasted HTML cannot reach the editor's API. These measures reduce risk from other local processes and stray requests. They do not make it safe to expose the editor port to a network, so do not do that. The built sites are static files with no server-side component.

## Scope and limitations

Stated plainly so you can decide whether these fit:

- Single author. There are no accounts, no multi-user editing, and no page history beyond the trash folder. Keeping the site folder in git gives you history.
- No plugins, shortcodes or templating language beyond Markdown, themes, and direct CSS.
- Every save rebuilds the whole site. WikiGen and StoryGen skip rewriting unchanged files, which keeps deploys through rsync or git small. Very large sites will rebuild more slowly.
- WikiGen sections are one level deep. There are no sub-sections.
- StoryGen has no reader accounts, comments or view counts, and no EPUB export. Word counts are computed from the rendered text, so they differ slightly from what a word processor reports.
- The Markdown renderer is a subset, as described above.
- Keep the hidden `.sitegen/`, `.wikigen/` or `.storygen/` folder if you want to keep editing. Everything else in the site folder is generated output.

## License

Released under the GNU General Public License version 3. See the [LICENSE](LICENSE) file for the full text. You may use, study, share, and modify these programs, and any distributed derivative work must also be licensed under the GPLv3.
