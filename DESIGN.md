# Nova site: the drawing sheet

The audience is developers installing Nova and looking up documentation and release notes.
GitHub Pages serves the `gh-pages` branch at `/NovaCode/`; release Markdown belongs to
`main/changelog/`.

This file is the binding art direction for the landing page (`index.html`). Everything below
is derived from one idea; if a change cannot be traced to it, it does not belong.

## Concept Card

```
Idea:        The landing page is a technical drawing of the product. Every claim is
             dimensioned on the sheet, and its limit is printed as a tolerance.
Metaphor:    An engineering drawing sheet: title block, edge zone references, dimension
             lines with end ticks, tolerance callouts, detail views, a parts list.
Signature:   The measure. Under each claim a hairline dimension line is drawn to the
             claim's true measured width, and an orange tolerance label sits at the far
             end of it. It re-measures on resize, so the number is real, not decorative.
Emotion:     Calm authority. Someone showing you the tolerances instead of the brochure.
Motion verb: rule. Lines extend from a fixed datum, type lands on the sheet, nothing floats.
Screenshot:  A sheet where an 84px expanded claim is dimensioned by a hairline that ends at
             a small orange "not verified" tolerance.
```

**Why this and not a terminal theme.** Nova's useful distinction is how it keeps work understandable:
other agents join in their own rooms rather than one flattened conversation, memory lives in files you
can inspect, results are reported as observed evidence rather than completion claims, and tool
approvals follow a policy you control. The drawing-sheet form makes those claims and their limits easy
to scan without turning the landing page into a catalog of every capability.

**Anchor fact:** the product delegates to specialist agents and other CLIs (`/rooms`), keeps memory in
readable Markdown with visible recall, verifies against real tool output (`/prime`), and enforces
approvals with hard-coded denials still active. `FEATURES.md` remains the detailed reference for
secondary capabilities and setup requirements.

## Three concepts, scored 1 to 5

| | A. The drawing sheet | B. Session transcript | C. The memory library |
|---|---|---|---|
| Territory | material of the profession (drafting) | how the tool actually runs | the product's own object |
| Idea | claims are dimensioned, limits are tolerances | the page is one long session log you scroll: prompts, tool calls, and the agent's own boundaries as stderr | the page is a card catalogue of the files Nova writes, drawer by drawer |
| Motion verb | rule | stream | pull, settle |
| Signature | the measure | the log advances, output arrives line by line | cards fan out of a drawer |
| Originality | 4 | 3 | 4 |
| Fit | 5 | 4 | 4 |
| Feasibility | 4 | 4 | 3 |
| Portability (lower is better) | 2 | 3 | 3 |

Chosen: **A**. It is the only one that turns the site's own honesty into structure, it stays
recognisable from a crop, and it is the cheapest to execute at 60 fps in plain CSS. Steal one
detail from B for the boot screen: the plot sequence is a log of what the sheet is waiting for.

## Art direction

**Palette**, six named roles per theme.

| Role | Light | Dark | Use |
|---|---|---|---|
| `--ground` | `#EFEDE7` | `#121316` | the sheet |
| `--panel` | `#F7F5F0` | `#191A1E` | inset detail views |
| `--ink` | `#17181A` | `#E8E6E1` | type, rules, inverted sheets |
| `--muted` | `#63625C` | `#9A9A94` | tolerances, metadata (AA on ground) |
| `--line` | `#CFCCC3` | `#2C2D31` | hairlines, about 12% ink |
| `--accent` | `#B23C0A` | `#FF6A2B` | tolerances, ticks, one action (5.1:1 on ground) |
| `--neg-accent` | `#FF6A2B` | `#B23C0A` | the same pen, on an inverted sheet |
| `--curtain` | `#17181A` | `#0C0D0F` | the plot overlay |

Orange is the annotation pen. It marks a limit, a dimension, or an action on the sheet.
Chapter dividers invert with the selected theme; the closing panel and coin use the original
dark palette to frame their monochrome engravings. `--neg-accent` keeps the annotation legible
on an inverted surface.

**Type.** Archivo (variable, `wdth` 62 to 125, `wght` 400 to 700, self-hosted) plus Geist Mono
(already in `assets/fonts/`). One family, two personalities: display is the same Archivo at
`wdth 112`, weight 700, tracking `-0.03em`, leading 0.92; text is Archivo at `wdth 100`, 400 or
600. Geist Mono is reserved for things that are genuinely measured or typed: dimensions, file
paths, commands, zone references, version numbers. Mono is never used as a generic small
label, and there is no tracked uppercase eyebrow above headings.

Scale: `--t-mega` `clamp(2.7rem, 1rem + 7.2vw, 7rem)`, `--t-xl` `clamp(2rem, 1rem+4vw, 4.2rem)`,
`--t-l` `clamp(1.6rem, 1.1rem+1.7vw, 2.5rem)`, `--t-m` `clamp(1.25rem, 1rem+.7vw, 1.6rem)`,
body `1.0625rem`/1.55, small `0.875rem`, mono `0.75rem`.

**Grid.** 12 columns, 20px gutter, sheet margin 28px desktop and 16px phone, max 1400px. Every
band aligns to it; display type hangs 0.06em into the margin optically. The sheet border is a
1px frame inset 14px with edge zone references (A–F, 1–8) in mono 10px, which is the one place
uppercase tracking is correct because it is drawing furniture.

**Shape.** One rule: radius `2px`, on buttons and panels only. Paper has square corners. There
are no soft shadows anywhere; depth is expressed by hairline weight (1px frame, 1px dimension,
2px title block), never by blur.

**Phones wrap, never clip.** Below 620px the workflow becomes a vertical sequence, text and
commands wrap, and the page does not hide content behind horizontal scrolling.

**Section rhythm.** Five chapters — delegation, memory, verification, control, the record — each open
with an inverted divider and a large sheet number. A parts list ("Only in Nova"), installation and a
short FAQ follow. Three chapters carry a figure (terminal capture, memory engraving, plan engraving);
verification is text-only and the record is a horizontal strip, so the page does not read as a
repeating template. Detailed feature inventories remain in the documentation.

**Chapter plates.** The sheet number sits in a fixed-width column (`clamp(88px, 11vw, 168px)`, not
`auto`), so every chapter title starts on the same datum and the outline numeral holds the sheet's
left edge instead of drifting with its glyph widths. The numeral drifts 12% of its own height
against the band as it crosses the viewport (scrub 1.2, transform only, `aria-hidden`): the one
scroll-linked scene besides the hero's depth. Verification's six checkpoints print as a ruled mono
strip, so the column carries the sequence rather than a sentence about it.

**The strip.** The record is one horizontal filmstrip of releases, oldest first: a hairline-bordered
column per release, a mono version in the accent pen, a sentence, and a release-notes link pinned to
the column's foot. On a wide screen with motion the sheet pins for one viewport and the strip runs
sideways on the scroll (`ScrollTrigger` pin, scrub 1, `anticipatePin`, travel = track − window), with
the progress hairline reporting the pin's progress and edge fades on both sides while cards are cut.
Beneath that it is still a real scroll container: phones, touch, reduced motion, the agent view and
no-JS all get the native scroller with snap, and the pinned build keeps the strip out of reach of
nothing — a card reached by Tab scrolls the pin to it, and the box clips with `overflow: clip` so the
browser cannot scroll the track behind the pin. A vertical wheel over the unpinned strip pans it and
hands the gesture back at either end.

**The parts list.** The spec table is the one place the page enumerates capabilities: a mono caption
with its real entry count, one row per capability, the command in mono beside the name, and the limit
printed in the right column as a tolerance. Hovering or focusing a row draws a 1px accent tick in the
left margin — the same gesture as the index rows — and lifts the row's text to full ink. Numbers 01–04
belong to chapters only; capability columns inside a chapter carry no ordinal, because they are parallel
capabilities, not steps.

**Unequal columns.** A chapter never divides into three equal cells. Delegation puts the foundation
(specialist agents) in a 1.5fr column spanning both rows with rooms and Council stacked beside it;
verification runs two across, and the record-keeping statement takes the full measure underneath. Below
760px every variant collapses to one column with top rules, so nothing is clipped or doubled up.

**Motion language.** One easing family, `cubic-bezier(.16, 1, .3, 1)` for things arriving and
`cubic-bezier(.4, 0, .2, 1)` for state changes. Micro 240ms, enter 520ms, drawn line 700ms.
Stagger 40ms. The hero entrance runs once on load: the cover plate develops first (its own
`art-ready`), then the headline, the claim rule, the copy, the actions and finally the title
block's rows rise 22px over 1.05s at `power4.out`, 90ms apart. The head script marks the document
`enter` before the first paint to hide that copy; `assets/motion.js` removes it, and so do its
no-GSAP path and the script's `onerror`, so nothing can stay hidden. The hero depth, the measure
and the coin still run on one requestAnimationFrame clock; the scroll-reveal layer above them is
GSAP with ScrollTrigger (`assets/gsap.min.js`, `assets/ScrollTrigger.min.js`, self-hosted) driven
by one shared Lenis ticker, so a reveal can never lag the scroll position. Reveals run once, at
`top 84%`: section heads rise word by word out of their own mask, `[data-fade]` blocks settle 26px,
`[data-draw]` rules draw left to right, and the three product plates open by `clip-path` with the
picture inside easing back from 1.07. Two controls are magnetic (`data-magnetic`), and a 14px
difference-blend ring follows a fine pointer, widening over anything actionable. Under
`prefers-reduced-motion` the module returns before doing anything: no smooth scroll, no split
type, no clip, no ring. Every hidden state is applied from `assets/motion.js` after its own
gate, so with script blocked, GSAP missing or reduced motion, the sheet is complete and readable.

**Hero depth.** The cover sheet prints on a paper panel down the right of the hero; the drawing
takes the whole left of the sheet, undimmed, because it is the cover art. The real terminal
capture later on the page provides product evidence. On scroll the three printed blocks lag by their own factor —
edge zones .11, title block .06, claim .035 — so the sheet separates from the paper as it leaves,
and travel stops once three quarters of the hero is gone. This is the one scroll-linked motion on
the page: it is decorative depth, not feedback, and it is transform-only so it cannot reflow
anything or widen the document. Under reduced motion the blocks keep their `transform: none` and
sit still as part of the sheet.

**Idle state.** The measure is the only thing that moves without the visitor acting: on the
first paint the rule draws left to right over 700ms and its tick lands on the claim's real
width. It never loops. A gauge was built for this slot and removed — it competed with the
title block, vanished on the inverted bands, and asked the visitor to read a second index
before reading the claim. The measure states one number and stops.

**Micro-interactions**, one treatment per role from the motion verb:
- Action (Install): a hairline sweeps across the block and an accent tick slides in; press
  depresses the block 1px. No colour swap.
- Nav: the underline draws left to right.
- Index rows and links: the row's rule extends and an accent tick appears in the left margin.
- Disclosure: the rule thickens and the marker rotates 90 degrees; a 1px accent tick arrives
  in the left margin on hover and on focus.
- Copy: the same ink wipe as the CTA, from the left edge, so command copy controls answer with
  geometry rather than a colour swap.
- Utility links (footer, legal): colour only, as the role allows.

## Composition plan

```
HERO                                    01 DELEGATION / 02 MEMORY
+-------------------------------+----+   +------------------------------+
| A                             | TB |   | [chapter divider, inverted]  |
|  coding agent, terminal       | v  |   | 01  Several agents           |
|  _______________________|± lim|  e  |   | specialists | rooms |council|
|  install     MIT / Python     | r  |   +------------------------------+
|  contents: delegation, memory,| t  |   | 02 memory: wiki + recall     |
|  verification, control        |   |   | 03 verification: checkpoints |
+-------------------------------+----+   | 04 control: plans, approvals |
                                         +------------------------------+
                                         | parts list: 11 capabilities  |
                                         | install / FAQ / closer       |
```

Each main section answers one question: who does the work, where its memory lives, what it observed,
or how you control plans and tool use. Deeper feature and integration details remain in the
documentation.

## Genericness test

*Would a generic model given "a site for a terminal coding agent" land here?* No: it lands on a
black terminal window with a green prompt and three equal feature cards, or a centered hero
over a gradient. Checked and changed: no dark-window mockups, no eyebrow labels above headings, no
mono for every small label, no 01/02/03 where the content is not a sequence, and no card boxes — the
capability columns are separated by hairlines inside one band, and a page-level parts list replaces the
usual feature-card grid. The radii are 2px rather than the current 999px pills because paper does not
have pill corners, and orange survives the redesign because an annotation pen is the concept's own
colour.

## What this file also governs (unchanged by the redesign)

- **Two-branch contract.** Site files live on `gh-pages`; release Markdown lives on
  `main/changelog/`. This checkout holds only the generated `changelog/index.html`.
- **Theme.** `assets/theme.js` follows the system until the visitor picks, then stores
  `nova-theme` and applies it before first paint on every page. Manual switches open the new
  palette behind a travelling drafting rule over 620ms through native View Transitions.
  Dark prints downward; light reverses upward. Shared rules live in `assets/theme.css`.
  Reduced motion and Agent view switch at once; older browsers use a short color fade.
  Both palettes are designed; neither is a filter over the other.
- **The hero drawing.** `assets/hero-layers/` holds the cover art as four planes (sky, room,
  subject, plants) in light and dark, plus a matching flat composite. The active `*-ink`
  planes share the coin's illustration style while retaining the original colors; the original
  planes remain in the source directory. The subject uses one regenerated `subject-ink`
  drawing in both themes to keep the face identical; sky, room and plants carry a theme `<source>`. Each plane has a
  `--depth`/`--scroll-speed`; `.art` gets `layered` only once all four decode, and the flat
  composite shows until then and under reduced motion (no transforms at all). The planes start
  registered at the top, separate on scroll, and ignore touch movement as a pointer effect. Wide screens are a
  split sheet: the words print on a solid paper panel whose width is `min(48%, 720px)` (the cap
  keeps the measure readable on large monitors) and the drawing takes all the width that is
  left, undimmed, which is what keeps the artwork reading as the page's image.
  At `≤900px` the sheet prints on paper and the drawing follows beneath it as a band — 4:3 on
  phones, 16:9 above 560px — because a 16:9 drawing behind a tall narrow hero is only a slice of
  itself.
- **The plot overlay.** `#boot` is the only overlay. It is hidden without JavaScript, removed
  from the DOM when it finishes, capped at 1.4s by a hard timeout, and states real milestones
  (fonts, hero, sheet) rather than a fake counter.
- **No-JS.** Documentation and release notes stay readable, the sheet renders complete, and
  every measure degrades to a static rule with its label.
- **Stars.** A single fixed `<canvas id="page-stars">` sits behind the page (`z-index:-1`, so
  `html`, not `body`, carries the ground colour) and draws the sky the hero art implies:
  faint stars that twinkle on their own clock, a 130px pointer repulsion that brightens the ones
  near the cursor, a shooting star every 3.4–9s, and one launched by a tap. Tint is per theme
  (`236,242,255` on ink, `150,92,32` on paper). The block is skipped entirely under reduced
  motion, pauses when the tab hides, and resizes on a debounce.
- **Launches and Nova.** The same starfield clock draws one small rocket at a time,
  with a curved, accelerating ascent, warm exhaust and a fading particle trail.
  It launches after the cover sheet leaves, then at 18–30 second intervals. Nova's
  coin-inspired chibi sprite sits beside the page contents: she watches the pointer
  and reacts to a tap, click or Enter/Space, which also requests a launch. An active
  flight is never duplicated. Reduced motion keeps the click reaction and greeting,
  without cursor tracking, a bounce or a rocket. The companion stays in document
  flow and disappears without JavaScript. Sources live in `characters/nova/`;
  served aligned atlases live in `assets/mascots/`.
- **The mark.** `assets/emblem-128.webp` on the `--curtain` ink chip, the same in both themes.
  It replaced the inline triangle so the header and footer carry the Nova emblem.
- **The coin.** A monochrome Nova seal with real SVG rim lettering turns on the existing CSS 3D
  disc. The dark field carries fine orbital guides; dragging and the arrow keys turn the coin.
- **Scrolling.** `assets/lenis.min.js` (Lenis 1.3.27, self-hosted) smooths wheel and touch on the
  real scroll position, so anchors, sticky chrome and the parallax offsets still work. On the
  landing and docs pages `assets/motion.js` owns the instance and rafs it from the GSAP ticker
  that also drives ScrollTrigger; `html.lenis` turns off the CSS smooth behaviour, and reduced
  motion never instantiates it. The release strip keeps a native scroller: pinning it would
  fight touch, keyboard and the agent view.
- **Docs and changelog** keep their own layouts. Both take the same role values, Archivo display
  face and emblem chip, so the three pages print on the same paper.

## Validation

Header icon controls have 44px hit areas while the theme switch keeps its compact
visual track. Header and sheet-content links, plus primary buttons, give keyboard
focus the same feedback as hover. Metadata and illustration captions use at least
11px text; redundant labels above section headings are omitted. Illustration
panels size from their available width, without a minimum height forcing overflow
on 320px screens. The loading strip uses cream text on its fixed dark surface in
both themes.

Mechanical scan exceptions reviewed in the browser: drafting hairlines sit on
their grid boundaries; section padding is inherited through wrappers and
`padding-block`; Geist Mono is reserved for commands and measured metadata;
repeating stripes form the coin's machined rim rather than a page background.

`python tools/verify_site.py` covers internal links, all documentation topics, disclosure
controls, both permalink formats, live refresh and failure recovery, desktop and phone,
reduced motion, the designed 404, and axe when `axe-core` is installed. It asserts no console
errors, no horizontal overflow, that `#boot` leaves the DOM, and that the motion layer settles
every reveal, word and clip while Lenis and the cursor are live. `tools/verify_hero.py`
recomputes the claim's widest rendered line and asserts the rule and the printed number match
it, in both themes, on a phone and under reduced motion; it also checks that a saved theme
repaints the browser chrome.
