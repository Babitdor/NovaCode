# v1.2.92 — The image preview no longer stutters

**Released 2026-11-27** · PyPI `novacode-cli==1.2.92` · tag `v1.2.92`

## Fix: "Image read … — click to inspect" preview stuttered and glitched

Two measured causes:

- **The home banner kept animating under the modal.** Every rain tick
  (15 fps) marked the screen damaged, and the compositor re-blended the
  preview's full-screen image with it — measured p99 frame gaps of 159 ms
  with the preview open. The rain's tick now holds still whenever a modal
  screen sits on top (the screen stack, not `app.screen`, which can lag a
  frame during the push). Measured with the fix: zero frames built under the
  modal, and a new regression test pins it.
- **The half-block fallback was built on the UI thread.** The preview's
  fallback render carries thousands of styled spans; building it inside the
  load worker stalled the frame budget right when the preview opened. It now
  builds in a worker thread like the image decode.

A preview bench (`scripts/bench_image_preview.py`) measures the frame budget
with a 2400×1500 screenshot on a 160×40 grid and stays in the tree for future
changes.
