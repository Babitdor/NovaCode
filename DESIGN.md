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

**Why this and not a terminal theme.** Nova's useful distinction is how it keeps work
understandable: memory lives in files you can inspect, plans can be reviewed, and tool approvals
follow a policy you control. The drawing-sheet form makes those claims and their limits easy to
scan without turning the landing page into a catalog of every capability.

**Anchor fact:** the product's memory is readable Markdown, plans are saved for later review, and
tool approvals follow configurable policy with hard-coded denials still enforced. `FEATURES.md`
remains the detailed reference for secondary capabilities and setup requirements.

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

**Section rhythm.** Three focused chapters—workflow, memory, and control—each open with an
inverted divider and a large sheet number. Installation and a short FAQ follow. A real terminal
capture shows the product; monochrome engravings illustrate memory and review without inventing
interface results. Detailed feature inventories remain in the documentation.

**Motion language.** One easing family, `cubic-bezier(.16, 1, .3, 1)` for things arriving and
`cubic-bezier(.4, 0, .2, 1)` for state changes. Micro 240ms, enter 520ms, drawn line 700ms.
Stagger 40ms. One clock: a single requestAnimationFrame loop reads scroll and time and writes
CSS custom properties; there is no second loop and no scroll listener doing layout work.
Under `prefers-reduced-motion` nothing moves, the plot overlay never appears, and the sheet is
complete and readable.

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
HERO                                    01 WORKFLOW
+-------------------------------+----+   +------------------------------+
| A                             | TB |   | [chapter divider, inverted]  |
|  coding agent, terminal       | v  |   | 01  From task to review      |
|  _______________________|± lim|  e  |   | three steps + real capture   |
|  install     MIT / Python     | r  |   +------------------------------+
|  contents: workflow, memory,  | t  |   | task → code → review         |
|  control                      |   |   |                              |
+-------------------------------+----+   02 MEMORY / 03 CONTROL
                                         | files / configurable policy |
```

Each main section answers one question: how Nova works, where its memory lives, or how you
control plans and tool use. Deeper feature and integration details remain in the documentation.

## Genericness test

*Would a generic model given "a site for a terminal coding agent" land here?* No: it lands on a
black terminal window with a green prompt and three equal feature cards, or a centered hero
over a gradient. Checked and changed: no dark-window mockups, no equal card grid, no eyebrow
labels, no mono for every small label, no 01/02/03 where the content is not a sequence. The
radii are 2px rather than the current 999px pills because paper does not have pill corners, and
orange survives the redesign because an annotation pen is the concept's own colour.

## What this file also governs (unchanged by the redesign)

- **Two-branch contract.** Site files live on `gh-pages`; release Markdown lives on
  `main/changelog/`. This checkout holds only the generated `changelog/index.html`.
- **Theme.** `assets/theme.js` follows the system until the visitor picks, then stores
  `nova-theme` and applies it before first paint on every page. Manual switches open the new
  palette as a circle from the toggle over 700ms through native View Transitions; the circle
  radius arrives in `--theme-r`, which `index.html` consumes. Reduced motion switches at once.
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
  real scroll position, so anchors, sticky chrome and the parallax offsets still work. `html.lenis`
  turns off the CSS smooth behaviour, and reduced motion never instantiates it.
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
errors, no horizontal overflow, and that `#boot` leaves the DOM. `tools/verify_hero.py`
recomputes the claim's widest rendered line and asserts the rule and the printed number match
it, in both themes, on a phone and under reduced motion; it also checks that a saved theme
repaints the browser chrome.
