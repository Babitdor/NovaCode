# Plan: a RAG evaluation harness

Derived from **The RAG Harness Field Report** (artifact `79511f7c`), itself merged from four
parallel research passes. Every number and claim below traces back to that report; the
report's sources are listed at the end.

The plan is written to be executed phase by phase, with a check per phase. A phase is not
done until its check passes.

---

## 1. Goal and non-goals

**Goal.** A harness that tells us, after any change to a retrieval-augmented system, whether
it got better or worse, why, and at what cost, on evidence we can defend.

**Non-goals.** Not a leaderboard of models. Not a demo dashboard. Not a framework. We are
not building distributed orchestration, dataset versioning UI, trace storage, or judge
calibration machinery from scratch. Those are adopted, and the effort goes into the golden
set, the thresholds, and looking at the data.

**The one-line thesis.** Most harnesses fail not because they compute the wrong metric, but
because they compare numbers produced by two differently-calibrated instruments and call the
difference a regression.

---

## 2. The five decisions that drive everything

These come from the evidence in the report and should not be reversed without new evidence.

1. **Retrieval and generation are instrumented separately.** A single end-to-end score cannot
   tell you whether to fix chunking or the prompt, and those are opposite fixes. Tracer work
   attributed 28.4% (medical QA) and 42.3% (HotpotQA) of hallucinations to the model ignoring
   correct context, which is a generation failure, not a retrieval one.
2. **The judge is treated as a measurement instrument, not an oracle.** It carries a version,
   a prompt hash, and a decoding config, and it is validated against human labels before
   anything gates on it.
3. **Evaluation must be reproducible enough to compare across days.** Temperature 0 is not
   reproducible (1,000 greedy decodes gave 80 distinct outputs) and an index update churned
   about 20% of a top-50 result set with no model change. So runs are seeded, snapshotted,
   repeated, and reported with variance.
4. **Gates fail on per-case verdict flips, not on average scores.** Averages let one severe
   regression hide behind mild improvements.
5. **We never tune on the set we measure on.** Dev/test split exists from day one.

---

## 3. Phases

### Phase 0 - Fix the measurement contract (half a day)

Deliverable: `evals/CONTRACT.md`, answering in writing:

- What exactly is the system under test: the retriever, the generator, or the pipeline?
- What counts as a failure, in one sentence a reviewer would accept?
- Which `k` is retrieved, and which `k` is actually **fed to the generator**? (These differ,
  and only the second one explains behaviour. "Lost in the middle" means a recall@10 of 0.9
  can be meaningless.)
- What is the correct behaviour for a question the corpus cannot answer? (Refusal, by
  default.)
- What is today's baseline, measured or admitted as unknown?

Check: five written answers, no blanks.

### Phase 1 - Golden set v1 (1 to 2 days)

Deliverable:

```
evals/golden/v1.jsonl      one case per line
evals/golden/manifest.json seed, case counts, source hash, date, review status
```

Case shape:

```json
{"id": "q001", "question": "...", "reference": "...",
 "expected_sources": ["doc#chunk"], "must_refuse": false, "tags": ["core"]}
```

Rules:

- 20 to 50 cases, every one hand-reviewed. Start with 20 real queries mined from logs or from
  the people who use the thing, not with imagined questions.
- Composition: roughly 40 to 60% core, 15 to 25% edge and unanswerable, tagged per category so
  per-category regressions are visible later.
- Store the retrieved contexts per case, so retrieval and generation can be scored apart.
- Version it. New cases arrive as `v2`, never as an edit to `v1`.

Check: a human has read every case; at least five refusal cases exist; `manifest.json` is
committed; the set loads and every case validates against the schema.

### Phase 2 - The free metrics first (1 day)

Deliverable: `evals/retrieval.py` computing recall@k, hit@k, MRR, and nDCG@k where graded
labels exist.

- Report both the retrieved `k` and the fed `k`.
- No model calls at all in this phase. This is the point of it.

Check: run it against a deliberately broken retriever and watch the numbers drop (a test that
cannot fail proves nothing); run it with a call counter and assert zero LLM calls.

### Phase 3 - Judge metrics, calibrated (2 to 3 days)

Deliverable: `evals/judges.py` implementing, in this order: faithfulness, answer relevance,
context recall, factual F1.

Judge protocol, all of it non-optional:

- A different model family from the generator (self-preference tracks a model's ability to
  recognise its own text).
- Pinned snapshot ID, never a moving alias. Temperature 0, one rubric dimension per call,
  prompt hashed into every record.
- Pairwise comparison with positions swapped and averaged, for anything comparative.
- Reference-guided grading where a reference exists (it cut a measured math-grading failure
  rate from 14 of 20 to 3 of 20).

Calibration: hand-label 30 traces first, then 100 to 300. Compute Cohen's kappa (or
Krippendorff's alpha). Do not gate on the judge below kappa 0.6.

Check: kappa is reported in the run artifact; every eval record carries judge id, temperature,
prompt hash, embedding version and index version; a scripted check confirms that changing the
judge snapshot changes the recorded identity.

### Phase 4 - Runner, caching, cost (1 to 2 days)

Deliverable: `evals/run.py`.

- Bounded concurrency as a parameter, not an accident.
- Retries only on transient errors; error responses are never cached.
- Checkpoint and resume, so a killed run does not discard completed work. In published harness
  work this was reported as essential, not a nicety.
- A keyed on-disk response cache, committed for CI. Cache only temperature-0 calls: caching one
  sample of a temperature-1 distribution misrepresents it.
- Cost, p95 latency and cache-hit recorded on the same artifact as the scores. Report a Pareto
  or scenario-weighted view, not a single number. Raising retriever `k` in one study moved cost
  from about $0.008 to $0.020 per run without improving a cost-weighted readiness score.

Check: kill the process mid-run and resume with no repeated calls; re-run unchanged and assert
zero provider calls; the artifact contains cost, p95 and cache-hit.

### Phase 5 - Gates (1 day)

Deliverable: `evals/gates.py` plus CI wiring.

- Non-negotiable cases (safety, worst-known incidents) must pass at 100%.
- Everything else fails on per-case verdict flips against a committed baseline, with a
  tolerance band of `baseline - 0.05`. Update the baseline only with the change that earned it.
- Fast representative subset on pull requests, full set nightly. A slow gate gets bypassed, and
  a bypassed gate is worse than no gate.
- Flaky cases are quarantined, tagged and still reported, never deleted. Emit JUnit XML so
  existing flaky-test tooling applies.

Check: inject a real regression (a degraded prompt) and watch the gate fail. Run the suite three
times on unchanged code and confirm no false failure. Both halves matter; a gate that never
fails is not a gate.

### Phase 6 - Ablations, one lever at a time (ongoing)

Order, cheapest first: `k` and reranking, then chunk size and overlap, then embedding model.
Re-run each against the committed baseline and keep a symptom to fix table (recall@k low ->
chunking; recall fine but context recall low -> boundaries and overlap; faithfulness low ->
grounding prompt).

Keep every cell's cost and p95 next to its quality. Published ablations found a reranker cut
top-20 retrieval failures 67% (5.7% to 1.9%), and that chunk-size optima are corpus-dependent
with peaks anywhere from 200 tokens to page-level while 128 underperformed.

Check: each ablation is one row with quality, cost and latency; nothing is adopted on quality
alone.

### Phase 7 - Production feedback loop (ongoing)

- Sample around 50 real queries a week and score them with the same scorers used offline.
- Track metrics per category, not just overall. In one production account whole query categories
  were failing about 40% of the time while the dashboard looked healthy, because users had
  stopped asking rather than complaining.
- Promote confirmed production failures into a new golden-set version.
- Make contamination a CI gate: provenance hashes plus embedding-similarity overlap between the
  eval set and any training or fine-tuning data must fail the build. Plant canary strings and
  keep the eval set behind a real access boundary, not just in a folder.

Check: the golden set has grown from production failures within a month; the contamination gate
fails when a deliberately leaked case is added.

---

## 4. Tooling recommendation

| Need | Choice | Why |
|---|---|---|
| Metric library | RAGAS or DeepEval | Apache-2.0, broadest metric coverage. DeepEval if pytest-native ergonomics matter. |
| CI gate with exit codes | promptfoo, or DeepEval's runner | promptfoo has the strongest purpose-built CI story (JUnit XML, fail-on-error) and stays MIT under OpenAI ownership. |
| Tracing and debugging | Langfuse (MIT core) or Comet Opik (Apache-2.0 for the whole platform) | Prefer them over Arize Phoenix, which is ELv2: source-available but not OSI-approved, a policy blocker in some organisations. |
| Choosing embeddings or a reranker | MTEB v2 / MMTEB, plus BRIGHT for reasoning-heavy retrieval | These benchmark the model, not the application. BEIR is now effectively a legacy subset. |

Do not standardise on OpenAI's hosted Evals: it shuts down 30 November 2026 and the open-source
repo is maintenance-only.

All thresholds are calibrated locally. Do not port a threshold from another tool or another
team; "context precision" and "context relevance" name the same idea with different formulas
and scales.

---

## 5. Risks, with the mitigation we are committing to

| Risk | What it looks like | Mitigation |
|---|---|---|
| Judge drift | A green suite flips with no commit | Pin judge snapshots, version the judge, re-baseline on upgrade, block trend lines across a judge change |
| Position and verbosity bias | Longer or first-shown answers win | Swap and average; length-control the rubric |
| Wrong blame | Tuning retrieval when the generator ignored good context | Separate retrieval and generation metrics; claim-level faithfulness |
| Silent non-comparability | January's 4.2 against March's 4.4 | Record judge, embedding and index versions in every run |
| Index churn | Rankings move with no model change | Freeze the index for benchmark runs |
| Tuning on the test set | 100% that generalises to nothing | Held-out test set, dev-to-test gap reported, fresh cases rotated in |
| Contamination | A leaked benchmark looks like a good one | Overlap gate in CI, canaries, access boundary |
| Silently stale metrics | The knowledge base moves, the scores do not | Re-curate on index change; alert on per-category regression |

---

## 6. Open questions, to be settled by experiment rather than by reading

1. **Chunk size and overlap for our corpus.** The sources directly contradict each other
   (about 200 tokens won one systematic sweep; another found page-level best with 512 and 1,024
   peaks and 128 worst). Sweep it ourselves, scoring retrieval and latency together.
2. **Is temperature 0 reproducible against our providers?** Run the same prompt N times, count
   distinct outputs, and store the result. Useful either way.
3. **Which gate is less noisy for us: per-case flips or pass-rate with tolerance?** Measure our
   own false-block rate over a month.
4. **What is the judge's agreement ceiling on our task?** kappa on 100 human labels. If it is
   below 0.6, the task needs a verifier or a human in the loop, not a better prompt.
5. **Does RAGAS `answer_relevance` return empty here?** One source reports it returning empty
   across all runs in a published harness. One scripted check settles it, and it decides whether
   RAGAS is our default.
6. **Dataset size needed for our effect size.** Not a universal number: run a power analysis on
   our own pilot data once Phase 3 is done.

---

## 7. Definition of done

- A change to the system produces a per-metric, per-case diff against a committed baseline, with
  cost and p95 latency on the same artifact.
- The gate fails on a real regression and does not fail on unchanged code.
- Every number is traceable to a judge version, an embedding version and an index version.
- The golden set is versioned, human-reviewed, and growing from production failures.
- A newcomer can read `evals/CONTRACT.md` and know what "better" means here.

---

## 8. Sources

Primary evidence behind the claims above, in rough order of weight:

RAGAS metric docs - Anthropic, Contextual Retrieval - Chroma, Evaluating Chunking - NVIDIA, Best
Chunking Strategy - RAGChecker (NeurIPS 2024) - ARES (NAACL 2024) - MT-Bench / Chatbot Arena
(2023) - JudgeBench (2025) - Thinking Machines, Defeating Nondeterminism - the LLM-judge bias
literature (position, verbosity, self-preference) - promptfoo RAG and caching guides - Lost in
the Middle (TACL 2023) - Confident AI, LLM regression testing - the readiness-harness
reproducibility repo.

Unverified and worth re-checking before standardising: RAGChecker's and Tonic Validate's exact
licences; whether RAGAS's maintenance has genuinely slowed; LangSmith's included trace volumes;
the widely repeated "70% of RAG systems fail in production", which none of the four passes could
trace to a primary source.

Method note: produced from four subagent research passes dispatched in parallel in one session
(tooling landscape, metrics and methodology, harness engineering, failure modes), merged with
contradictions preserved rather than smoothed.
