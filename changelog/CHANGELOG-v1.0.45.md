# NovaCode v1.0.45

## Fixed

- `/effort` on Claude Sonnet 5.5 (and other Claude 4.6+ and Fable models) failed with "`thinking={"type": "enabled", "budget_tokens": ...}` is not supported". Nova now sends adaptive thinking with `output_config.effort` to those models and keeps `budget_tokens` for older ones. "off" is the lowest effort, since disabled thinking is rejected by Opus 5.5, Sonnet 5.5 and Fable.
- OpenAI: `/effort` reaches every reasoning model (o-series, GPT-5 and later), not only names containing o1/o3, and is no longer sent to models that reject it (gpt-4o, gpt-4.1, `*-chat`, o1-mini/preview). "off" is "low", since "none" and "minimal" are not accepted by every model.
- Gemini: 3.x and later get `thinking_level` (Gemini 3 Pro has no medium), 2.5 gets `thinking_budget` within each model's limits ("off" is 0 on Flash and the 128 minimum on Pro), and 2.0 gets no thinking setting.

## Validation and limits

- 34 tests cover each model family, including the requests langchain builds for Claude, OpenAI (both APIs) and Gemini 3; the model-build, NVIDIA and TUI effort tests pass.
- Not tried against the live providers.
