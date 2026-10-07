# NovaCode landing page: design reference

The authority for what this page looks like, why, and what a future change is
allowed to do to it. Written from `landing/index.html` (1,134 lines) plus
measurements taken from the rendered page, not from intent.

**How to read the numbers.** Values are tagged:

- **Authored**: literal in `index.html` (a token, a declaration, a `clamp()`).
- **Measured**: read back from the live page in Chromium at 1280 x 900, 900 x 900
  and 390 x 844, in both colour schemes. A `clamp()` resolves to a different
  pixel value per viewport, so the authored expression is what travels; the
  measured value is what it looks like today.

Anything in this file that is neither authored nor measured is opinion, and is
labelled as such.

---

## 1. The thesis

> An editorial page wearing a terminal's chrome.

This is the file's own opening comment and it is the correct summary. The page
is a long-form, magazine-shaped argument about a developer tool. The terminal is
its *chrome*: the title bars, the monospace labels, the docked prompt, the
status line at the bottom of the window. The reading experience is editorial;
the surface treatment is a TUI.

Two consequences follow, and everything else in this document is downstream of
them.

**The product is shown, never imitated.** Terminal-like framing must contain
genuine Nova output. Never hand-draw a fake run. The session and shell captures
in `assets/` are the reference. A mock terminal that could plausibly be any
devtool is a lie, and this page is not allowed to tell one.

**Dark mode is a continuation, not an inversion.** The hero is a night scene. In
dark mode the page extends that world: stars on a canvas, a faint grid, an
accent glow, faint scanlines. In light mode the art stays night and the page
below it is daylight. Nothing else about the layout changes.

---

## 2. Brand

> **The brand spec is `BRAND.md` at the repo root.** That file owns the name
> forms, the promise, the limit rule, the voice, the accent decision and the mark,
> in a surface-agnostic form. This section records how *this page* expresses them,
> and stays the authority for the measurements below.

**Name.** Nova, referred to in the interface as `NOVA.CODE`, with the shell
identity `nova@code:~$`.

**One-line promise.** A coding agent that verifies its own work.

**What that means, as the page spells it out.** Nova reads a codebase, plans,
edits, runs the tests, reads the failures, and retries. Generated code is not
the finish line. The repeated word on this page is *proof*: the demo section
runs a real task to a real failure and shows the recovery, and the sections
after it are each backed by a capture, a command, or a named file rather than a
claim.

**Personality.** Five traits, each one visible somewhere on the page:

| Trait | How it shows up |
|---|---|
| Technically credible | Names files, flags, tiers, thresholds and model calls rather than speaking in benefits |
| Autonomous but controlled | "Autonomous doesn't mean uncontrolled." Every tool has a tier, and the tiers are yours to set. |
| Honest about limits | Opt-in costs one extra call; retries stop at three; headless can fail closed |
| Local and inspectable | MIT licensed, runs on your machine and your model, learning written to readable files you can delete |
| Calm | No urgency banners, no "revolutionise", no exclamation marks |

The failure is the tone test. The demo section is built around a run that
*failed twice*. The page is more credible for the failure than it would be for
a clean run.

**Audience.** Developers who live in a terminal. The install section leads with
`uv run nova` and a copy button, not a signup form. Nothing on the page asks
for an email address.

---

## 3. Voice

Short declarative sentences. Technical words used correctly, without glossing.
No hedging adverbs, no intensifiers, no second person when first person would
do. Numbers rather than adjectives.

Headlines are sentence case and are built as direct contrasts:

- "Code written isn't the same as a task finished."
- "Autonomous doesn't mean uncontrolled."
- "Describe the behaviour. Let it find the code."

The `commands` used as chapter eyebrows are literally commands a user could
type: `nova > open code.book`, `nova > man nova`, `> uv run nova`. They are
real, not decorative.

**Trust is stated, not implied.** Every capability that could be overstated has a
sentence next to it that caps the claim:

- Grading costs one extra model call per turn, and is off by default.
- Nova does not retrain your model.
- Improvements are prompts, skills and thresholds on disk, readable and deletable.
- Approval policy is per operation and user-configurable.
- A candidate prompt only replaces the current one after enough runs to be worth
  trusting, and can be rolled back.
- Every step in the demo is a capability described later on the page, except the
  last two, which need a setting turned on.

If you add a feature, add its limit in the same breath. That sentence is the
brand.

---

## 4. Message hierarchy

Thirteen blocks, in this order. The order is the argument; changing it changes
the argument.

| # | Section | Job it does |
|---|---|---|
| 1 | `top` | Full-bleed hero art, `NOVA.CODE` wordmark, docked `uv run nova` prompt, Install button. First screen ends on the Install button, by design. |
| 2 | `statement` | The thesis in prose, plus an inventory of capabilities as a two-column definition list. |
| 3 | `demo` | The proof: one real run, start to finish, including the failure and the fix. |
| 4 | `providers` | A quiet full-width strip of provider logos. Deliberately low contrast. |
| 5 | `verify` | The six-step verification loop, the five grader criteria, and how to turn it on. |
| 6 | `code` | Navigation and comprehension: semantic search, related-code, long sessions, context sizing. |
| 7 | `build` | Execution: subagents, skills, MCP servers. |
| 8 | `safe` | Approval tiers and snapshots, shown through a real session capture. |
| 9 | `reach` | Where Nova reaches: bridges to Discord, Telegram, and the browser. |
| 10 | `grow` | The learning loop: trace, grade, retry, learn. With its limits. |
| 11 | `install` | Provider-agnostic install, one tab per platform, copyable commands. |
| 12 | `faq` | The objections, answered plainly, in an accordion. |
| 13 | `close` | One box. "Install it and give it a real task." MIT licence. Two buttons. |

**Measured** page height at 1280 wide: **10,240px**, thirteen sections plus
footer. Section heights at that width run from 98px (`providers`) to 1,638px
(`safe`). The page is long on purpose. Do not compress it into cards to save
scroll.

**Rhythm.** Editorial statement, then the evidence for it, then the next
statement. Never two statement blocks in a row. Every claim is followed by
something you can look at: a screenshot, a command, a log, or a chip.

---

## 5. Visual principles

**5.1 One accent, nothing else is coloured.**
Orange is Nova's brand accent (`BRAND.md` §7); the tokyo-night blue the product
surfaces ship is a product-surface theme, not a second brand colour.
Verified by walking every element on the page and bucketing computed text
colours by saturation. Result: **five distinct text colours on the entire page**,
of which exactly two are chromatic, and the second is only ever the foreground
on top of the accent:

- dark: `rgb(244,114,63)` (accent) and `rgb(22,17,14)` (on-accent)
- light: `rgb(194,65,12)` and `rgb(255,255,255)`

Forty elements paint in a chromatic colour at all, and that 40 is accent text
plus on-accent button text combined. That is the whole chromatic budget. Success
and failure do not get green and red; they get an accent tick and an accent
cross, and the *word* carries the meaning. The hero artwork contains violet and
amber, and that is fine, because illustration colour is not UI colour.

**5.2 Things you press are pills. Things you look at are 14px panels.**
Authored as `--r-press: 999px` and `--r-view: 14px`. Measured radii across the
live page:

| Radius | Elements | What it is |
|---|---|---|
| `999px` | 33 | Buttons, chips, tabs, pills |
| `14px` | 9 | Panels, cards, terminal windows, the docked prompt |
| `10px` | 3 | Command rows |
| `7px` | 1 | The brand mark image |
| `6px` | 41 | Inline `<code>`, focus rings |
| `50%` | 9 | Status dots, carets, the scroll gauge |

The two smaller values are deliberate: code snippets and the brand mark are
dense objects that should not float. Do not normalise them to 14px.

**5.3 The theme is one decision, made once.**
Measured: every `section`, the `footer`, and the close box paint exactly **two
grounds per theme** (page background and surface). No section inverts, no section
goes dark in a light page. A visitor who has OS light mode never sees a dark
section other than the hero art.

**5.4 Contrast is a floor, not an accident.**
All measured pairs clear WCAG AA with room to spare. Body copy is muted, not
primary, and still clears AAA in dark mode.

| Pair | Dark | Light |
|---|---|---|
| text on bg | 16.36 | 17.14 |
| muted on bg | 7.54 | 6.43 |
| muted on surface | 6.99 | 5.90 |
| accent on bg | 6.73 | 4.96 |
| on-accent on accent | 6.54 | 5.18 |

The weakest is muted on surface in light mode at 5.90. If you add a light-mode
surface surface and put small muted text on it, re-check it.

**5.5 Motion means arrival and feedback. Nothing else.**
See section 11.

**5.6 Real output beats beautiful output.**
The session capture and the shell capture are the two highest-trust assets on
the page. Any new feature gets a real capture if it can.

---

## 6. Colour tokens

Authored on `:root` and overridden in `@media (prefers-color-scheme: dark)`.
The dark values below were also read back from the rendered page and match.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#fafafa` | `#0e0e10` | Page ground |
| `--surface` | `#f0f0f2` | `#17171b` | Panels, section fills |
| `--raised` | `#ffffff` | `#1d1d22` | The lighter fill for small raised controls. Read in exactly three places: the `.copy` button, the selected `.tab`, and the `.proof` chips (mixed 70% toward transparent). |
| `--text` | `#17171a` | `#ececef` | Primary text |
| `--muted` | `#5b5b65` | `#a1a1ac` | Body copy, all secondary text |
| `--line` | `#dedee3` | `#2a2a31` | Hairlines, borders |
| `--accent` | `#c2410c` | `#f4723f` | The single accent |
| `--on-accent` | `#ffffff` | `#16110e` | Text on the accent |
| `--accent-wash` | `#fbe9e0` | `#2a1810` | **Defined but currently unreferenced.** No rule in `index.html` reads it. Treat it as a reserved token, not as existing behaviour. |

Supporting values:

| Token | Light | Dark |
|---|---|---|
| `--shadow` | `0 1px 2px rgb(23 23 26 / .06), 0 18px 40px -18px rgb(23 23 26 / .22)` | `0 1px 2px rgb(0 0 0 / .4), 0 22px 48px -20px rgb(0 0 0 / .7)` |
| `--glow` | `0 0 44px -10px color-mix(in srgb, accent 55%, transparent)` | same expression |
| `--grid` | n/a | `rgb(255 255 255 / .028)` |
| `--r-press` | `999px` | `999px` |
| `--r-view` | `14px` | `14px` |
| `--wrap` | `1240px` | `1240px` |
| `--gutter` | `clamp(20px, 4vw, 40px)` | same |
| `--ease` | `cubic-bezier(.16, 1, .3, 1)` | same |

The accent differs per theme on purpose: `#c2410c` in light, `#f4723f` in dark.
Light-mode orange at dark-theme saturation would glow. Reach for the token, not
for a literal.

**The nav has its own palette.** While it sits over the hero art it hardcodes
light values (`--text: #f3f3f5`, `--accent: #f4723f`, `--line: rgb(255 255 255 / .34)`)
regardless of the visitor's theme, because the art is night in both themes. Once
the page has scrolled past the art, `.nav.stuck` swaps every one of those to the
page tokens and adds a glass background. This is the one place the page
temporarily overrides the theme, and it is scoped to the art by an
IntersectionObserver sentinel rather than a scroll listener.

---

## 7. Typography

Two families, both self-hosted as variable fonts under `assets/fonts/`:

- **Geist** (sans) for prose and headings
- **Geist Mono** for everything that the machine would say

**Measured font distribution on the live page: 267 elements in Geist, 206 in
Geist Mono.** Roughly 43% of all elements are monospace. That ratio is the
taste. Mono is not decoration for code; it is how the interface labels itself.

| Role | Authored | Measured @1280 | Measured @390 |
|---|---|---|---|
| Body (`body`) | `1.0625rem / 1.6`, weight 400 | `17px / 27.2px` | |
| H1 (statement) | `clamp(2.3rem, 3.4vw + .7rem, 3.35rem)`, 620, `-.035em`, lh `1.04` | `65.92px / 68.56px / -2.307px` | `35.2px / 36.61px / -1.232px` |
| H2 | `clamp(1.9rem, 2.6vw + .7rem, 2.9rem)`, 600, `-.03em`, lh `1.08` | `44.48px / 48.04px / -1.334px` | |
| H3 | `1.2rem`, 600 | `19.2px / 24.96px` | |
| Lede | `1.125rem`, max `52ch` | `18px`, max `698.8px`* | |
| Body copy (`.body`) | max `62ch` | `17px`, max `698.8px` | |
| Accordion answer | max `64ch` | | |
| Demo note | `.95rem`, max `62ch` | `15.2px`, max `624.8px` | |
| Eyebrow / chapter | mono | `12.48px`, `+1.123px` tracking | |
| Command line | mono | `15.2px` | |
| Button label | mono `550` | `14.72px` | |
| Proof chip | mono `500` | `12.8px` | |
| Status line | mono `500` | `12.16px` | |

\* `ch` scales with font size, so 52ch of 18px Lede and 62ch of 15.2px note text
both land near 625px. The `ch` unit is the rule; the pixel value is a
consequence.

**Measures.** Never full width. Prose is capped in `ch` units so the line length
stays readable at any viewport. The Lede is the widest thing on the page.

**Accented words inherit.** A highlighted word inside an H1 or H2 takes the
heading's own size and weight and changes only its colour. It never becomes bold
on top of bold, and it never jumps size. Look at "Code written isn't the same as
a task finished" and "Autonomous doesn't mean uncontrolled."

**Mono carries positive tracking.** `+.09em` on eyebrow-scale mono. Small
monospace needs air.

---

## 8. Layout

| Property | Value | Source |
|---|---|---|
| Content width | `1240px` | `--wrap`, measured exactly 1240px at 1280 viewport |
| Gutter | `clamp(20px, 4vw, 40px)` | `--gutter` |
| Section padding | `clamp(72px, 10vw, 136px)` block | `section` |
| Vertical rhythm | Sections do not alternate backgrounds. Ground changes come from panels, not from full-bleed bands. | measured |
| Grid tracks | `minmax(0, 1fr)` | prevents long code from overflowing |
| Horizontal overflow | none at 1280, 900, or 390 | measured `scrollWidth === innerWidth` at all three |

The `minmax(0, 1fr)` discipline is not optional. Without it a single long token
in a code block will push the page wide.

---

## 9. Shape and depth

- **Borders are hairlines.** `1px solid var(--line)`. They define edges; they do
  not create emphasis.
- **Shadows are two-part and restrained.** A 1px contact shadow plus a wide,
  heavily negative-offset drop shadow. If you are adding a third shadow, stop.
- **Blur is used for glass, not for decoration.** The stuck nav
  (`saturate(160%) blur(16px)`) and the docked prompt (`blur(18px) saturate(150%)`)
  are the only blurs, and both sit over the hero art where a solid fill would
  look heavy.
- **Transparency has a reduced mode.** `@media (prefers-reduced-transparency:
  reduce)` drops the nav to an opaque `--bg` and removes `backdrop-filter`.

---

## 10. Components

**Terminal window (`.win`).** `14px` radius, hairline border, `--shadow`. A
`38px` title bar with three `9px` dots filled in `--line`; only the first takes
the accent and `--glow`, so the dots read as decoration rather than as three
status lights. Hovering a window tints its border toward the accent
(`45%` accent mixed into `--line`) and adds `--glow`. Windows are titled with the
real thing: `rate-limit`, `nova`.

**Docked prompt (`.dock`).** Sits `58px` below the hero art with a negative top
margin, so it overlaps the image edge rather than floating after it. Width
`min(780px, 100%)`. Border is `color-mix(accent 35%, --line)`, so the panel is
tinted by the accent without being a second accent colour.

**Buttons.** `.btn` has `min-height: 46px`, `999px` radius, `padding: 0 22px`,
mono `550 .9rem/1`, `gap: 10px`. In the nav they compress to `38px` /
`0 16px` / `.92rem`. The primary button injects a `❯` chevron as a `::before`,
so every primary action reads as a command. Pressing it does
`translateY(1px) scale(.985)`. Primary carries `--glow`; hovering mixes 12% of
the body text colour into the accent, so the button shifts toward the page's own
light or dark (down in light mode, up in dark) instead of glowing brighter.
Quiet buttons are transparent with a `--line` border that goes to `--text` on
hover.

**Proof chips.** Mono, `999px`, `--line` border, prefixed with an accent `✓`.
Used for capability lists. Never used as decoration.

**Tabs.** A roving-tabindex tablist with left/right arrow wrapping. **It opens on
the visitor's platform**, selected by `/Win/.test(navigator.userAgent)` at load.

**Status line.** Fixed `34px` bar at the bottom of the viewport, full width,
mirroring Nova's TUI. Left: `NOVA` in an accent block. Centre: the path of the
chapter currently in view, driven by an IntersectionObserver with a `-45%/-50%`
root margin. Right: a scroll-progress gauge. It is `aria-hidden`, so it is
chrome, not content.

The chapter paths are a small piece of voice in their own right:

| Section | Path shown |
|---|---|
| top | `~/desk` |
| demo | `~/desk/demo.sh` |
| verify | `~/desk/verify.book` |
| code | `~/desk/code.book` |
| build | `~/desk/build.book` |
| safe | `~/desk/safe.book` |
| reach | `~/desk/where.book` |
| grow | `~/desk/grow.book` |
| install | `~/desk/INSTALL.md` |
| faq | `~/desk/man/nova` |

If you add a section, give it a path in this table, or it will inherit whatever
was last in view.

**Copy buttons.** Confirm in place: `Copy` to `Copied`, or `Press Ctrl+C` if the
clipboard write throws, reverting after `1800ms`. No toast, no notification.

**Process diagrams.** Horizontal timelines with a line that fills in `1.4s`.

**Providers strip.** One muted lead-in line, "Runs on the model you already
use", then a wrapping row of provider logos at `26px` height and `opacity: .62`,
going to full opacity on hover. It exists to say "bring your own provider" and
nothing more. Do not add a headline to it.

---

## 11. Motion

Motion is for arrival, and for telling the visitor something happened. It is
never ambient page decoration.

There are **two separate entrance systems**, and they are easy to confuse:

| What | Class | Timing |
|---|---|---|
| Scroll reveal | `.rv` | `0.7s` opacity + transform, `translateY(22px)`, `70ms` per `--i` |
| Keyed entrance | `.rise` | `0.8s`, `translateY(18px)`, `90ms` per `--i` plus `60ms` |
| Hero image | `.art img` | `1.5s`, `scale(1.05)` to none, `50ms` delay |
| Docked prompt | `.dock` | `0.9s`, `translateY(22px)`, `600ms` delay |
| Typed command | `.cmdline .typed` | `55ms` per character, `250ms` delay, `steps(n)` |
| Caret blink | `.caret`, `.statement h1::after` | `1.1s`, `steps(1)`, infinite |
| Process line fill | `.steps::after` | `transform: scaleX(0) to 1`, `1.4s`, `150ms` delay |
| Scroll gauge | `.statusline .gauge i` | `animation-timeline: scroll(root block)`, linear, no duration |
| Easing | all of the above | `cubic-bezier(.16, 1, .3, 1)` |

The scroll gauge is a genuine scroll-driven animation with no time component at
all. Where `animation-timeline: scroll()` is unsupported, the gauge is removed
from the layout entirely rather than left as a dead bar.

Scroll reveals are `IntersectionObserver`-driven: an element gains `.in` once,
with `rootMargin: "0px 0px -8% 0px"` and `threshold: 0.12`, then is unobserved.

The hero art's starfield is the one continuous animation on the page, and it is
dark-mode only: `round(width * height / 5200)` points on a canvas, capped at
`dpr 2`, 12% of them rendered at 2px, 18% tinted to the accent, per-star alpha
`0.25-0.85`, each twinkling on its own period between 900ms and 3500ms. It stops
on `visibilitychange` and reseeds on resize and on a scheme change.

**Known sharp edge:** the 18% accent stars are tinted with the literal string
`"244, 114, 63"`, not with `var(--accent)`. A canvas cannot read a CSS variable,
so the value is duplicated in JS. It currently matches the dark accent
(`#f4723f`) exactly. If you retune the dark accent, change this string in the
same commit or the stars quietly drift away from the theme.

**Under `prefers-reduced-motion: reduce`, measured:** reveal transition duration
is `0s`, revealed elements sit at `opacity: 1`, `scroll-behavior` is `auto` on
both `html` and `body`, and the caret animation is `none`. The page is fully
readable with all animation off.

The mechanism is worth copying rather than reinventing: every rule that hides
content in order to animate it in (`.js .rv{opacity:0}`, `.js .rise{opacity:0}`,
`.js .cmdline .typed{width:0}`) lives **inside** a
`@media (prefers-reduced-motion: no-preference)` block. The `reduce` branch
therefore does not need to un-hide anything. Nothing can be stranded invisible
because the rule that would have hidden it never applied.

---

## 12. Responsive

Measured at three widths:

| | 1280 | 900 | 390 |
|---|---|---|---|
| `.art` aspect ratio | `1672 / 941` | `4 / 5` | `4 / 5` |
| `.art` max-height | `calc(100dvh - 126px)` | `calc(100dvh - 190px)` | same |
| `.art` object-position | `42% 38%` | `14% 40%` | `14% 40%` |
| `.art` rendered box | 1280 x 720 | 900 x 710 | 390 x 488 |
| Nav links | shown | hidden | hidden |
| H1 | 65.92px | | 35.2px |
| Content width | 1240px | 900px | 390px |
| Horizontal overflow | none | none | none |
| Page height | 10,240px | | 11,960px |

The portrait art crop at `≤900px` shifts the image to `14% 40%`, which keeps the
character in frame and lets the wordmark go, rather than centring and cutting
the person in half.

The mobile page is *taller* than the desktop page. That is correct: single
column, the six-step verification flow becomes a vertical sequence with downward
arrows, and feature pairs lose their vertical dividers. The argument is
preserved; only the density changes.

---

## 13. Accessibility

Already implemented, and part of the design rather than an afterthought:

- Skip link to main content.
- Visible focus: `outline: 2px solid var(--accent)` with `outline-offset: 3px`
  and a `6px` border radius on the ring, so it follows the shape of inline code
  chips as well as panels. It is an `outline`, not a shadow, so it never shifts
  layout.
- Semantic landmarks and a real heading order, `h1` once, `h2` per section.
- All controls labelled; the tablist is a proper tablist with arrow-key movement
  and a roving tabindex.
- The status line and decorative dots are `aria-hidden`.
- `prefers-reduced-motion` and `prefers-reduced-transparency` are both honoured,
  not merely declared.
- The hero art is `<picture>` with `fetchpriority="high"` and a preloaded
  `imagesrcset` (`assets/hero-960.webp` at 960w, `assets/hero.webp` at 1672w),
  with intrinsic size `1672 x 941` reserved so the page does not reflow.

---

## 14. Rules for changes

**Do**

- Reach for a token. If you need a colour, size, radius or easing that is not a
  token, you have probably found a real gap; add the token and use it.
- State the limit next to the claim.
- Ship a real capture for a real feature.
- Keep the reading order. Statement, then evidence.
- Re-measure contrast if you introduce a new token pair.
- Run the light and dark passes by eye, at 1280 and at 390.

**Don't**

- Don't add a second accent colour. Not for success, not for error, not for a
  section.
- Don't invent a fake terminal, fake session, or fake diff.
- Don't invert a section, or give a section its own background band.
- Don't widen prose past its `ch` cap, and don't un-cap it.
- Don't add continuous or looping motion.
- Don't add a framework, a build step, or a JS dependency. The page is one
  static file with inline CSS, inline JS, inline SVG, and one `<canvas>`. It must
  keep working when opened straight from disk.
- Don't introduce a radius outside the set in section 5.2.
- Don't put the wordmark or a claim in the hero art region that needs to be
  legible at `14% 40%` on a portrait phone.

**The test for any change.** Does it still read as an editorial page in a
terminal's chrome? If a new element could be pasted into any other developer
tool's site without looking wrong, it does not belong here.

---

## 15. Verifying a change

Do not trust a screenshot description or the source alone. The measurements in
this document came from:

1. Serving `landing/` over plain HTTP (`python -m http.server`) and driving it in
   Chromium.
2. Reading `getComputedStyle` on live elements, in both
   `prefers-color-scheme` values. Note that `clamp()` resolves per viewport, so
   also record the authored expression.
3. Walking every element and bucketing computed `color` by saturation, to check
   the one-accent rule rather than assuming it still holds.
4. Walking up the tree for the nearest opaque background to compute a real
   contrast ratio, rather than pairing tokens on paper.
5. Reading the screenshots. Structure and DOM will not tell you that a page looks
   calm; only looking at it will.

Two mistakes worth not repeating, both made while writing this document:

- `.body` is used by both the prose paragraphs and the mono inventory panel in
  the statement section. A bare `querySelector('.body')` measured the panel
  (13.76px mono) and produced a body-copy figure that was wrong by 3px and the
  wrong typeface entirely. Scope the selector to a section.
- The hero art's `aspect-ratio` lives on the `.art` **container**, not the
  `<img>`. Measuring the image reports its intrinsic ratio (`1672/941`) at every
  width, which looks like the portrait rule is missing when it is working.

---

## 16. Provenance

- Source: `landing/index.html` as of commit `7a717d7` ("landing: lead with
  verification, cut unverified claims").
- That commit is **not** `HEAD`. `HEAD` has since moved to `05a98bd`, but
  `git diff 7a717d7 HEAD -- landing/index.html` is empty, so the page is
  byte-identical and every measurement below still holds. Re-run that diff
  before trusting this document against a newer page; if it is non-empty, the
  figures need re-measuring.
- All colour, type, radius, layout, contrast, responsive and reduced-motion
  figures were measured from the rendered page at that commit, not read off the
  stylesheet. Where an authored value and a rendered value differ (the `clamp()`
  ranges, for example) both are given and labelled as such.
- Assets: `landing/assets/` holds the hero (`hero.webp`, `hero-960.webp`), the
  real captures (`session-hero.png`, `shot-shell.webp`), provider logos, the
  emblem and favicon, the OG image, and the self-hosted Geist and Geist Mono
  variable fonts. Generation helpers live in `landing/tools/`.