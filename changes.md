# Changes

2026.11.25 - Security patches and backend fixes

* All four tools: the editor's preview server streams files instead of reading them whole, answers HEAD requests, and serves byte ranges, so large audio or video files can be scrubbed in the preview.
* All four tools: publishing stages files with hard links in a `staging` folder inside the sources folder instead of copying everything to the system temp folder. It falls back to a copy where links are not possible, and clears anything a crashed publish left behind.
* All four tools: SFTP, FTP, FTPS and the folder method send only files whose size or modification time changed since the last publish to the same target. **Upload every file** in the Publish panel sends them all.
* All four tools: files are written through hidden temporary names, so a publish running while the editor saves can never pick up a half-written file.
* All four tools: **Test connection** for rsync now finds paths that start with `~/`. The path was quoted in a way that stopped the server's shell from expanding `~`, so it always reported MISSING.
* SiteForge and StoryGen: feed items use absolute links and image addresses, as SiteGen's already did. Relative ones broke in feed readers, which resolve them against the post's own address. A `]]>` in a post no longer turns into a literal `]]&gt;` in the feed.
* SiteForge and WikiGen: built pages opened from the editor may run only the generated search.js, named by its exact address, instead of any script file in the site folder.
* SiteForge, WikiGen and StoryGen: **Rescan files** skips symlinked folders and Markdown files, the same rule publishing and importing already follow.
* SiteForge import: an image whose name is already taken by a different file comes over under a new name, and links to it in the imported text are rewritten. Before, it was skipped and the imported text showed the other image.
* New `tools/check_shared.py`, which fails when code the tools share has drifted between copies, and `tools/selftest.py`, which starts each tool in a throwaway folder and tests the preview server, feeds, staging and publishing. A GitHub Actions workflow runs both, plus a compile check, on pushes to `main` and on pull requests.

Reasons for changes: preparing the publish path and preview server for large audio files ahead of the podcast module, fixing broken links in feed readers and the rsync connection test, and closing a gap in the preview's script policy.

2026.11.01 - UX fixes

* Layouts with a menu down the side get a Hide menu button: WikiGen Classic, Docs, Rail, Portal, Secure portal and Framed; SiteGen Sidebar, Secure portal and Framed; StoryGen chapter pages with the chapter sidebar, Secure portal and Framed; SiteForge's Sidebar site layout, the Classic, Docs and Right sidebar wiki layouts, story chapter sidebars, Secure portal and Framed.
* The button is a CSS checkbox toggle, so it works with JavaScript off. A few lines of inline script remember the choice between pages.
* SiteGen and SiteForge Sidebar layout: the button reads Collapse sidebar and moves the menu into the Classic header on top, so the title and menu stay on the page.
* WikiGen Rail: the rail now uses the theme's sidebar colors with an accent edge instead of the inverted text color, so it matches dark themes. When the rail is hidden, the normal top bar comes back with the site title.
* StoryGen Serial: the header is a light tint of the accent over the theme's header color with an accent rule, instead of a solid accent bar with background-colored text.
* WikiGen and SiteForge Handbook: opening one section dropdown closes the others, and a click outside or Escape closes them. The Book layout's Contents dropdown (StoryGen and SiteForge) behaves the same way.
* WikiGen and StoryGen headers with a logo: the tagline lines up with the title text instead of the top of the logo, and a logo placed above the title is centered over it.

Reasons for changes: side menus took space with no way to hide them, the Rail and Serial layouts used inverted colors that clashed with dark themes, Handbook dropdowns stayed open on top of each other, and the top bar went out of line when a logo sat above the title.
