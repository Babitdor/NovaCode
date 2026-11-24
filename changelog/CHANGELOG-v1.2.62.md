# NovaCode 1.2.62

## Serena toggle in the prompt composer

- Serena Mode now has a `Serena: off/on` button in the composer's toggle row,
  right beside the Code Mode toggle — same interaction model: click to flip,
  the badge/prompt-styling update, and the button label mirrors the live flag
  however the mode changes (`/serena`, the button, or session reset).
- Turning it on from the button still goes through the Serena MCP gate: an
  unconnected server keeps the mode off and surfaces the `/mcp` guidance.
