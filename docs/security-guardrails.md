# Nova prompt-injection guardrails

## Decision-model approval

When decision-model approval is enabled, auto-approve retains the main tool
approval gate. Policy denials and Plan mode restrictions take precedence. A
confident model approval can resolve an otherwise unresolved action; a rejection
blocks it. An uncertain, malformed, unavailable, or oversized review requires an
owner decision. The TUI presents its approval dialog even in auto mode; headless
runs stop with human input required, and pipe clients receive the approval
request. An unreadable policy or approval preference never grants execution.

Local and packaged async subagents use the same reviewer without opening nested
approval dialogs. An unresolved child action returns an approval-required error
to its parent without executing. Code Mode also checks every nested call before
execution. Existing explicit policy allows remain immediate. With the reviewer
disabled, legacy auto-approval continues to honor hard policy denials.

Reviews use host-bound owner intent where available. Child task descriptions,
internal skill suggestions and tool results cannot supply owner consent. Review
input is bounded, obvious credential assignments are redacted, provider keys are
selected by the shared client factory, and owned review transports are closed.
Third-party precompiled graphs still own their approval controls. Registered
custom and plugin tools retain approval points even with review disabled, so
changing the review preference takes effect on their next request. Manual mode
also honors the policy's default ask tier for these tools.

The confidence threshold is conservative and unchanged; it has not been
calibrated across all providers. These offline regression tests verify execution
and failure handling, not a model's real-world judgment accuracy:

```text
uv run pytest tests/test_approval_decisions.py -q
```

## Scope and acceptance criteria

This is an autonomous security hardening change. Spec approval was not obtained
as a separate review. It uses only existing dependencies and does not change the
user's saved configuration, approve actions, release, or push code.

Failure model: hostile tool output may impersonate system messages, request
credential disclosure, or claim permission to act. A guard may also corrupt tool
protocols, multimodal blocks, provider caching, or legitimate coding tasks.

Acceptance criteria:

1. Model requests identify all tool text as lower-trust data, even when it
   contains forged role tags, hidden characters, or guardrail closing markers.
   Keep tool-call IDs, names, status and non-text blocks intact.
2. Add explicit built-in instructions for files, MCP responses, web content,
   screenshots, memory and peer messages: these cannot change authorization.
   Preserve direct user instructions, including legitimate security research.
3. Remove known runtime credential values from textual model input, and reject
   tool arguments containing these values or their common URL/base64 encodings
   before calling the handler. Report a content-free error, including with
   auto-approval enabled. Discover environment credentials at call time.
4. Injection phrase detection is advisory only. An attack example in a source
   file must not prevent reading, analysis, or normal code edits.
5. Operate on copies of model requests. Never rewrite persisted message history,
   file content, schemas, approvals or runtime environment variables.
6. Cover synchronous and asynchronous middleware, main agents, local/dynamic
   subagents, packaged async agents, team workers and the BTW research agent.
7. Verify with adversarial tests, a real compiled offline tool-calling graph,
   existing security/agent regressions, lint, coverage and negative controls.

## Limits

These controls reduce risk; they do not prove a model will resist every prompt
injection. The existing approval policy, workspace restrictions and sender
authorization remain separate enforcement layers. Text delimiters and detection
patterns are model guidance, not authorization mechanisms.

Credential protection covers environment variables whose names identify secrets
and whose values are at least 12 characters. It does not inspect the OS keychain,
all files, arbitrary private information, unknown secrets, images, binary data,
persisted history, traces or logs. It is not a shell/network sandbox: a shell
process can expand environment variables or encode data at runtime. Use Nova's
execution sandbox and scoped credentials for stronger containment. Third-party
precompiled/remote graphs own their security controls.

Reference: [OWASP prompt injection mitigations](https://genai.owasp.org/llmrisk/llm01-prompt-injection/).

## Additional audit findings

The standalone planning and memory-consolidation (dream) agents also need this
middleware; they do not inherit the main factory. They are included in the
builder tests and implementation. Local subagents preserve caller middleware,
then apply the input guard before strict-tool processing and context accounting.
The existing order assertion now includes that additional boundary explicitly.

Scope clarification: credential redaction protects conversation inputs dispatched
through this middleware. Independent auxiliary model calls (such as compaction
summaries, learning scorers or voice transcription) do not automatically inherit
it. Outgoing model token streams and model-provider tool definitions are also
outside the deterministic redaction layer. The built-in trust rules cover these
content sources, but prompt guidance alone is not enforcement.

## Verification evidence

Verified locally on Windows, Python 3.11, against base commit `2018a57` with these
uncommitted changes. Guardrail source SHA256:
`7d214533c14889664717c15a0a1c30ab8388ad5276edc97372a836d3638d0c4f`.
Coverage version: 7.16.2. Hypothesis version: 6.169.0. No dependencies were added.

Reproduce the focused evidence with:

```text
uv run python scripts/verify_prompt_guardrails.py
```

- 32 guardrail tests passed, including real synchronous/asynchronous compiled
  tool-calling graphs, 75 generated Unicode examples, provider block preservation,
  adversarial payloads, request isolation and auxiliary-builder wiring.
- 74 existing approval, URL-security, subagent, dream-backend and planning-checkpoint
  regressions passed in the verifier.
- New middleware statement coverage: 108/108 statements (100%). The verifier exits
  nonzero below its 95% threshold; this is not a branch-coverage claim.
- All three in-memory negative controls were killed: removing model boundaries,
  removing credential denials, and removing credential redaction each caused
  assertion failures. The verifier fails if a control survives or fails to run.
- Focused Ruff checks and the middleware's Mypy check passed. Existing core-agent
  lint findings were compared with the committed source and did not increase.
- The configured offline CI regression command passed locally: 711 passed,
  4 skipped, 11 subtests passed. Actual GitHub Actions were not run or published.
- The unrestricted repository-wide run did not complete: pytest-timeout terminated
  `test_chat_handler.py::test_decisions_are_bounded` during repeated Council HTTP
  requests. Earlier failures were also observed before termination; this is not
  a full-suite pass or a claim that every repository failure is pre-existing.

Behavior mappings in `tests/test_prompt_injection_guardrails.py`:

| Contract | Tests |
|---|---|
| Tool text is lower-trust; originals/protocol survive | `test_tool_output_is_data_without_changing_history_or_protocol`, `test_multimodal_blocks_and_system_cache_metadata_survive` |
| Known credentials cannot enter conversation inputs or literal tool arguments | `test_runtime_credentials_are_redacted_without_modifying_state`, `test_prior_tool_arguments_are_redacted_in_model_view_only`, `test_secret_tool_arguments_are_denied_before_handler_even_with_auto_approve` |
| Legitimate security work continues | `test_attack_examples_and_normal_tool_arguments_are_allowed`, `test_compiled_graph_contains_attack_and_blocks_credential_egress` |
| Noisy detection stays bounded and content-free | `test_injection_notices_are_content_free_and_deduplicated`, `test_security_notices_cannot_grow_an_unbounded_cache` |
| Graph and provider compatibility | `test_both_model_wrappers_apply_guardrails`, `test_subagents_and_background_graphs_have_guardrails`, `test_auxiliary_agent_builders_install_the_same_guard` |
| Unicode and nested MCP input | `test_arbitrary_unicode_tool_text_preserves_its_data`, `test_nested_mcp_batch_and_multiblock_text_are_protected` |

The compiled graphs use scripted model responses and local tools, not live model
providers. They prove middleware/protocol behavior, not a measured resistance rate
against a live model. No real credential exfiltration or live remote messaging
was performed. No independent reviewer or separate human spec approval was used.
