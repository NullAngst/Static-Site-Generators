# Changes

2026.11.25 - New feature: podcasts, plus security patches and backend fixes

* SiteForge: a fourth module, Podcast. A show page, a page per episode with the browser's own audio player, and an RSS feed with the itunes, content, atom and podcast namespaces that Apple Podcasts, Spotify and every other podcast app read. Podcast can be the homepage and can be served from the site root.
* Show settings: title, author, About the show, language, a primary and a secondary category from Apple's list, explicit, episodic or serial, copyright, owner name and email, artwork, show complete, keep out of directories, locked against imports, up to three support links.
* Episodes: drafts, publishing, scheduled dates, episode and season numbers, full, trailer and bonus types, summaries, episode art, show notes in Markdown, chapters, transcripts and the SEO panel. Each episode's GUID is fixed when it is first saved and never changes.
* Audio: uploads stream to disk outside the editor's lock with a progress bar, up to 2 GB by default and adjustable. The media job probes the file with ffprobe, normalizes loudness to -16 LUFS in two ffmpeg passes, encodes to MP3 or AAC in M4A, and writes tags, cover art and chapters into the file. Without ffmpeg, MP3 and M4A are used as uploaded. A replaced recording gets a new file name, and the old one waits under Unused audio.
* Artwork is checked against Apple's rules (square JPG or PNG, 1400 to 3000 pixels, RGB, no transparency), with or without ffprobe, and Fix artwork writes a 3000 pixel square JPG next to the original.
* Transcripts (.vtt or .srt) go into the feed and onto the episode page as text. Chapters go into a Podcasting 2.0 chapters file, the episode page, the file's tags and Copy for YouTube.
* The feed check runs on every build and holds back Build and publish on a failure unless Publish anyway is ticked. The feed address locks after the first publish; a change that would move it asks first, and the old address keeps a copy of the feed that tells apps to move.
* Publishing: the feed goes out after the audio it points at, a separate media host (rsync, SFTP, FTPS, FTP, a local folder, or rclone for R2, B2 and S3) gets the audio first and is checked over HTTP before the feed is sent, a live check looks at the published audio and feed, Podcast Index is pinged when the feed changed (no key needed), and WebSub is optional. `--publish` builds and publishes from the command line for a cron job.
* Directories: a checklist for Apple Podcasts, Spotify, Amazon Music, YouTube, iHeartRadio, Pocket Casts, Podcast Index and the apps that follow Apple, with the listing addresses becoming subscribe links on the show page.
* Four podcast layouts under Layout and CSS: List, Cards, Spotlight and Archive. They work with every theme and every site layout.
* New tool, PodcastGen (`podcastgen.py`, port 8769, sources in `.podcastgen/`): SiteForge with only the podcast, served from the site root with the feed at `/feed.xml`. It is generated from `siteforge.py` by `tools/make_podcastgen.py`, and its sites import into SiteForge with every address unchanged.
* All four existing tools: the publish code takes optional hooks (hold the publish back, files to send last, folders to leave out, steps before and after), which the podcast uses; the other tools behave as before. rsync can send in two runs, and git takes excluded folders out of the commit.
* All four tools: the editor's preview server streams files instead of reading them whole, answers HEAD requests, and serves byte ranges, so large audio or video files can be scrubbed in the preview.
* All four tools: publishing stages files with hard links in a `staging` folder inside the sources folder instead of copying everything to the system temp folder. It falls back to a copy where links are not possible, and clears anything a crashed publish left behind.
* All four tools: SFTP, FTP, FTPS and the folder method send only files whose size or modification time changed since the last publish to the same target. **Upload every file** in the Publish panel sends them all.
* All four tools: files are written through hidden temporary names, so a publish running while the editor saves can never pick up a half-written file.
* All four tools: **Test connection** for rsync now finds paths that start with `~/`. The path was quoted in a way that stopped the server's shell from expanding `~`, so it always reported MISSING.
* SiteForge and StoryGen: feed items use absolute links and image addresses, as SiteGen's already did. Relative ones broke in feed readers, which resolve them against the post's own address. A `]]>` in a post no longer turns into a literal `]]&gt;` in the feed.
* SiteForge and WikiGen: built pages opened from the editor may run only the generated search.js, named by its exact address, instead of any script file in the site folder.
* SiteForge, WikiGen and StoryGen: **Rescan files** skips symlinked folders and Markdown files, the same rule publishing and importing already follow.
* SiteForge import: an image whose name is already taken by a different file comes over under a new name, and links to it in the imported text are rewritten. Before, it was skipped and the imported text showed the other image.
* New `tools/check_shared.py`, which fails when code the tools share has drifted between copies, and `tools/selftest.py`, which starts each tool in a throwaway folder and tests the preview server, feeds, staging, publishing, the podcast feed, uploads, the media pipeline and the PodcastGen import. A GitHub Actions workflow installs ffmpeg and runs both, plus a compile check and a check that `podcastgen.py` matches `siteforge.py`, on pushes to `main` and on pull requests.

Reasons for changes: a self-hosted podcast without a hosting service, where directories read a feed this tool builds and the audio sits on your own server or storage. The preview server and publish path had to handle large audio files first, and the once-over before the build found the broken feed links, the rsync connection test and the gap in the preview's script policy.

2026.11.01 - UX fixes

* Layouts with a menu down the side get a Hide menu button: WikiGen Classic, Docs, Rail, Portal, Secure portal and Framed; SiteGen Sidebar, Secure portal and Framed; StoryGen chapter pages with the chapter sidebar, Secure portal and Framed; SiteForge's Sidebar site layout, the Classic, Docs and Right sidebar wiki layouts, story chapter sidebars, Secure portal and Framed.
* The button is a CSS checkbox toggle, so it works with JavaScript off. A few lines of inline script remember the choice between pages.
* SiteGen and SiteForge Sidebar layout: the button reads Collapse sidebar and moves the menu into the Classic header on top, so the title and menu stay on the page.
* WikiGen Rail: the rail now uses the theme's sidebar colors with an accent edge instead of the inverted text color, so it matches dark themes. When the rail is hidden, the normal top bar comes back with the site title.
* StoryGen Serial: the header is a light tint of the accent over the theme's header color with an accent rule, instead of a solid accent bar with background-colored text.
* WikiGen and SiteForge Handbook: opening one section dropdown closes the others, and a click outside or Escape closes them. The Book layout's Contents dropdown (StoryGen and SiteForge) behaves the same way.
* WikiGen and StoryGen headers with a logo: the tagline lines up with the title text instead of the top of the logo, and a logo placed above the title is centered over it.

Reasons for changes: side menus took space with no way to hide them, the Rail and Serial layouts used inverted colors that clashed with dark themes, Handbook dropdowns stayed open on top of each other, and the top bar went out of line when a logo sat above the title.
