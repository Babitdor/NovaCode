# NovaCode 1.0.20 — OpenCode Zen and guided Console sign-in

- Add OpenCode Zen alongside OpenCode Go in /auth, /model, router targets, role models, and first-run setup. Zen uses its own API key and endpoint; existing Go configuration remains compatible.
- Offer “Sign in to OpenCode Console” for both services. Open the official Console in a browser, then return to Nova's masked API-key field. This implements OpenCode's documented Console/key flow, rather than claiming unsupported third-party OAuth.
- Discover available models independently for Zen and Go. Select Chat Completions, Responses, or Anthropic Messages according to each model's documented API. Exclude native Google and System One models from the conversational picker.
- Identify gateway traffic as NovaCode, retain the stable session header, mask saved Zen credentials in settings, and strip them from shell subprocess environments.
- Avoid reporting a public model-list response as proof that an OpenCode key was authenticated.

Official setup: https://opencode.ai/docs/zen/ and https://opencode.ai/docs/go/.
