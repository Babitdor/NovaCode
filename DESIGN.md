# Nova site: phosphor

The audience is developers installing Nova and looking up documentation and release notes.
GitHub Pages serves the `gh-pages` branch at `/NovaCode/`; release Markdown belongs to
`main/changelog/`.

This file is the binding art direction for the landing page (`index.html`) and the shared
chrome of `docs/index.html` and `404.html`. Everything below is derived from one idea; if a
change cannot be traced to it, it does not belong.

## Concept Card

```
Idea:        The site is a phosphor plate. Every claim Nova makes is struck into a cold
             screen: cyan on charcoal, hairline rules, an assay mark naming the evidence.
Metaphor:    A proof mint: dies, hallmarks, edge lettering, assay office. Nova's own word
             is "proof" — and a proof coin is the finest strike a mint produces.
Signature:   The strike. A chapter's plate lands as a die press — fast press, one tiny
             rebound, then still — and its engraving fills in. Secondary signature: the
             assay mark, a punched chip under each claim linking to its evidence.
Emotion:     Weight and honesty. Metal, hallmarks, "this has been assayed."
Motion verb: strike and settle. Big things press slower than small things; nothing floats.
Screenshot:  A mirror-dark plate under a frosted engraved headline, the coin struck
             center, a stamped assay chip naming the proof beneath each claim.
```

**Why this and not a warm terminal.** The rut for this category is a dark terminal window
with a warm prompt and a friendly sans. Phosphor takes the boot screen the product actually
opens with — charcoal-teal black, one cyan, bitmap-lettered chrome — and makes it a designed
surface instead of a screenshot: quiet, cold, exact. Nova's claims
stay the same — delegation that keeps work separate, memory in plain sight, verification as
observed evidence, approvals that fail closed — but the page strikes them as proof goods:
chapters are die pressings, every claim carries its assay mark, the record runs as edge
lettering on a rail, and the word "proof" means what it means at an assay office.

## Direction round (recorded)

Predecessor direction: "Teletext Service" (`broadcast-programming-teletext-service`), the
user-locked challenger that shipped the previous design. The user commissioned a full redesign
under **Phosphor**; its two surviving artifacts carry over untouched in style — the hero's
layered picture and the 3D emblem coin — and the retired signatures (page dial, starfield and
rockets, teletext status rows, REVEAL keys) are listed at the end of this file.

## Art direction

**Palette (phosphor's eight)**, per theme — dark is the unlit screen, light is the same plate
in daylight.

| Role | Dark | Light | Use |
|---|---|---|---|
| `--ground` | `#030E3E` (charcoal teal) | `#FAD9B6` (cold paper) | the screen / the stock |
| `--panel` | `#131B5D` | `#FFFFFF` | plates, code, specimen cards |
| `--ink` | `#FAD9B6` | `#0A1215` | body, rows |
| `--muted` | ink @ .72 | ink @ .74 | metadata, captions |
| `--line` | ink @ .26 | ink @ .22 | rules, plate edges |
| `--accent` (cyan) | `#F7B98A` | `#9E4A52` | headlines, die numbers, assay chips |
| `--proof` (red) | `#B96F6E` | `#9E4A52` | only: revealed answers, alerts |
| `--live` (mint) | `#B3A7AC` | `#6B5B7A` | live dots only |

One hue family: cyan through teal, from phosphor `#F7B98A` down to the deep `#9E4A52`. No
warm colour anywhere — the previous brass, cream and iron are retired with the mint. Red and
mint-green are reserved signals and never decorative. The light theme is the same plate in
daylight: identical roles on cold paper — a designed pair, not a filter.

**Type.** Instrument Serif 400 (+italic, self-hosted OFL, `assets/fonts/`) is the engraved
display voice: headlines, chapter titles, the closer. Geist Mono (already self-hosted) is the
body and every measured string: body `16px/1.6`, metadata at .8rem-.84rem, measure 60-70ch.
Display tracking tightens per size. Silkscreen retires from index, docs and 404 (the file stays
on disk); Archivo stays self-hosted for the docs reading surface only.

**Grid & rhythm.** Content column max 1120px with `--pad` gutters (kept). Section rhythm
varies: full-bleed plates (hero art, coin), inset ledgers (spec, FAQ), a tray (contents), a rail
(record). Chapters open as die headers: the die number in cyan Geist Mono, the title engraved
in Instrument Serif. Phones wrap, never clip: below 760px workflows and splits collapse to one
column, below 720px the spec table stacks to ruled rows (the accessibility tree keeps table
roles).

**Shape.** Radius 0 everywhere — a phosphor plate has no soft corners. Depth is rule weight
(1px hairlines, 2px plate edges), never blur; the coin's ground shadow stays the one shadow.

**Motion language.** One verb, strike and settle: big things press slower than small things,
nothing floats. Entrances press down at `power4.in` (0.5-0.9s), settle at `power3.out`,
staggered 30-70ms; big plates press slower than small chips. The clock stays the self-hosted
GSAP + ScrollTrigger + Lenis stack (`assets/motion.js`) — kept because the regression suite
binds it and because its gates are right: nothing hides when script, GSAP or motion is
unavailable. Idle motion is only the coin turning and a slow sheen along the active chapter
headline's engraving hairlines; under `prefers-reduced-motion` the module returns before doing
anything: no smooth scroll, no press, no sheen, no clip.

## The kept artifacts (style-untouched)

- **The picture (hero layers).** `assets/hero-layers/` holds the cover art as four planes (sky,
  room, subject, plants) in light and dark, plus a flat composite. The active `*-ink` planes
  share the coin's illustration style. Each plane has a `--depth`/`--scroll-speed`; `.art`
  gets `layered` only once all four decode, and the flat composite shows until then and under
  reduced motion. The planes start registered at the top, separate on scroll, and ignore touch
  movement as a pointer effect. Wide screens are a split plate: words on the text cell
  (`min(48%,720px)`), the picture takes the rest undimmed. At ≤900px the picture follows the
  words as a band — 4:3 on phones, 16:9 above 560px. The four-plane mechanism, its classes and
  its script are carried over exactly as they shipped with the teletext design.
- **The emblem coin.** The 3D coin (`.coin-band`) is the page's own seal: same engraved faces
  (`assets/coin.webp`), rim slices, orbiting sparks, drag and arrow-key spin, ring text. Its
  stage keeps the geometry variables (`--disc/--half/--tilt/--spin/--live`) so the kept script
  runs unchanged. The band keeps its own darker ground — the ident room inside the page.
- **Nova the mascot** (`assets/mascots/`), the contents companion, stays with its sprite
  behavior.
- **Retired signatures.** The page dial (keyed P-numbers and the status row), the starfield and
  its launch clock, REVEAL keys, teletext block framing and Silkscreen as a display face are
  removed from index, docs and 404. Their assets may remain on disk; nothing on the pages
  references them.

## What this file also governs (unchanged by the redesign)

- **Two-branch contract.** Site files live on `gh-pages`; release Markdown lives on
  `main/changelog/`. This checkout holds only the generated `changelog/index.html` — never
  hand-edit between the `generated:changelog-*` markers.
- **Theme.** `assets/theme.js` follows the system until the visitor picks, then stores
  `nova-theme` and applies it before first paint on every page. Manual switches reveal the new
  palette behind a travelling press wipe (`assets/theme.css`, View Transitions). Both palettes
  are designed; neither is a filter over the other.
- **The boot overlay.** `#boot` is the only overlay: a die press that states real milestones
  (fonts, page, reveal), hidden without JavaScript, removed from the DOM when it finishes,
  capped by a hard timeout, skipped on return visits (`plot-skip`).
- **No-JS.** Documentation and release notes stay readable, the page renders complete, the
  measure degrades to a static hairline rule with its label.
- **Agent view.** `assets/view.js` keeps the readable mirror; the page chrome hides in agent
  view exactly as the sheet furniture did.

## Genericness test

*Would a generic model given "a site for a terminal coding agent" land here?* No: it lands on
a dark terminal window with a green prompt and three equal feature cards. Phosphor is neither:
die-pressed chapter plates, engraved serif headlines, punched assay chips under claims, an
edge-lettered record rail, and one cyan that marks the page's own voice.

## Sections (the striking plan)

Hero split plate (die press, headline, assay chips, actions, picture) · contents tray of struck
chips · die 01 delegation · die 02 memory · die 03 verification · die 04 control (each a die
header over engraved plate panels) · five **specimen screens built in CSS** · spec assay table ·
the record as an edge-lettered die rail (native horizontal scroller) · install as stamped
command rows · FAQ hallmark book (`<details>`) · closer and start · the emblem coin · coin mark
footer.

**The screens are drawn, not photographed.** Each walkthrough's terminal state (`.tui`: title
bar, panes, prompt row) is HTML and CSS in the phosphor palette, staged under a cyan-cornered mount
with a stamped caption. The three step marks are real elements (`[data-tut="0..2"]`), lit by the
same `data-step` switch the sticky layout drives, so a highlight can never drift off the thing it
names. Each caption still links the real capture ("View real screenshot") — the reconstruction is
presentation, the linked file is evidence, and the prose remains the authority on behaviour.
- **The mark.** `assets/emblem-128.webp` stamps the masthead and footer, the same in both
  themes.
- **Scrolling.** `assets/lenis.min.js` (Lenis 1.3.27, self-hosted) smooths wheel and touch on
  the real scroll position, so anchors, sticky chrome and the parallax offsets still work. On
  the landing and docs pages `assets/motion.js` owns the instance and rafs it from the GSAP
  ticker that also drives ScrollTrigger; `html.lenis` turns off the CSS smooth behaviour, and
  reduced motion never instantiates it. The record rail keeps a native scroller: pinning it
  would fight touch, keyboard and the agent view.
- **Docs and changelog** keep their own layouts. Both take the same role values, Geist Mono
  body and the emblem chip, so the three pages print on the same stock.

## Validation

Header icon controls have 44px hit areas while the theme switch keeps its compact visual
track. Header and drawer links, plus primary buttons, give keyboard focus the same feedback
as hover. Metadata and captions use at least 11px text; no minimum heights force overflow on
320px screens; text is never hidden under `prefers-reduced-motion`.

`python tools/verify_site.py` covers internal links, all documentation topics, disclosure
controls, both permalink formats, live refresh and failure recovery, desktop and phone,
reduced motion, the designed 404, and axe when `axe-core` is installed. It asserts no console
errors, no horizontal overflow, and that `#boot` leaves the DOM. The per-feature checkers are
aimed at the phosphor markup: the hero headline and measure, the specimen-framed walkthroughs and
the agent-view contract; they update with the markup they bind to, never silently.
