# BRAND.md

**Nova's brand, stated so a new surface can be built from it without copying an old
one.**

This file is for anyone building or reviewing anything that carries the Nova name:
the landing page, the TUI, the web surfaces, the README, a social card, a slide. It
assumes you have never seen the landing page.

It deliberately does **not** carry hex values, font files, the lockup's cell
geometry, or the landing page's section order. Those are implementation, they
already have owners, and they will change. What belongs here is the reasoning that
produces the same decisions twice.

**How to read it.** Each section gives a *rule* first and a *citation* second. A
rule is stated so you can check your work without opening the source. A citation
tells you who owns the number, so you read it rather than retype it.

**Read sections 1 to 6 and you can build a Nova surface correctly.** The rest is
for when you are about to do something unusual.

---

## 1. The name

| Form | Where it belongs |
|---|---|
| `Nova` | prose, headlines, documentation |
| `NOVA.CODE` | the interface, the hero wordmark |
| `nova@code:~$` | the shell identity, prompts, status lines |
| `NovaCode` | the repository and the product name in its own README |
| `novacode-cli` | the distribution, `pyproject.toml` |
| `nova` | the console script |

**Rule.** Pick the form that matches the surface you are on. Never invent a fifth
variant, and never mix two forms in one string. If a surface needs both a display
name and a machine name, they are the two rows above and nothing else.

---

## 2. The promise

> **A coding agent that verifies its own work.**

Expanded, because the short form is easy to fake: Nova reads a codebase, plans,
edits files, runs the tests, reads the failures, and retries. Generated code is not
the finish line. The finish line is a result that has been checked.

Every capability Nova has is downstream of that sentence. If a feature does not
serve verification, it does not belong in the brand story.

---

## 3. The limit rule

> **If you add a feature, add its limit in the same breath.**

This is the most transferable sentence in the file, and it is the reason people
trust Nova's claims.

Real examples, all of them true of the product:

- Grading costs one extra model call per turn, and is off by default.
- Nova does not retrain your model.
- Retries stop at three.
- Improvements are prompts, skills and thresholds on disk, readable and deletable.
- Approval policy is per operation and yours to set.

**Rule.** No capability is stated without the sentence that caps it, and the cap
sits next to the claim, not in a footnote. A page, a README bullet, a TUI tooltip
and a release note all follow this equally.

**Test.** If your copy could describe a competitor by swapping one noun, it is
missing the limit.

---

## 4. Personality

Five traits. Each has a test you can apply to any surface.

**Technically credible.** Name files, flags, tiers, thresholds and model calls
rather than speaking in benefits.
*Test:* does the sentence contain something a reader could not infer from the
product's name alone?

**Autonomous but controlled.** Nova acts without being asked and stops where you
said. Every tool has a tier, and the tiers are yours to set.
*Test:* is there something here the reader controls?

**Honest about limits.** Section 3.
*Test:* is the weakest claim on this surface stated, not buried?

**Local and inspectable.** MIT licensed. Runs on your machine and your model.
Learning is written to readable files you can delete.
*Test:* can a reader see what Nova did, in a form they could open?

**Calm.** No urgency banners, no intensifiers, no exclamation marks, no
revolutionary.
*Test:* would you be embarrassed to read this sentence in a year?

---

## 5. The tone test

Build the demo around the run that **failed twice**, and show the recovery. The
landing page's proof section is deliberately a failure, because the failure is the
tone test: a brand that cannot show a failure is indistinguishable from a
competitor's, and indistinguishable brands are not memorable ones.

**Rule.** Any demonstration you build contains one thing going wrong and being
caught. If your demonstration has never failed, it is not finished.

---

## 6. Voice

Short declarative sentences. Technical words used correctly and not glossed for a
reader who does not need them. Numbers rather than adjectives. No hedging adverbs,
no intensifiers, no second person where first person would do.

**Headlines are sentence case and are built as direct contrasts.** The pattern is
the point, not the lines:

- "Code written isn't the same as a task finished."
- "Autonomous doesn't mean uncontrolled."
- "Describe the behaviour. Let it find the code."

**Eyebrows are commands a user could actually type.** `nova > man nova`,
`> uv run nova`. Real, never decorative. A reader who copies one gets working
output.

**Standing rules.**

- **No em dashes.** In headlines, body, code comments, or commit messages.
- **No exclamation marks.**
- **No signup.** Nothing asks for an email address. The install path leads with
  the command and a copy button.
- **Trust is stated, never implied.** If a claim could be read as an overstatement,
  the sentence next to it caps it.

---

## 7. Colour: one accent, and which one it is

**The rule.** One accent, nothing else is coloured. Success and failure do not get
green and red. They get an accent tick and an accent cross, and the **word**
carries the meaning.

**The decision.**

> **Orange is Nova's brand accent.** `#f4723f` on dark grounds, `#c2410c` on light
> grounds, because a dark-theme orange at light-theme saturation glows. Reach for
> the token, never for a literal.
>
> The product's tokyo-night blue (`#7aa2f7`) is a **product-surface theme**, chosen
> per TUI theme. It is not a second brand colour. When you build a web surface,
> reach for the brand orange.

That paragraph is here because the two palettes have coexisted in the repo without
either being documented, and the next contributor should not have to rediscover
which is which.

**Corollary rules.**

- Illustration colour is not UI colour. Artwork may carry violet and amber; the
  interface may not.
- Accent means one accent on the whole surface. Not one per section.
- A panel may be tinted toward the accent without becoming a second accent.
- A theme is one decision made once. No section inverts, no section gets its own
  background band.
- **Contrast is a floor, not an accident.** Every text and background pair clears
  WCAG AA. If you add a new token pair, re-measure it against the surface it will
  actually sit on, not against another token on paper.

**Citation.** The two hex values and the measured contrast ratios are in
`landing/DESIGN.md` §6 and §5.4, read from `landing/index.html` (`:59` light, `:91`
dark). The product-side accents are `novacode_cli/brand.py:THEME_ACCENTS`, and
`tests/test_brand.py::test_accent_table_matches_the_real_textual_themes` is what
keeps that table honest.

---

## 8. The mark

The identity is one lockup: a braille **portrait** beside a block-glyph `NOVA`
**wordmark**, drawn in one accent. It is not a terminal screenshot and not a
generic glyph. If you are drawing Nova, you draw that.

**Rules.**

- **Never caption the wordmark with the wordmark.** The art already spells NOVA. A
  `NOVA · a terminal coding agent` line under it says the same thing twice, which
  is why the shipped lockup carries only a version. The one fact the art cannot
  draw is the version, so the version is the only caption.
- **Respect the width tiers.** Full lockup, portrait alone, then a one-line mark.
  Never crop or squeeze the art to fit a surface; drop a tier instead.
- **One lockup, centred as a unit.** The portrait and the wordmark align on their
  top edges. Centring a short wordmark against a taller portrait floats the name
  into the middle of the face.
- **Every row is the same width.** Ragged rows are the defect that made earlier
  Nova art read as torn.

**Two traps if you draw this in a terminal.** Pad with real spaces (U+0020), not
braille blanks, or the padding paints an opaque border and occludes what is behind
the art. Measure width in terminal **cells**, not code points: a character plus a
variation selector is two codepoints and one cell, and padding by code-point count
gives you art that is uniform in the source and ragged on screen.

**Citation.** All art lives in `novacode_cli/brand.py`, which is the single owner:
the wordmark rows, the portrait, the compact mark, the width constants, and the
one-accent accessor. `tests/test_brand.py` pins the geometry and asserts the
lockup does not repeat the name it draws. Read the module's docstring before
touching it; two defects that cost real time are documented there.

---

## 9. Derived assets are never hand-edited

The identity artwork has one source image and one script. Regenerate, do not
retouch.

Source `assets/Nova.png`, and `landing/tools/make_brand_assets.py` derives the
favicon, the nav emblem and the social card from it. The crop boxes in that script
were measured by scanning the source for non-black pixels, not guessed. If you
need a different size, add it to the script and run it.

**Rule.** A generated asset that someone edited by hand is a bug. Regenerate it,
and if the hand edit was intentional, move it into the tool first.

---

## 10. Shape, depth and motion

These generalize across media better than colour does, so they are stated as
rules rather than values.

**Radius carries meaning.** A pill is something you press: buttons, chips, tabs.
A rounded rectangle is something you look at: panels, cards, terminal windows.
Dense objects, such as inline code and the mark, get tight radii on purpose.
Do not normalise them.

**Borders are hairlines.** They define an edge. They do not create emphasis.

**Shadows are two-part and restrained.** A contact shadow plus one wide,
heavily-offset drop shadow. A third means stop.

**Blur is for glass, not decoration.** Only where a solid fill would look heavy,
which in practice means only over artwork.

**Motion is arrival and feedback, and nothing else.** Something entering, or
something the reader did having an effect. No ambient loops, no decorative
movement.

**The accessibility mechanism is worth copying, because it is a rule and not a
value.** Any rule that hides content in order to animate it in must live inside a
`prefers-reduced-motion: no-preference` block. Then the reduced-motion branch has
nothing to un-hide, and nothing can ever be stranded invisible. Honour reduced
motion and reduced transparency as real branches, not as declarations.

**Focus is an outline, never a shadow,** so it can follow the shape of a rounded
object and never shifts layout.

**Citation.** The measured radius inventory, the depth rules, the motion timings
and the reduced-motion measurements are in `landing/DESIGN.md` §5.2, §9, §11 and
§13.

---

## 11. How the brand flexes per surface

These stay invariant on **every** Nova surface, without exception:

- One accent, and it is orange.
- The mark, drawn as the mark.
- The promise, and the limit rule.
- No em dashes, no exclamation marks.
- The product is shown, never imitated. Every screenshot, terminal and diff on a
  Nova surface is real output. A mock that could plausibly belong to another
  developer tool is a lie, and a Nova surface does not tell one.

These are free per surface:

| Surface | What it leads with | What is optional |
|---|---|---|
| Landing page | editorial prose in a terminal's chrome, the reference implementation | section count and order |
| TUI | the art, then the work. Chrome stays out of the way | every colour: the TUI themes are user-selectable, which is a feature |
| Web surfaces (`/cowork`, `/chat`, `/create`) | the landing page's tokens applied to a live surface | layout |
| README and docs | prose. No chrome at all | everything visual |
| Social and OG cards | the mark on the brand night-sky ink | nothing |

The TUI row is the important one. Its palette is derived from the user's chosen
theme on purpose, so `/theme` recolours it correctly. A brand document that tried
to pin those colours would be wrong the moment someone picked a different theme.
Brand governs the mark and the promise there; the theme governs the pixels.

---

## 12. The two tests

Before a change ships, both questions.

**The paste test.** Could this element be pasted onto any other developer tool's
site without looking wrong? If yes, it does not belong on a Nova surface. A Nova
surface has to be recognisable as Nova, and recognisability is the whole asset.

**The show-your-work test.** Does this surface let a reader inspect what Nova
actually did, rather than reading a description of what Nova did? Nova's claim is
verification. A surface that hides the verification is contradicting the promise.

---

## 13. Provenance and maintenance

Every rule here is cited to the file that owns it:

| Owned by | Carries |
|---|---|
| `landing/DESIGN.md` | voice, tokens, measured contrast, radii, motion timings, page structure and its own verification recipe |
| `novacode_cli/brand.py` | the art, the width tiers, the product accents |
| `landing/tools/make_brand_assets.py` | the source image and the crop boxes behind every derived asset |

**Keeping this file honest.**

- Cite, never retype. If a number you need is in a source file, read it there.
- When `landing/DESIGN.md` is re-measured, the rules here do not change, but check
  that the citation still points at the right section.
- `landing/DESIGN.md` §16 carries a standing instruction worth honouring before
  trusting any figure quoted from it: diff the landing page against the commit its
  measurements were taken from. If the diff is non-empty, the figures need
  re-measuring rather than repeating.

**One deliberate omission.** This file carries no token file and no stylesheet.
That is a choice, not an oversight: tokens drift silently and prose does not. If a
future surface needs a machine-readable copy, generate it from the source of truth
rather than hand-maintaining a second set.

**Verified against** `landing/index.html`, `landing/DESIGN.md`,
`novacode_cli/brand.py`, `landing/tools/make_brand_assets.py` and `README.md` as
they stood when this file was written. Re-check the citations if any of those have
moved.
