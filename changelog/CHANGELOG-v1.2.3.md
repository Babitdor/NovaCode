# NovaCode 1.2.3

## Visible, selective memory recall

- Show “Recalling memory” inline when relevant topic excerpts are automatically
  supplied to the model, with the source path when backed by a file.
- Show “Reading memory” after a successful explicit memory-page read, including
  reads made through Code Mode. Failed reads and search results do not claim a
  page was read.
- Keep standing preferences and the compact index available automatically.
  Search summaries and read relevant full pages when more detail is needed,
  rather than loading the whole memory wiki.
- Suppress duplicate notices for the same source and action within a turn.
  Notice events carry source metadata, not memory bodies, and do not add
  messages to model history. Existing permissions and retrieval limits apply.

## Memory sources

- Return the actual project-scoped topic path from memory search and automatic
  recall. General and project topics with the same name remain distinct.
- Identify generated knowledge without inventing a file path to read.

## Verification

- Add model-boundary, normal-tool, Code Mode, shared-stream and TUI regression
  coverage for memory notices.
- Fix workspace-mock cleanup in resume tests so later async-agent tests use
  their own workspace correctly.
