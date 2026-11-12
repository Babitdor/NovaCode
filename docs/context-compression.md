# Model-controlled context compression

Nova uses a native Python adaptation of
[billion-context](https://github.com/ranxianglei/billion-context) and its
[acp-kernel](https://github.com/ranxianglei/acp-kernel) range-compression design.
The reference checkout is commit `9674cfafbc78c50245ae0d887483d3d6ce880c15`.
The upstream copyright, license and product-attribution terms ship in
`novacode_cli/context/licenses/billion-context.txt`.

## How it works

Context compression is enabled by default. The active model decides what
completed work to compress and writes the summary through its ordinary tool
loop. No additional provider, Node.js runtime, proxy or background service is
required. Tool calls and summary text use the active model's normal token budget.

- `acp_status`: inspect estimated working-history tokens, stable message and
  block references, summary tiers and the protected recent tail. The result is
  paginated; its token estimate excludes system instructions and tool schemas.
- `compress(start_ref, end_ref, summary)`: fold an inclusive range into a
  detailed summary. Existing blocks can be combined with newer material into
  second- and third-tier summaries. Call this tool alone, without parallel tools.
- `search_context(query)`: search visible history, all block summaries and exact
  archived message contents in the current conversation. Results contain bounded
  excerpts and the block references needed for recovery.
- `decompress(block_id, offset, limit)`: recover exact original message JSON,
  including nested blocks. Follow `next_offset` to retrieve another page. Pages
  are capped at 20,000 characters, so restoring a large block cannot itself flood
  the context. Evidence returns as tool output, not privileged instructions.

Nova prompts the model to inspect and compress completed work when message-token
estimates reach 45% of the context window. Nova's existing configured automatic
compaction threshold and provider-overflow recovery remain the fallback when the
model does not compress enough. `/compact` also remains available.

Compression must actually reduce tokens. System messages, the recent three
messages, incomplete tool exchanges and partially overlapping blocks cannot be
folded. User messages and their multimodal content remain verbatim in the model
view. Each successful fold updates its summary and archive in one checkpoint
transaction. Original transcript messages are not rewritten by these tools.
Summary references remain stable as new messages arrive, avoiding gratuitous
prefix changes. A fold necessarily changes the prefix at its selected range;
no provider cache-hit rate is promised.

## Persistence and scope

Blocks and the original-message archive belong to the graph thread and persist
with Nova's existing checkpointer. Reopening a session restores them. They are
not shared memory-Wiki entries and do not expose another session's content.
The archive remains searchable after automatic or manual compaction replaces
working history. Summaries are model assessments, not proof or authorization.

This adaptation reduces model context, not checkpoint storage. Original messages
remain in the checkpoint archive, and storage grows with conversation history.
Billion-token storage scaling and upstream benchmark improvements have not been
established for Nova. The upstream proxy adapters, native-provider compaction
interception, optional CCR/absorb/crush transformations and semantic search are
not part of this integration. Nova retains its existing tool-result offload and
overflow handling.

To disable new model-authored folds, set `context_hierarchical` to `false` in
Nova's existing configuration. Previously folded history remains readable and
retains its projection; the normal compaction fallback stays enabled.

## Validation

Tests exercise complete tool exchanges, exact paginated recovery, nested blocks,
thread isolation, async execution, reopened SQLite checkpoints, direct tool
availability with Code Mode, and automatic fallback after an earlier fold.
These use scripted models; live-provider summary quality and long-session cost
improvements require evaluation with real workloads.
