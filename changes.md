# Changes

2026.11.01 - UX fixes

* Layouts with a menu down the side get a Hide menu button: WikiGen Classic, Docs, Rail, Portal, Secure portal and Framed; SiteGen Sidebar, Secure portal and Framed; StoryGen chapter pages with the chapter sidebar, Secure portal and Framed; SiteForge's Sidebar site layout, the Classic, Docs and Right sidebar wiki layouts, story chapter sidebars, Secure portal and Framed.
* The button is a CSS checkbox toggle, so it works with JavaScript off. A few lines of inline script remember the choice between pages.
* SiteGen and SiteForge Sidebar layout: the button reads Collapse sidebar and moves the menu into the Classic header on top, so the title and menu stay on the page.
* WikiGen Rail: the rail now uses the theme's sidebar colors with an accent edge instead of the inverted text color, so it matches dark themes. When the rail is hidden, the normal top bar comes back with the site title.
* StoryGen Serial: the header is a light tint of the accent over the theme's header color with an accent rule, instead of a solid accent bar with background-colored text.
* WikiGen and SiteForge Handbook: opening one section dropdown closes the others, and a click outside or Escape closes them. The Book layout's Contents dropdown (StoryGen and SiteForge) behaves the same way.
* WikiGen and StoryGen headers with a logo: the tagline lines up with the title text instead of the top of the logo, and a logo placed above the title is centered over it.

Reasons for changes: side menus took space with no way to hide them, the Rail and Serial layouts used inverted colors that clashed with dark themes, Handbook dropdowns stayed open on top of each other, and the top bar went out of line when a logo sat above the title.
