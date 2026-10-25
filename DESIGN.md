# Nova site repair

The audience is developers installing Nova and looking up documentation and release notes.
Keep the existing static HTML, CSS and JavaScript: GitHub Pages serves the `gh-pages`
branch at `/NovaCode/`; release Markdown belongs to `main/changelog/`.

The existing concept is an editorial manual with terminal chrome. Keep its Geist and
Geist Mono fonts, neutral light/dark surfaces, orange accent, pill buttons and 14px panels.
Preserve the landing page composition and product examples. This is a functional repair,
so no new visual concept, framework, loader, decorative animation or invented content is needed.
The hero uses the matched generated day/night artwork in `assets/hero-source/`, based
on `Nova-Hero-light.png` and `Nova-Hero.png`. The originals and prompts remain versioned.
`tools/make_hero_assets.py` encodes responsive WebP images. Native picture
sources follow the OS without JavaScript; `assets/theme.js` also applies a saved theme.
The transparent header uses the theme's text palette and matching face portraits from
the generated artwork. Its compact rounded nav gains glass only after scrolling. Six
primary links show on desktop; the mobile menu retains all nine section links. The
smallest screens hide only the redundant @code suffix so all controls keep touch targets.
Docs and Changelog use the same theme-aware portraits and keep their header controls
within a 320px viewport.
Manual theme switches dissolve and softly refocus the hero over 750ms using native View
Transitions, waiting for the next image to decode. Navigation and the overlapping prompt
stay above the hero snapshots. Older browsers get a short reveal; reduced motion switches
immediately. Repeated toggles supersede the previous transition.
The complete scene drifts at 40% of scroll speed, using CSS scroll-driven animation or
a small script fallback. Pointer or touch input leans the scene; touch release settles
it after 1.4 seconds. Reduced motion shows the intact still frame.
On touch devices served over HTTPS, phone orientation drives the same camera target by
default, with no tilt button. Browsers requiring sensor permission request it on the first
real interaction; denial leaves touch available without repeated prompts. Mobile tilt
and touch never show the cursor glow; it is limited to desktop mouse
input above 900px in both CSS and WebGL. The first valid
reading calibrates the current holding position; screen rotation recalibrates it. Tilt
pauses offscreen, and is disabled by reduced motion. Touch remains the
fallback when orientation is unavailable or permission is denied.
Where WebGL runs, a canvas samples the intact artwork with a softened continuous depth
field from `assets/hero-depth.webp` (red: room, green: coder). Small bounded offsets give
the desk, coder and sky different travel without revealing a painted-over silhouette or
separating cutout edges. Both themes share this rendering path. Theme switches reload the
current image texture without creating additional canvases or observers; the picture stays
visible while loading and after context loss. Rendering runs only while the scene moves.
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
