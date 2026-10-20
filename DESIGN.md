# Nova site repair

The audience is developers installing Nova and looking up documentation and release notes.
Keep the existing static HTML, CSS and JavaScript: GitHub Pages serves the `gh-pages`
branch at `/NovaCode/`; release Markdown belongs to `main/changelog/`.

The existing concept is an editorial manual with terminal chrome. Keep its Geist and
Geist Mono fonts, neutral light/dark surfaces, orange accent, pill buttons and 14px panels.
Preserve the landing page composition and product examples. This is a functional repair,
so no new visual concept, framework, loader, decorative animation or invented content is needed.
The release archive stays still at rest for reading. Native disclosure controls provide feedback.

Navigation must work under a project subdirectory, through history, and from bookmarked
documentation/release fragments. Without JavaScript, documentation and release notes remain readable.
The changelog ships complete HTML from main's Markdown, then checks main for new or edited
releases in the browser. Errors and API rate limits leave the saved archive readable.

Validation covers all documentation topics, internal links and assets, disclosure controls,
old and new deep links, live refresh, failure recovery, mobile layouts and reduced motion.
