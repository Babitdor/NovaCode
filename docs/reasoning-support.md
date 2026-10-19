# Reasoning controls

Audited 2026-10-19. `/effort low|medium|high|off|default` saves the preference
and rebuilds the current model. `default` removes the override. The selected
model, provider and shared steering list are preserved.

| Provider / route | Request control | Limits |
| --- | --- | --- |
| OpenAI, including ChatGPT sign-in | Chat Completions `reasoning_effort`, or Responses `reasoning.effort` | Recognized reasoning models only. `off` requests `low`; models without reasoning receive no override. |
| Anthropic | Adaptive `thinking` and `output_config.effort`, or an explicit thinking budget for older models | Adaptive models use minimum effort for `off`. An explicit `thinking_budget` still takes precedence on older models. |
| Google | Gemini 3+ thinking level; Gemini 2.5 thinking budget | Gemini 3 Pro maps medium to high. Models that cannot disable thinking use the minimum. Gemini 2.0 receives no thinking settings. |
| Command Code | Claude uses the Anthropic route; GPT uses OpenAI effort; DeepSeek V4 uses low/high effort | DeepSeek maps medium to high and off to low. Other families retain defaults until their controls are verified. |
| OpenCode Go / Zen | Claude now receives the same thinking settings as native Anthropic; GPT uses Responses effort; DeepSeek V4 uses low/high effort | Non-Claude Messages models and other unverified families retain defaults. Native Gemini remains excluded from these chat routes. |
| OpenRouter | Unified `reasoning.effort` in the request body | Gateway translates to the upstream. `off` requests low. Actual support depends on the selected model. |
| Ollama | OpenAI-compatible `reasoning_effort`; off becomes none | Support depends on the installed model and server. |
| NVIDIA NIM | `chat_template_kwargs.thinking` | Boolean control: low, medium and high all enable thinking. Support depends on the model template. |

`off` does **not** universally mean zero reasoning tokens. Some models require
thinking, and some routes expose only a minimum effort. `/effort` reports the
mapping for the active provider/model instead of claiming universal support.

## Evidence and limits

Offline regressions inspect constructed SDK payloads and check reasoning
stream preservation. They exercise all nine registered providers through the
provider-specific suites. They do not establish that every hosted model accepts
or honors those settings. No authenticated inference or paid evaluation was run.
Command Code's public model-list endpoint was read without credentials to check
current model identifiers and supported routes.

References:

- [Command Code Provider API](https://commandcode.ai/docs/provider): native OpenAI and Anthropic request routes.
- [OpenRouter reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens): unified gateway controls and model-dependent support.
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/): low/high effort mapping.
- [OpenCode models](https://opencode.ai/docs/models/): provider and model-specific options.

## Status line and approval mode

`/statusline` opens a scrollable checkbox picker with a live preview. Save
persists the selection; Reset restores the default draft; Cancel discards it.
An empty selection is allowed. Settings apply to the status line across designs.

`/auto-approve` toggles the current session; `on`, `off` and `status` are also
accepted. It changes the composer to the theme's warning color and shows an
explicit mode label. The setting is session-scoped, including child tabs and
Telegram topics, and does not change the persistent launch default. Changing it
does not revoke a tool call already in flight or approve an already-open dialog.
