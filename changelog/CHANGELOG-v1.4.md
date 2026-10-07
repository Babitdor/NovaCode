# Changelog - v1.4

## New Features

### Per-Turn Model Routing (`/router`)
- **A route per kind of work**: Nova can hold several routes, each a `provider:model` pair, and pick one for each turn instead of running every turn on a single model
- **Criteria are the classifier's prompt**: A route carries a plain-English description of the work it should win, which is what the decision model reads when choosing. A route without criteria is refused rather than silently never selected, because a route the classifier cannot match is worse than one that is missing
- **`/router` screen**: A native modal to add, edit, remove and reorder routes. The screen edits a working copy and returns a payload, so Cancel has no side effects and nothing reaches the config until Save
- **Decision model is configurable**: Presets for **Jev** (TypeSafe System One API), **Tev1** (local Ollama, the default and free), or a custom endpoint and model name
- **Off until switched on**: Routing defaults to disabled, so an existing `Nova.config.json` behaves exactly as before and a session that never opens `/router` is unaffected
- **Falls back rather than fails**: If the classifier is unreachable, returns something unreadable, or names a route that cannot be built, the turn runs on the default route - a router that could break a turn would be worse than no router
- **Routes live in `Nova.config.json`**: `router_enabled`, `router_routes`, `router_default_route`, `router_decision_endpoint`, `router_decision_model` and `router_min_confidence`, written through a validating setter so a typo caught in the screen never reaches model construction mid-turn

### Settings Screen (`/settings`)
- **Native preferences modal**: Opens from `/settings`, applies immediately, and persists to `Nova.config.json`
- **Matrix Rain switch**: Turns the home-banner animation on or off without editing the environment or restarting

## Improvements

### Idle Resource Efficiency
- **Every numerical backend is capped, not just OpenBLAS**: `OPENBLAS_NUM_THREADS` was already pinned to 1, but OpenMP, MKL and NumExpr still defaulted to a machine-wide thread pool, so each Nova process - the TUI and every spawned session - could occupy every core while idle. All four are now pinned with `setdefault`, so an explicit user value still wins
- **Voice models load on demand**: A text session no longer constructs or preloads the native voice models. Previously any of `enabled`, `speak_responses` or push-to-talk triggered the full model load at boot, so a user who had never spoken to Nova paid a large load for a feature they had not touched. Only explicitly enabled voice builds a pipeline now, and the models load on first use
- **Speech does not allocate the microphone stack**: `VoicePipeline.warmup` takes `input_audio=False` to build the TTS side alone, and `_ensure_components` splits into `_ensure_input_components` and `_ensure_tts` so each direction loads only what it needs. Explicit `/voice download` still warms the full stack
- **The home banner no longer repaints forever**: `MatrixRain` animated roughly 15 times a second on an idle terminal. It is now off by default, keeps its first themed frame, and stays available as an opt-in through `NOVA_ANIMATIONS=1` or the new Settings switch. Resizing and theme changes still repaint a static banner
- **Live tool output is bounded by construction**: The pending-output map held an unbounded `list[str]` per call, so a chatty command grew it until the painter happened to drain it. `OutputTail` now caps each call by bytes and by lines, and `MAX_PENDING_CALLS` caps how many calls may be buffered at once. Dropping a call trims only the live display - final tool results still arrive through the normal event/session path

## Bug Fixes

- **Banner animation could never start**: `MatrixRain.on_mount` enabled the rain through `set_animation_enabled`, which returns early when the widget is not mounted - and `is_mounted` is still `False` inside `on_mount`. Setting `NOVA_ANIMATIONS=1` therefore produced a static banner. The timer start moved into `_start_timer`, which `on_mount` calls directly and `set_animation_enabled` delegates to.

## Technical Changes

- **`OutputTail` extracted to `novacode_cli/tui/output_buffer.py`**: Self-contained (no TUI imports) so the bounded buffer can be tested and reused independently of the app that consumes it
- **Test coverage**: `tests/test_resource_efficiency.py` pins the thread-pool caps, lazy voice loading, the opt-in banner, and the buffer ceilings; `tests/test_model_router.py` covers route selection, fallback, and config round-trips
- **Measurement scripts**: `scripts/benchmark_idle.py` measures an idle process, `scripts/verify_resource_efficiency.py` checks the claims against a running build, and `scripts/mutate_resource_efficiency.py` re-injects each defect to prove the tests are not vacuous
- **Documentation**: `Performance-Spec.md` states the acceptance criteria the work was implemented against and `Performance-Evidence.md` records what was measured against them - including that the originally reported freeze was not reproduced, so the fix is not claimed to resolve it

---

## v1.3 - Previous Release

See [`CHANGELOG-v1.3.md`](CHANGELOG-v1.3.md) for the Council of Agents, the HITL policy engine, the LangGraph platform deployment, the LLM wiki, the eval harness, the plugin system, and per-role model selection.
