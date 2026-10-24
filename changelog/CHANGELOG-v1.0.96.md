# NovaCode 1.0.96

## Preserve reply language in Wenyan mode

- Keep the user's language at every Caveman intensity unless another language is explicitly requested. Selecting Wenyan no longer asks Nova to translate replies into Chinese.
- Apply classical Chinese styling only to Chinese replies. Use matching Lite, Full or Ultra compression for replies in other languages.
- Cover all three Wenyan levels in main-agent and subagent request paths, preserving model selection and message history.
- Clarify the Options tooltip and document language behavior. Extend the optional live inference script to accept every intensity.

Validation: 68 focused tests passed, plus six live English/Portuguese replies using the saved model preserved their requested languages. These probes establish language behavior for those samples, not overall correctness or token savings. No dependencies added.
