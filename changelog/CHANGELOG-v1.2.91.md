# v1.2.91 — The /sessions picker shows its options again

**Released 2026-11-27** · PyPI `novacode-cli==1.2.91` · tag `v1.2.91`

## Fix: modal lists collapsed to an empty bordered strip

`/sessions` opened its picker with the two choices in the widget's model but
**zero rows of screen space** — the modal rendered as a single bordered strip
that looked like an empty input above Select/Cancel. The same collapse hit
every modal list sharing the rule (`#sessions`, `#pick-list`, `#infolist`,
plugins, skills, hooks, wiki pages…).

Root cause: the shared rule capped the lists with `max-height: 40%`, but their
parent modal (`#modal-box`) is `height: auto` — a percentage against an
indefinite parent resolves to zero, and the auto-height computation clamped
the list to nothing.

- The cap is now absolute (`max-height: 16`): short lists size to their
  content, long lists scroll inside the box as before.
- New regression tests assert the picker and the saved-sessions screen give
  every option actual screen space, not just a populated model.
