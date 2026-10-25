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

Feature content follows the root `FEATURES.md` inventory (v1.0.99): each landing section
names its command, its requirement and where it stops, and preview or experimental work is
labelled. New sections reuse the existing furniture (cmdline, `.win` panels with a bar,
ledger, pair, split, tree, caps, install tabs) rather than inline styles or extra colours.
The contents panel links every feature section. Docs carry the full catalog on the
`#page-features` topic.

The light and dark palettes follow the system until the visitor uses the theme toggle in
the top bar; `assets/theme.js` saves that choice (`nova-theme` in localStorage) and applies
it before first paint on every page, including the 404. CSS keys dark rules off both
`[data-theme="dark"]` and the system query, so pages stay themed without script.

Validation covers all documentation topics, internal links and assets, disclosure controls,
old and new deep links, live refresh, failure recovery, mobile layouts and reduced motion.
