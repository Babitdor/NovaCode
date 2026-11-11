# NovaCode 1.2.7

## More relevant long-term memory search

- Keep learned topic memories out of automatic recall; search them only when
  `memory_search` is explicitly used.
- Retrieve candidates locally with semantic matching, then use the currently
  selected model to rerank a bounded shortlist.
- Split corpus loading, retrieval, excerpts and reranking into focused modules.
- Keep reranking provider-neutral and restrict it to reordering retrieved
  candidate IDs; invalid rankings fall back to local relevance order.

## Verification

- Focused memory and model-router tests: 157 passed.
- Ruff and mypy checks passed for the extracted memory modules.
