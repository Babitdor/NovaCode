# NovaCode site: design reference

The authority for what this site looks like, why, and what a future change is
allowed to do to it. Written before the build, then corrected against the
rendered page. Values are tagged **Authored** (literal in the source) or
**Measured** (read back from the built page in Chromium).

Brand rules live in `../BRAND.md`. That file owns the name forms, the promise,
the limit rule and the voice. This file owns how *this site* expresses them.

---

## 1. Concept card

```
Idea:        Nova's promise is verification, so the site is the proof: a sheet
             that states each claim, its limit, and the evidence, then checks
             itself in front of you.
Metaphor:    a printer's proof / an engineering drawing sheet. Title block,
             revision table, hairline rules, margin notes, a proof stamp.
Signature:   the verification pass. A pinned stage where a real change to Nova
             is applied line by line, its test runs, and the verdict flips from
             FAIL to PASS. Scroll scrubs it.
Emotion:     calm precision. The confidence of a document that has been checked.
Motion verb: check and settle. A cursor moves, a mark lands, a rule locks.
Screenshot:  the sheet at rest: near-white paper, hairline rules, mono row
             numbers in the margin, one vermilion proof mark, a huge serif
             claim, a dark plate of a real session.
```

**Why this and not the alternatives.** Three concepts were considered.

| Concept | Territory | Originality | Fit | Feasible | Portable (lower is better) |
|---|---|---|---|---|---|
| A night sky that brightens as work verifies | the name (nova = a star that flares) | 2 | 3 | 5 | 4 |
| The page is one session's scrollback | the profession's material | 3 | 5 | 4 | 3 |
| **The proof sheet** | a metaphor for how it works | 4 | 5 | 4 | **2** |

The star idea is generic and the existing hero art already occupies it. The
scrollback idea is what the previous site did, and "the page is a terminal" is
the default devtool move. The proof sheet is the only one that could not be
moved to another product: it depends on Nova's own doctrine that every claim
carries its limit and every capability carries its evidence.

**The anchor.** `BRAND.md` §3, the limit rule: *if you add a feature, add its
limit in the same breath.* A sheet that states a claim, its cap and its proof is
that rule turned into a layout.

---

## 2. The genericness test

*If I gave a similar prompt to a generic model, would it land here?*

No. A generic answer is a dark hero, a gradient, three feature cards, a mocked
terminal and "supercharge your workflow". This page is a document: a title
block, a revision table, numbered claims with margin limits, a real diff from
Nova's own history, a real test run, a status line that counts proofs as you
scroll, and a stamp that lands when the count is complete. The structure is the
argument, not a container for it.

Three things were changed after asking the question:

1. The hero was going to be a centred claim over the illustration. It is now an
   asymmetric sheet: claim left, a dark plate of the real TUI right, title block
   under the claim.
2. The capabilities were going to be three cards. They are now ledger rows with
   a number, a name, a limit and a proof mark.
3. The illustration was going to be full-bleed. It is now a single treated
   portrait plate with a caption, because a mascot wallpaper is not evidence.

---

## 3. Palette

One theme. The product is a terminal and the sheet is paper; a second theme
would mean composing every frame twice and neither would be the best frame.
`prefers-color-scheme` is not used.

| Token | Value | Role |
|---|---|---|
| `--paper` | `#f6f5f1` | Page ground |
| `--paper-2` | `#eceae3` | Tinted band, table headers, inset rows |
| `--ink` | `#12110e` | Primary text, rules at full strength |
| `--ink-2` | `#45423b` | Body copy |
| `--ink-3` | `#6a665c` | Meta, captions, margin notes |
| `--rule` | `#d5d1c6` | Hairlines |
| `--rule-2` | `#b5b0a2` | Structural rules, table borders |
| `--accent` | `#b23a0c` | The single accent: proof marks, active phase, links |
| `--accent-2` | `#e04a12` | Large marks and fills only, never small text |
| `--plate` | `#101013` | Dark plate ground (the terminal world) |
| `--plate-2` | `#17171b` | Raised surface inside a plate |
| `--plate-ink` | `#e9e7e2` | Text on a plate |
| `--plate-rule` | `#2b2b33` | Rules on a plate |

**The chromatic budget.** Exactly one hue family (vermilion) appears in the UI.
Success and failure do not get green and red: they get an accent tick and an
accent cross, and the word carries the meaning. The product's own syntax colours
appear only inside plates, because a photograph of the product is evidence and
evidence is allowed to be itself.

**Contrast floor.** Every text pair clears WCAG AA. Measured pairs are recorded
in §9.

---

## 4. Type

Three families, three strict roles. The split is the concept: **the serif is the
human voice, the sans is prose, the mono is the machine.**

| Family | Source | Role |
|---|---|---|
| Instrument Serif | Google, self-hosted, 400 + 400 italic | Display: h1, h2, the claim, pull quotes, big numbers |
| Geist | self-hosted variable, 100-900 | Prose: body, lede, nav, buttons, FAQ answers |
| Geist Mono | self-hosted variable, 100-900 | The machine: labels, commands, code, table cells, captions, the status line |

A serif for a terminal tool is the deliberate risk. It is what makes the page a
document rather than a dashboard, and it is why the mono reads as evidence
instead of decoration.

**Scale** (fluid, `clamp()`). Authored values, with what they resolve to at the
two widths the site is checked at:

| Role | Authored | @1440 | @390 |
|---|---|---|---|
| Display XL (hero claim) | `clamp(2.6rem, 5.4vw, 5.2rem)`, lh .92, tracking -.03em | 77.8 / 71.6 | 41.6 / 38.3 |
| Display L (section h2) | `clamp(2rem, 3.4vw, 3.4rem)`, lh .98, tracking -.02em | 49.0 / 48.0 | 32.0 / 31.4 |
| Display M (statement) | `clamp(1.45rem, 2.1vw, 2rem)`, lh 1.18 | 30.2 / 35.7 | 23.2 / 27.4 |
| Lede | `clamp(1.05rem, 1.15vw, 1.25rem)`, lh 1.5, max 46ch | 16.8 / 25.2 | 16.8 / 25.2 |
| Body | `1rem`, lh 1.62, max 62ch | 16.0 / 25.9 | 16.0 / 25.9 |
| Small | `.875rem`, lh 1.55 | 14.0 / 21.7 | 14.0 / 21.7 |
| Mono label | `.75rem`, tracking .12em, uppercase | 12.0 / 14.4 | 12.0 / 14.4 |
| Mono data | `.8125rem`, tracking .01em | 13.0 / 21.1 | 13.0 / 21.1 |

Display tracking is negative and set per size. Mono labels carry positive
tracking. Body is 16px with 1.62 leading. Nothing is set below 12px.

---

## 5. Grid, shape, rhythm

- **Grid.** 12 columns, `--wrap: 1400px`, `--gutter: 24px`,
  `--margin: clamp(20px, 4.5vw, 72px)`. Baseline 8px.
- **Alignment rule.** Every block starts on a column line. Margin notes hang in
  the left gutter at `--margin` and never enter the text column.
- **Shape.** One radius: `--r: 2px`. Documents have square corners. No pills, no
  floating cards. A plate is a rectangle with a hairline and a caption.
- **Section rhythm.** Sections alternate between full-bleed rules and inset
  bands. No two consecutive sections share a layout family. Vertical rhythm is
  `clamp(88px, 11vh, 160px)` between sections, halved inside a section.

**Layout families, one per section** (see the wireframes in §7): asymmetric
split, hanging-margin statement, pinned dark stage, ledger table, tier diagram,
name strip, offset timeline, narrow centred column, sticky-split accordion,
bordered close box.

---

## 6. Motion

- **One clock.** Lenis drives scroll, GSAP's ticker drives everything else. No
  `scroll` listeners, no private `requestAnimationFrame` loops.
- **Easing.** `--ease: cubic-bezier(.22, 1, .36, 1)` for arrivals;
  `cubic-bezier(.65, 0, .35, 1)` for scrubbed progress. One family.
- **Durations.** Arrival 0.6-0.9s. Feedback 0.18-0.24s. Stagger 40-70ms.
- **The verb.** *Check and settle.* Content arrives 14px low and settles; rules
  draw from the left; marks land with a 0.6 → 1 scale snap and no bounce.
- **Idle.** The status line's cursor blinks at 1.1s. The proof counter ticks
  when a claim is passed. The stage's phase rail keeps a slow pulse while
  pinned. Nothing else moves at rest.
- **Reduced motion.** No smooth scroll, no pin, no scrub, no intro. The stage
  renders its final state (verified) as a static composition. All content is
  visible without JavaScript.

---

## 7. Wireframes

```
1  MASTHEAD                                            [stacked, full-bleed]
┌──────────────────────────────────────────────────────────────────────┐
│ NOVA.CODE ······························ proof  loop  verify  install│
├───────────────────────────────────┬──────────────────────────────────┤
│ A coding agent that               │ It reads the codebase, plans,    │
│ verifies its own work.            │ edits, runs the tests, reads the │
│                                   │ failures, and retries.           │
│                                   │                                  │
│                                   │ Every claim on this page is a    │
│                                   │ test you can run.                │
├───────────────────────────────────┴──────────────────────────────────┤
│ $ uv run nova                                              [copy]    │
├──────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │  dark plate: real TUI capture, one title bar, no second chrome   │ │
│ │  (plate-session.webp)                                            │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│  FIG. 01  A real session, unedited.                                  │
├──────────────────────────────────────────────────────────────────────┤
│ SHEET 01/03 │ REVISION 3 │ STATUS DRAFT │ LICENCE MIT │ RUNTIME …    │
└──────────────────────────────────────────────────────────────────────┘
```

```
2  THE CLAIM                                           [hanging margins]
┌──────────────────────────────────────────────────────────────────────┐
│ CLAIM 01                                                             │
│        Code written isn't the same                                   │
│        as a task finished.                                           │
│                                                                      │
│  ┌ note ─────┐   Nova reads the codebase, plans the change, edits     │
│  │ the loop  │   the files, runs the tests, reads the failures and    │
│  │ is six    │   retries. The finish line is a result that has been   │
│  │ steps     │   checked, not a diff that has been produced.          │
│  └───────────┘                                                       │
│                                                                      │
│        "If you add a feature, add its limit in the same breath."     │
└──────────────────────────────────────────────────────────────────────┘
```

```
3  THE PROOF                                           [pinned dark stage]
┌──────────────────────────────────────────────────────────────────────┐
│ PROOF 01   context_compaction.py   +7 −2                             │
│ ┌──────────────────────────────────┬───────────────────────────────┐ │
│ │ 41  def _is_image_block(block):  │ $ pytest tests/test_context_  │ │
│ │ 42 -    if block.get("type"):    │   compaction.py -q            │ │
│ │ 42 +    if block.get("type") ==  │                               │ │
│ │ 43 +       "image":              │ 2 failed, 34 passed in 25.00s │ │
│ │ 44 +        return True          │                               │ │
│ │ 45 +    return False             │ ── retry ──────────────────── │ │
│ │                                  │ 36 passed in 11.68s           │ │
│ │                                  │                               │ │
│ │                                  │        ┌──────────────┐       │ │
│ │                                  │        │  VERIFIED    │       │ │
│ └──────────────────────────────────┴────────┴──────────────┴───────┘ │
│ PLAN ── EDIT ── TEST ── READ ── RETRY ── VERIFY                      │
└──────────────────────────────────────────────────────────────────────┘
```

```
4  THE LOOP                                                  [ledger table]
┌──────────────────────────────────────────────────────────────────────┐
│ 01  PLAN      writes the todos before it touches a file      ✓       │
│ 02  EDIT      applies the change, one hunk at a time         ✓       │
│ 03  TEST      runs the suite it found, not one it invented   ✓       │
│ 04  READ      reads the failure, not just the exit code      ✓       │
│ 05  RETRY     stops at three attempts                        ✓       │
│ 06  VERIFY    grades the result on five criteria             ✓       │
├──────────────────────────────────────────────────────────────────────┤
│ criteria:  correct · complete · tested · minimal · honest            │
│ turn it on:  nova config set verify.enabled true                     │
└──────────────────────────────────────────────────────────────────────┘
```

```
5  THE CONTROLS                                              [tier diagram]
┌──────────────────────────────────────────────────────────────────────┐
│                    ┌──────────────┐  auto    reads, searches, plans  │
│  every tool has    │   TIER 1     │  ask     writes, runs, deletes   │
│  a tier, and the   ├──────────────┤  deny    network, secrets, push  │
│  tiers are yours   │   TIER 2     │                                  │
│                    ├──────────────┤  ┌────────────────────────────┐  │
│                    │   TIER 3     │  │ dark plate: approval prompt│  │
│                    └──────────────┘  └────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

```
6  THE REACH                                                    [name strip]
┌──────────────────────────────────────────────────────────────────────┐
│ OPENAI   ANTHROPIC   GOOGLE   GROQ   OLLAMA   OPENROUTER   AZURE     │
├──────────────────────────────────────────────────────────────────────┤
│ $ nova -p "fix the failing test"        headless, exits with a code  │
│ discord  telegram  browser              the same loop, elsewhere     │
└──────────────────────────────────────────────────────────────────────┘
```

```
7  THE LEARNING                                             [offset timeline]
┌──────────────────────────────────────────────────────────────────────┐
│   │                                                                  │
│   ●── TRACE      every turn is written to a file you can open        │
│   │                                                                  │
│   ●── GRADE      five criteria, one extra model call, off by default │
│   │                                                                  │
│   ●── RETRY      a candidate prompt has to earn its place            │
│   │                                                                  │
│   ●── LEARN      prompts, skills and thresholds on disk, deletable   │
└──────────────────────────────────────────────────────────────────────┘
```

```
8  THE INSTALL                                        [narrow centred column]
┌──────────────────────────────────────────────────────────────────────┐
│                    ┌──────┬──────┬──────────────┐                    │
│                    │  uv  │ pip  │ from source  │                    │
│                    ├──────┴──────┴──────────────┤                    │
│                    │ $ uv run nova        [copy]│                    │
│                    └────────────────────────────┘                    │
└──────────────────────────────────────────────────────────────────────┘
```

```
9  THE QUESTIONS                                        [sticky-split accordion]
┌──────────────────────────────────────────────────────────────────────┐
│ Does it need an API key?  │  +  Which models does it support?        │
│                           │  ─────────────────────────────────────   │
│ Five questions,           │  +  Does it send my code anywhere?       │
│ answered plainly.         │  ─────────────────────────────────────   │
│                           │  +  What does it cost?                   │
└──────────────────────────────────────────────────────────────────────┘
```

```
10 THE CLOSE                                                  [bordered box]
┌──────────────────────────────────────────────────────────────────────┐
│  Install it and give it a real task.          ┌──────────────────┐   │
│  $ uv run nova                                │                  │   │
│  [install] [github]                           │   portrait       │   │
│  MIT · local · your model                     │   plate          │   │
│                                               └──────────────────┘   │
│                                               FIG. 02  The author    │
└──────────────────────────────────────────────────────────────────────┘
```

The close box is the one place the page shows a person. The portrait sits in a
dark plate, cropped to the same 4:5 as the hero plate, with a figure caption
under it. On `verify.html` the same box runs single-column and closes with a
five-cell record strip instead.

### The other two pages

`verify.html` — the record. Same nav, same status line, same foot. A page head,
then nine numbered steps: the claim, what was run, the output, and what it does
not prove. It closes with a five-cell record strip (sheet, claims, verified,
unverified, last run). Every step points at a file or a test that exists in the
repository, so the page is a map to the evidence rather than a restatement of
it.

`404.html` — the void. Centred, one stamp, two ways back. No nav links and no
footer columns; the page's own buttons are the navigation.

---

## 8. The page-wide driver

One module maps scroll progress to every visual that carries the concept, so
they always agree:

```
scroll progress ──┬── status line: CLAIMS n/9 · PROOFS n/9 · STATUS DRAFT→VERIFIED
                  ├── margin proof marks: each claim's mark lands as it is passed
                  ├── title block: STATUS field flips at the last claim
                  └── close box: the stamp lands
```

The status line is the TUI's own status bar, so the device is the product's, not
a borrowed HUD. It is fixed to the bottom, 34px tall, and the page reserves that
space.

---

## 9. Measured record

Taken from the built site, not from the sketch. Every number below was read out
of the rendered page or a headless run against it.

**Contrast** (computed from the rendered colours, WCAG 2.1 ratio):

| Pair | Ratio | Needs |
|---|---|---|
| `--ink` on `--paper` (body, h1, h2) | 17.31 | 4.5 |
| `--ink-2` on `--paper` (lede, nav, foot links) | 9.19 | 4.5 |
| `--ink-3` on `--paper` (captions, meta) | 5.25 | 4.5 |
| `--accent` on `--paper` (criteria keys) | 5.49 | 4.5 |
| `--plate-ink` on `--plate` (stage text) | 14.46 | 4.5 |
| `--plate-ink-2` on `--plate` (diff code) | 12.68 | 4.5 |
| `--plate-ink-2` on `#181826` (plate bar) | 6.86 | 4.5 |
| `#8a8790` on `--plate` (rail steps) | 5.38 | 4.5 |
| `--paper` on `--ink` (button label) | 17.31 | 4.5 |

The lowest pair is 5.25, so every text colour clears AA with margin. `--ink-3`
was lightened twice during QA (`#78746a` → `#6f6b61` → `#6a665c`) to get there.

**Type at the two checked widths** — see the scale table in §2. Nothing renders
below 12px.

**Page height:** 9,345px at 1440×900; 10,717px at 390×844.

**Load** (headless Chromium, cold cache, 1440×900):

| Metric | Value |
|---|---|
| LCP | 244 ms |
| CLS | 0.0000 (no layout shift recorded) |
| DOMContentLoaded | 213 ms |
| Requests | 11 |
| Transfer | 434 KB |
| JS | 147 KB (GSAP + ScrollTrigger + Lenis + app.js) |
| CSS | 43 KB |
| Fonts | 87 KB (4 woff2, all used) |
| Images | 166 KB |

**Scroll performance:** a full-page scroll in 2.5 s produced 361 frames, worst
frame 8.4 ms, zero frames over 20 ms. Headless Chromium is uncapped, so the fps
figure is not a display measurement; the meaningful number is that no frame
during the pinned stage exceeded 20 ms.

**axe-core:** 0 violations at 1440×900, 834×1112 and 390×844 on `index.html`,
`verify.html` and `404.html`. No console errors. No horizontal page overflow at
any of the three widths (`scrollWidth === innerWidth`).

**Not measured:** real-device frame timing, Safari and Firefox rendering, and
screen-reader output. The site is checked in Chromium only.

---

## 10. What a future change may not do

- Add a second accent hue to the UI. The product's colours stay inside plates.
- Add a pill, a floating card, or a radius other than `--r`.
- Put an eyebrow label above a heading unless the label is a real sheet number,
  file path or command.
- Add a section that is a statement without evidence next to it.
- State a capability without its limit in the same block.
- Replace the real diff or the real test output in the stage with a mock.
- Add a loader. The hero intro is the entrance.

---

## 11. Deploying

The site is three static pages and an `assets/` folder. There is no build step:
serve the directory and it works. `python -m http.server` is enough for a local
check.

Three things need the production URL, and are deliberately left out until there
is one:

1. `<link rel="canonical">` on each page.
2. `og:url` (and an absolute `og:image`; it is relative today, which most social
   scrapers will not resolve).
3. `sitemap.xml`. `robots.txt` ships without a `Sitemap:` line for the same
   reason.

Everything else is absolute or self-contained: fonts, GSAP, ScrollTrigger and
Lenis are vendored under `assets/`, so the pages have no third-party runtime
dependency and work offline.
