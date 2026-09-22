# SiteGen

A single-file static site generator with a local markdown editor and live preview. You write pages and blog posts in your browser, and SiteGen builds a plain HTML and CSS website you can host anywhere.

The generated sites contain no JavaScript. The editor itself runs in the browser and does use JavaScript, but nothing it produces does. The output is static files: HTML, one CSS stylesheet, your images, and optional feed and sitemap files.

## What it does

- Local authoring server that opens an editor in your browser
- Markdown editor with a live preview beside it
- Pages (home, about, privacy, and custom pages) and a blog with a post feed
- Drafts and publishing, with drafts kept separate from the built site
- Tags, tag pages, and an optional RSS feed
- Per-page and per-post SEO fields, plus site-wide defaults, sitemap.xml and robots.txt
- Eight built-in themes and a CSS editor for anything the themes do not cover
- Image uploads into an `assets/` folder
- Menu editor for showing, hiding, reordering, and adding custom links

## Requirements

- Python 3.8 or newer
- A web browser

No third-party packages. It uses the Python standard library only, so there is nothing to install beyond Python itself. It runs on Linux, macOS, and Windows.

## Getting started

Clone the repository or download `sitegen.py` on its own. The single file is the whole program.

```sh
git clone https://github.com/NullAngst/Static-Site-Generators.git
cd SiteGen
python3 sitegen.py ~/my-site
```

That creates the site folder if it does not exist, starts a local server on `127.0.0.1`, and opens the editor in your browser.

Other ways to run it:

```sh
python3 sitegen.py                 # reopen the last site you used
python3 sitegen.py ~/my-site       # open or create a site in that folder
python3 sitegen.py ~/my-site --port 9000
python3 sitegen.py ~/my-site --no-browser
python3 sitegen.py --help
```

The default port is 8765.

## Using it

The editor has a toolbar of actions across the top, a page and post list on the left, the markdown editor in the middle, and a live preview on the right.

- **Add page / Blog feed / About page / Define privacy policy** create pages. About and privacy pages start from a template you then edit.
- **Add blog post** opens the post editor. **Save** keeps it as a draft. **Publish** writes it into the blog feed, sorted by date, and creates the feed page automatically if you do not have one yet. **Unpublish** moves a post back to drafts.
- **Tags** on a post are comma separated. Existing tags show up as one-click chips. Clicking a tag on the built site opens a page listing every post with that tag.
- **Edit CSS** shows global variables at the top and page-specific rules at the bottom, with a theme picker.
- **Edit menu** lists your pages so you can hide any of them from the navigation without deleting the page, and lets you add custom links.
- **Site settings** holds the title, tagline, footer, author, language, site URL, and the RSS and SEO options.

When you are done, the site is already built. The files in your site folder are the finished website. Upload them to any static host or web server.

## Output structure

For a site folder named `my-site`:

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
  robots.txt             when the sitemap or noindex option is set
  .sitegen/               markdown sources, drafts, and config
```

The `.sitegen/` folder holds the markdown sources and configuration so the site can be reopened and edited later. The rest of the folder is the publishable site. `style.css` is treated as yours: SiteGen appends starter rules for new pages but does not overwrite what is already there.

## SEO

Site-wide settings provide the defaults: a meta description, a default social image, a Twitter/X handle, a toggle to generate `sitemap.xml` and `robots.txt`, and a toggle to discourage indexing for staging.

Each page and post has its own SEO panel with a title-tag override, meta description, social image, canonical URL, and a per-item noindex checkbox. A live search-result preview shows how the entry will look, with a description character count.

Fields fall back so you do not have to fill in every one:

- Post meta description: the post's own field, then its summary, then the first paragraph.
- Page and home meta description: the page's own field, then the site description, then the tagline.
- Social image and canonical fall back from the item to the site defaults.

Generated pages include the title, meta description, canonical link, Open Graph tags, Twitter card tags, and `article:published_time` and `article:tag` on posts. The sitemap lists pages, published posts, and tag pages, and excludes anything marked noindex.

Two things to know. Absolute URLs (canonical, Open Graph, sitemap entries) require the site URL to be set in settings. Without it, those tags are left out rather than emitted with a wrong or relative value. SiteGen also will not overwrite a `robots.txt` or `sitemap.xml` that it did not generate, so a hand-written one of yours is left alone, which also means its `Sitemap:` line is not added to your own robots.txt.

## Themes and styling

Eight themes ship built in: Clean light, Midnight, Terminal, Paper, Nord, Neon night, Solarized light, and Minimal serif. Picking a theme sets a block of CSS variables. You can then edit the stylesheet directly for anything else. The editor separates global variables from per-page rules so page-specific tweaks stay contained.

The editor has its own dark mode toggle for the authoring interface. It is independent of the theme your site uses.

## Markdown support

SiteGen uses its own markdown renderer covering a practical subset rather than the full CommonMark specification: headings, bold, italic, inline code, links, images, unordered and ordered lists, task lists, blockquotes, tables, and fenced code blocks. Links and images use paths from the site root, for example `about.html` or `assets/photo.png`.

Raw HTML in markdown is sanitized. `<script>` and `<style>` blocks, `on*` event handler attributes, and `javascript:` URLs are stripped. If you rely on arbitrary embedded HTML or a markdown extension that is not in the list above, it will not render.

## Security notes

SiteGen is a local authoring tool, not a public web service. The editor server binds to `127.0.0.1` only and is meant to run on the machine you are working on.

It includes a per-run token on its API routes, a Host header check, and path-traversal guards on the file serving used for previews. These reduce the risk from other local processes and stray requests. They are not a substitute for treating the editor as local software: do not expose the editor port to a network or the internet. The sites it builds are static files with no server-side component, so the built output has no such surface.

## Scope and limitations

Stated plainly so you can decide whether it fits:

- Single author. There is no multi-user support, accounts, or collaboration.
- No templating language beyond markdown, themes, and direct CSS. There are no plugins or shortcodes.
- The build regenerates the whole site. There is no incremental build or asset pipeline.
- The markdown renderer is a subset, as described above.
- Content lives in `.sitegen/` inside the site folder. Keep that folder if you want to keep editing; the rest is disposable output.

## License

SiteGen is released under the GNU General Public License version 3. See the [LICENSE](LICENSE) file for the full text. In short, you may use, study, share, and modify it, and any distributed derivative work must also be licensed under the GPLv3.
