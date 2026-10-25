# Nova site repair

The audience is developers installing Nova and looking up documentation and release notes.
Keep the existing static HTML, CSS and JavaScript: GitHub Pages serves the `gh-pages`
branch at `/NovaCode/`; release Markdown belongs to `main/changelog/`.

The existing concept is an editorial manual with terminal chrome. Keep its Geist and
Geist Mono fonts, neutral light/dark surfaces, orange accent, pill buttons and 14px panels.
Preserve the landing page composition and product examples. This is a functional repair,
so no new visual concept, framework, loader, decorative animation or invented content is needed.
The one exception is the hero parallax the owner asked for: the art is two layers cut from
`Nova-Hero.png` (the room, painted over where the coder sat, and the coder with her desk),
which slide at 40% and 24% of the scroll speed through a CSS scroll-driven animation, with
no script. They stay still and aligned under reduced motion or without `animation-timeline`.
Under a mouse the two layers also lean away from the cursor (the desk twice as far, the same
amount vertically so only her sides uncover the room) and a pale moonlight follows it. A phone
has no hover, so a finger held on the art does the same and the scene settles back 1.4s after
it lifts. Browsers without scroll-driven animations (Safari before 26, Firefox) get the drift
from a small scroll script instead. Reduced motion gets the still frame.
Where WebGL runs, a canvas takes over once it has drawn: a 3D photo of the same two layers
with `assets/hero-depth.webp` (red: the room's depth with the coder painted out; green: hers),
so the moon, city, code panes and desk each move by their own distance with the pointer and
the scroll, and the moonlight lands harder on near surfaces. It renders only while something
moves, caps depth at the laptop so the blurred edge plants don't smear, and leaves the CSS
layers (with their alt text) underneath as the fallback.
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
