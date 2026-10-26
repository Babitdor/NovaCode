# Workspace knowledge and teammate input

Local main-agent sessions expose `knowledge_lookup(query, paths=[])` for code
and documentation orientation. A query alone finds up to three relevant stored
overviews. Supplying up to four relative text paths reuses or generates a general
overview independent of query wording. A miss directs the agent to source tools.
Edits, quotations, exhaustive analysis and verification still require exact reads.

`/cache status|clear|on|off` acts on the live workspace and assistant identity.
`semantic_cache_enabled` defaults to true and persists per namespace in the
profile's `cache/knowledge/knowledge.sqlite3`. Off also clears retained artifacts.
Clear/off invalidate in-flight producers. Restricted sessions, delegated agents
and VM-only teammates do not receive the host knowledge tool.

The owner-private SQLite directory contains summaries, file references and full
content hashes, model/prompt/embedding identities, embeddings, expiry and numeric
access counters. It does not store queries, raw sources or conversation history.
Every hit rereads complete sources through the invoking backend. Symlink escapes,
traversal, private runtime paths and credential/key files are excluded. Files must
be UTF-8 text, with at most 32 KiB across four files. Summaries are limited to
4,000 characters, expire after seven days, and are evicted least-recently-used
above 2,000 per namespace or 64 MiB of retained payload across the profile.

Semantic lookup requires an already installed Model2Vec snapshot. Knowledge
lookup never downloads weights; without them exact source-set reuse still works.
Embedding and SQLite work run off the event loop. Summary generation uses the
active graph model with no tools or history, no SDK retries and a 60-second
deadline. Cache failures fall back to existing source tools.

`/cache status` reports exact/semantic hits, misses, invalidations, generation
calls, total lookup count/time and retained bytes. `avoided_tokens_estimate` is
only a rough character-based estimate for exact reuse. Cold provider calls and
tokens appear under `knowledge_summary` in the session usage tree. There are no
dollar-savings or provider-cache-price claims.

Each existing teammate's Work tab has **Auto approve desktop input**, off by
default. It automatically allows future clicks, typing and keyboard proposals
within the assigned task. Pending input still requires Approve once or Deny.
The authenticated owner command is `set_auto_approve` with an explicit `agent_id`
and a JSON boolean `auto_approve`. The switch and command are independent of
global auto-approve and other teammates. Exact intent, screenshot, decision source
(`owner` or `auto_approve`), issued state and result remain durable. Pause,
takeover, observation checks, VM ownership and input limits still apply. Restart
retains the setting but pauses work until Resume; uncertain input is not replayed.

## Verification

Run `python -m scripts.verify_knowledge_approval` for focused offline regressions,
or add `--mutations` for explicit adversarial mutations (restored after each run).
The runner prints commands/results and records its dependency versions.
`python -m scripts.replay_token_efficiency --output .tmp/token-replay.json`
includes repeated-overview, stale-source and unauthorized-source fixtures. The
report labels them synthetic offline measurements, never production savings.

An optional live-provider smoke test incurs normal model charges: launch Nova
with a configured provider in a disposable workspace, run `/cache clear`, ask for
`knowledge_lookup` with one small file, then repeat with paraphrased wording and
the same path. Check `/cache status` for one generation and one exact hit, and
the usage tree for only one `knowledge_summary` call. Change the file without
changing its length and repeat; it must generate a fresh overview. Run `/cache
off` and repeat; no new summary call should occur. Clear the disposable cache
afterward. No live-provider smoke test runs automatically.
