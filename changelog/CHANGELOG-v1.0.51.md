# NovaCode v1.0.51

## Added

- Nine providers in `/auth` and `/model`: Azure OpenAI (v1 API; endpoint in
  `/auth` or `AZURE_OPENAI_ENDPOINT`), Amazon Bedrock (optional `[bedrock]`
  extra; Bedrock API key or any AWS credential), Mistral, Groq, Cerebras, xAI,
  Hugging Face, Kimi For Coding and MiniMax. Model lists come from the cached
  models.dev catalog, curated ids first.
- Session history is a tree in one append-only `tree.jsonl` per session.
  `/tree` browses every branch, filters by message type, bookmarks entries and
  continues from any earlier point without losing the old branch.
- `/export [file]` writes a self-contained HTML page of the session tree, with
  token-shaped secrets masked. `/share` uploads it as a secret GitHub gist
  (after a confirmation) and prints a link that renders it.
- `/nova` shows a pixel avatar above the composer that reacts to what the agent
  is doing. Frames are a 64-pixel sprite sheet drawn with octant glyphs, or
  half-blocks where octants are unavailable.

## Fixed

- The `/mcp` dialog's buttons sit in two rows of three, so Close is no longer
  cut off on terminals between about 90 and 120 columns.
- A refused MCP OAuth registration (403) now says why. For Figma's hosted
  server, which only admits approved clients, it points to the `figma-desktop`
  preset. Connection errors are unwrapped from task-group exception groups.

## Removed

- The native Windows avatar overlay window (128-pixel RGBA frames). It crashed
  Windows Terminal; the character-cell avatar is used on every platform.

## Validation and limits

- Focused runs passed: 86 tests across sessions, avatar, providers, MCP and
  status-line controls, then 12 MCP tests including a button-fit check at 90,
  100 and 120 columns.
- Live calls to the nine new providers were not made (no keys available).
  Kimi For Coding may restrict its plan to approved coding agents.
- `uv.lock` adds `langchain-aws` and boto3 for the optional `bedrock` extra
  only; the base install is unchanged.
