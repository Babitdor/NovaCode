# NovaCode interface styles

Open `/theme` and choose **Interface style**. Selection applies live and persists
between sessions. Color themes remain independent, including light themes.
Dashboard, Classic, Centered, Banner, and Minimal remain available.

## Eight additional designs

| Style | Home | Conversation |
| --- | --- | --- |
| Terminal | Typewriter shell opening and four compact command rows | Open transcript and a single prompt rule |
| Workbench | Full Nova character beside a two-by-two tool desk | Bounded tools and square composer |
| Notebook | Field-note heading and ruled task entries | Ruled prose with generous spacing |
| Outline | ASCII-only frame ornaments and task buttons | Plain ASCII frames for messages and dialogs |
| Focus | Centered wordmark and quiet starting links | Narrow reading column and restrained chrome |
| Studio | Centered character lockup with a constellation shimmer | Rounded message cards and composer |
| Ledger | Actual model/workspace/session facts and a dense action strip | Compact entries and a double command rule |
| Blueprint | Scanning specification/build/verify drawing and double-rule actions | Technical headings and double-line borders |

Each new home provides Explain, Refactor, Build, and Debug starters. Clicking or
focusing a starter and pressing Enter fills the existing editable composer;
only sending the prompt starts work. Changing styles preserves an existing
draft and mounted conversation. Short terminals hide decorative home content
and the starting actions to leave room for typing. Workbench and Studio reuse
Nova's canonical character art. Terminal, Studio, and Blueprint animate while
home is visible; Notebook, Outline, Focus, Ledger, and Workbench stay quiet.

Use **Animated home scenes** in `/theme` to disable motion without changing the
style. These effects share Nova's existing clock, capped at six frames per
second, and stop during chat, dialogs, OS focus loss, low-resource mode, or a
small terminal. No separate animation timer or worker is created. Reduced
motion displays the complete static artwork; tight layouts hide the large
portrait and keep the task starters and composer usable.

The styles share Nova's existing message, tool, modal, and composer widgets.
Plan-mode formatting remains separate. Style classes are changed in one batch
per screen, avoiding one full restyle for every available preset.

## Design references

These are original Nova layouts informed by terminal interaction patterns;
no external app code, logos, or themes were copied.

- [btop](https://github.com/aristocratos/btop): framed regions, clear panel hierarchy,
  and dense information. Its actual terminal screenshot was reviewed.
- [Lazygit](https://github.com/jesseduffield/lazygit): bounded workspaces and compact
  task-oriented navigation.
- [Nushell quick tour](https://www.nushell.sh/book/quick_tour.html): ruled structured
  output and readable labels, informing Notebook and Ledger.
- [Zellij theme components](https://zellij.dev/documentation/themes.html): separate
  component roles and emphasis while keeping colors independent from layout.

Verification exercises all eight homes, keyboard task starters, chat/tool
visibility, plan mode, light themes, dialogs, and a 48-by-18 terminal. Actual
Textual SVG captures were inspected. The added stylesheet is included as wheel
package data; no new runtime dependency is required.


Final local verification: 8 new-preset lifecycle cases and 34 existing home,
style-switching, dialog, completion-menu, and toggle checks passed. Tests ran
with an isolated child-process home because another active Nova session was
changing its own preferences during development. The real user configuration
was not restored or overwritten. New/affected standalone lint and home-module
mypy passed, and legacy-file lint did not worsen. A wheel build contains the
exact current stylesheet bytes. Linux rendering remains for CI verification.


Character-scene follow-up: the standalone portrait now comes from the shared
brand source. Workbench uses a two-column character/tool composition, while
Studio uses the full lockup as a centered constellation canvas. Animated text
frames keep a stable row count to avoid shifting the composer. Tests exercise
frame changes, art reuse, the shared clock, modal/blur/low-resource pauses,
saved opt-out, and portrait-to-compact resize. No new runtime dependency.

Follow-up validation: 15 scene/preset lifecycle cases plus 141 existing animation,
home, dialog, and brand checks passed. Character captures were reviewed on
Windows; the wheel includes the current scene module and stylesheet. Linux
terminal rendering remains unverified locally.
