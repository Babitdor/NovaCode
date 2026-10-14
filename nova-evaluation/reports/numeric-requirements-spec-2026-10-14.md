# Numeric requirement check: executable specification

Trace evidence: the historical `financial-document-processor` trial wrote
`total_amount=6236.77` and `vat_amount=623.68` for one invoice. The instruction
requires `total_amount` **including tax**, and the verifier expected 6860.45.
The same category appears on several invoices. The current baseline replay was
launched before this proposed prompt change.

## Acceptance scenarios

1. When a task asks for a computed numeric value, the general agent workflow
   directs the model to identify what each source number includes or excludes,
   check units, and work one explicit example against the requested definition.
2. When a requested value includes a component such as tax or a fee, the agent
   must check whether the source total already includes that component before
   adding it. It must check the final aggregate against its rows or parts.
3. The guidance reaches a model call in an actual agent graph. It must not
   instruct the model to always add tax or change nonnumeric tasks.
4. A matched replay uses the same task revision, model, reasoning setting and
   environment as the current baseline. A one-task gain or regression is
   exploratory, not a general accuracy estimate.

No dependency or environment change. Edit the general workflow prompt and
add a focused graph test. Run RED, GREEN, focused regressions, lint/format,
and review the diff. Keep the full suite separate due the previously requested
time and credit limit.

Spec approval: not obtained (autonomous run after the user's `Proceed`).

## Addendum after first matched replay

The first candidate replay corrected the tax-inclusive amounts but still scored
0: it classified an order/shipping document with a price as an invoice and
produced one extra CSV row. One additional scenario is needed:

5. For classification tasks, NovaCode checks ambiguous items against the full
   requested category definition rather than one shared cue. In particular,
   a price field alone cannot establish a document type. The prompt reaches
   a model call in the existing scripted graph test. Run one final matched
   replay, then stop live trials regardless of its result.
