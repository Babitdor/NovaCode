# NovaCode v1.0.69

## Improved

- Refined the Council web workspace with a slate and sea-glass palette, clearer decision brief, editorial welcome typography, and polished advisor, question, verdict, and composer treatments.
- Added Council settings to the web header with all controls from the inline editor: six member identities, judge, personality templates, system prompts, and provider/model overrides with session inheritance.
- Web settings use the same validation and saved configuration as inline Council. Templates, resets, and seat changes remain private drafts until Save; Cancel discards them. Active discussions retain their configuration.
- Added responsive settings navigation, keyboard focus handling, model ID suggestions, and clear labels for the planning-only advisor and judge.

## Validation

- Browser checks cover settings drafts, templates, model overrides, save errors, persistence requests, reset, cancel, and narrow-screen layouts alongside existing streaming and decision controls.
- HTTP checks verify shared persistence, validation, provider metadata, cross-origin save rejection, and preservation of the main session model. No new runtime dependencies.
