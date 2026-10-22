# NovaCode v1.0.81

## Improvements

- Base Nova's packaged standing instructions on the supplied `prompts/agent.md`
  reference: preserve memory and user work, recall plans, respect project scope,
  investigate before editing, verify results and communicate concisely.
- Share those instructions between the startup agent's `core_agent_system.jinja`
  and the default/continuation `System_Prompt_Nova.jinja`. Keep runtime tool
  guidance and user memory separate. The shared block is packaged and cannot be
  replaced by global prompt-history or activated-artifact overrides.

## Tests

- Ensure both main prompt paths render the same packaged standing rules.
- Keep update-restart TUI tests offline: disable automatic background update
  checks while their manual update operations remain explicitly stubbed. This
  prevents a real update request from holding the Windows test executor open
  during certificate loading and teardown.
