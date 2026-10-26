# Nova site repair

The audience is developers installing Nova and looking up documentation and release notes.
Keep the existing static HTML, CSS and JavaScript: GitHub Pages serves the `gh-pages`
branch at `/NovaCode/`; release Markdown belongs to `main/changelog/`.

The existing concept is an editorial manual with terminal chrome. Keep its Geist and
Geist Mono fonts, neutral light/dark surfaces, orange accent, pill buttons and 14px panels.
Preserve the landing page composition and product examples. This is a functional repair,
so no new visual concept, framework or invented content is needed. The boot screen and
the theme reveal are the only additions.
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
Manual theme switches open the new palette as a circle from the theme toggle over 700ms
using native View Transitions, after the next image set has decoded. The whole page,
including the hero, navigation and prompt, is revealed by the same wipe, so no text is
cross-faded. Older browsers fade the palette in place; reduced motion switches
immediately. Repeated toggles supersede the previous reveal.
The landing page opens with a boot screen once per tab: a terminal line, a hairline
progress bar driven by font, hero and load milestones, and a curtain lift. The boot
screen is hidden without JavaScript and under reduced motion, and a hard timeout means
it can never hold the page.
The hero is four actual full-frame image planes: sky/city, room/code panels,
coder/desk, and near foliage. The room, subject and foliage are real RGBA cutouts;
the sky is a complete clean plate behind them. `tools/make_hero_assets.py` encodes
each plane and composites the same assets into the static fallback. All planes share
the same crop and fixed 1.08 scale; only their translations differ. No shader, depth
map, canvas, mesh deformation or pixel displacement is used.
Pointer, touch and phone tilt move foreground planes farther than the background.
One passive scroll handler updates CSS translations at different speeds. A frame of
overscan keeps edges covered. Touch release settles after 1.4 seconds. Reduced motion,
no JavaScript or failed layer loading shows the complete neutral composite.
On touch devices served over HTTPS, phone orientation drives the same camera target by
default, with no tilt button. Browsers requiring sensor permission request it on the first
completed tap (click), when transient user activation exists; denial leaves touch
available without repeated prompts. Listening alone never disables the touch fallback:
only a valid orientation reading gives the sensor control. Mobile tilt
and touch never show the cursor glow; it is limited to desktop mouse
input above 900px. The first valid
reading calibrates the current holding position; screen rotation recalibrates it. Tilt
pauses offscreen, and is disabled by reduced motion. Touch remains the
fallback when orientation is unavailable or permission is denied.
Both themes use the same plane order and motion rules. Theme transitions wait for the
selected image set to decode. The static composite remains visible until every required
plane is ready, including when a layer fails to load.
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
