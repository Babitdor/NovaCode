# Task: Performance Audit & Optimization of Python Coding Agent CLI

## Objective

Audit and optimize this Python Coding Agent CLI for speed, resource efficiency, reliability, and responsiveness.

Prioritize measurable improvements over premature optimization. Preserve existing functionality, agent behavior, tool interfaces, and compatibility.

**Do not rewrite the project in Rust. Optimize the Python implementation first.**

## Phase 1 — Performance Profiling

- [ ] Inspect the project architecture and identify performance-sensitive components.
- [ ] Measure cold and warm CLI startup times.
- [ ] Identify expensive imports and unnecessary initialization.
- [ ] Profile CPU-intensive operations using `cProfile` or equivalent tooling.
- [ ] Measure peak memory usage during representative tasks.
- [ ] Instrument LLM request latency, including time to first token and total response time.
- [ ] Measure token usage per request and per completed task.
- [ ] Measure file I/O, repository search, and subprocess execution times.
- [ ] Identify blocking operations in asynchronous code.
- [ ] Establish baseline benchmarks before making changes.

## Phase 2 — CLI Startup Optimization

- [ ] Identify and remove unnecessary startup work.
- [ ] Lazy-load heavy dependencies when appropriate.
- [ ] Avoid initializing LLM clients until required.
- [ ] Avoid scanning repositories during CLI startup unless essential.
- [ ] Optimize configuration and environment-variable loading.
- [ ] Ensure `--help` and `--version` execute without unnecessary initialization.
- [ ] Avoid repeated filesystem checks and redundant configuration parsing.
- [ ] Evaluate startup performance after modifications.

## Phase 3 — Repository Search and File I/O

- [ ] Identify inefficient recursive directory traversal.
- [ ] Respect `.gitignore` and exclude irrelevant directories.
- [ ] Avoid scanning `.git`, virtual environments, dependency folders, generated files, and binary files unless explicitly requested.
- [ ] Evaluate using `ripgrep` for fast repository-wide text searches.
- [ ] Search filenames and symbols before reading full file contents where practical.
- [ ] Implement bounded file reads where appropriate.
- [ ] Avoid repeatedly reading unchanged files.
- [ ] Avoid loading large files entirely into memory when streaming would suffice.
- [ ] Limit search-result sizes while preserving relevant information.
- [ ] Preserve support for Unicode, unusual filenames, and cross-platform paths.

## Phase 4 — Caching

- [ ] Identify expensive computations that produce reusable results.
- [ ] Implement caching for unchanged file metadata or parsed file content where beneficial.
- [ ] Consider caching repository structure and symbol indexes.
- [ ] Use reliable invalidation when files are added, modified, renamed, or deleted.
- [ ] Prevent stale context from being sent to the LLM.
- [ ] Avoid caching non-deterministic shell command results.
- [ ] Avoid caching sensitive information unnecessarily.
- [ ] Introduce bounded cache sizes and appropriate eviction policies.
- [ ] Measure cache hit rates and performance gains.

## Phase 5 — LLM Request Optimization

- [ ] Identify unnecessary or duplicate LLM requests.
- [ ] Analyze repeated instructions and redundant conversation history.
- [ ] Reduce unnecessary context without harming coding accuracy.
- [ ] Avoid sending complete files when relevant sections are sufficient.
- [ ] Implement or improve context-window management.
- [ ] Evaluate provider-supported prompt caching.
- [ ] Avoid repeated token counting or serialization when inputs are unchanged.
- [ ] Preserve correct tool-call and conversation history ordering.
- [ ] Ensure caching never substitutes stale or incorrect tool results.
- [ ] Track tokens, latency, and costs per agent task.

**Important:** Do not optimize token usage at the expense of agent correctness or task completion quality.

## Phase 6 — Tool Execution

- [ ] Audit the shell execution implementation.
- [ ] Identify unnecessary subprocess creation.
- [ ] Eliminate duplicate tool executions where safe.
- [ ] Use asynchronous subprocess execution where beneficial.
- [ ] Execute independent read-only tools concurrently when safe.
- [ ] Keep dependent operations and conflicting file modifications sequential.
- [ ] Avoid deadlocks caused by unconsumed stdout or stderr.
- [ ] Handle subprocess timeouts, cancellation, and cleanup correctly.
- [ ] Bound tool-output size without discarding critical diagnostics.
- [ ] Preserve command exit codes and error messages.
- [ ] Avoid introducing shell injection vulnerabilities.

## Phase 7 — Async I/O and Concurrency

- [ ] Audit network calls and other I/O for unnecessary blocking.
- [ ] Reuse HTTP clients and connections where appropriate.
- [ ] Avoid blocking the event loop with synchronous operations.
- [ ] Introduce controlled concurrency for independent operations.
- [ ] Avoid unbounded task creation.
- [ ] Implement appropriate request timeouts.
- [ ] Use retries with exponential backoff and jitter for transient failures.
- [ ] Respect provider rate limits.
- [ ] Ensure cancellation propagates correctly.
- [ ] Prevent race conditions involving shared agent state, conversation history, and file modifications.

## Phase 8 — Terminal UX and Responsiveness

- [ ] Ensure the CLI displays feedback immediately after receiving a request.
- [ ] Stream LLM output when supported.
- [ ] Display tool-execution progress without excessive terminal updates.
- [ ] Avoid unnecessary terminal redraws.
- [ ] Support clean cancellation using Ctrl+C.
- [ ] Prevent terminal corruption following exceptions or interrupted tasks.
- [ ] Minimize output buffering where streaming is expected.
- [ ] Keep normal output concise while retaining a verbose/debug mode.

## Phase 9 — Memory and Data Structures

- [ ] Identify unnecessary copies of large strings, messages, and file contents.
- [ ] Avoid repeatedly serializing unchanged objects.
- [ ] Use generators or streaming where appropriate.
- [ ] Detect unbounded growth in conversation history and internal caches.
- [ ] Inspect data structures used for repository indexing and tool results.
- [ ] Ensure subprocess pipes, file handles, and network connections are released.
- [ ] Investigate memory leaks or long-lived object retention.
- [ ] Compare peak memory usage against baseline measurements.

## Phase 10 — Reliability and Regression Testing

- [ ] Add tests for modified performance-sensitive components.
- [ ] Verify agent tool-call correctness.
- [ ] Verify repository search accuracy.
- [ ] Verify file read/write behavior.
- [ ] Test caching invalidation after file changes.
- [ ] Test subprocess failures, timeouts, and cancellation.
- [ ] Test large repositories and large tool outputs.
- [ ] Test Unicode and platform-specific path behavior.
- [ ] Confirm no reduction in coding-agent task success.
- [ ] Run existing tests, linting, and type checks.

## Phase 11 — Benchmarking

Create or improve reproducible benchmarks for:

- [ ] CLI startup time.
- [ ] Small and large repository searches.
- [ ] File-reading operations.
- [ ] Tool execution overhead.
- [ ] End-to-end agent task latency.
- [ ] Time to first visible response.
- [ ] LLM request count and token consumption.
- [ ] Peak memory usage.
- [ ] Cache hit rate and invalidation overhead.

Compare before and after results using identical workloads wherever possible. Run repeated measurements to reduce noise and distinguish network/model variance from local application overhead.

## Phase 12 — Final Report

Provide a report containing:

- [ ] Performance bottlenecks identified.
- [ ] Optimizations implemented.
- [ ] Benchmark results before and after.
- [ ] Startup-time improvement.
- [ ] Memory-usage improvement.
- [ ] Tool-execution improvements.
- [ ] LLM token and request reductions.
- [ ] Tests executed and their results.
- [ ] Remaining limitations and recommended future improvements.

## Implementation Rules

1. **Inspect and benchmark before making significant changes.**
2. Prioritize high-impact bottlenecks instead of blindly applying every checklist item.
3. Implement optimizations incrementally and verify results.
4. Avoid unnecessary abstractions, dependencies, and architectural rewrites.
5. Preserve existing CLI commands, public interfaces, configurations, and agent behavior.
6. Do not sacrifice correctness, security, or maintainability for marginal performance gains.
7. Do not introduce concurrency where it creates nondeterministic agent behavior.
8. Do not introduce caching without a reliable invalidation strategy.
9. Avoid modifying unrelated code.
10. If an optimization is not worthwhile, document why and leave it unimplemented.

## Expected Deliverables

1. Performance baseline and bottleneck analysis.
2. Implemented optimizations with focused code changes.
3. Regression tests for affected components.
4. Reproducible performance benchmarks.
5. Before-and-after comparison.
6. A prioritized list of further optimization opportunities.

**Success criterion:** The CLI should become measurably faster, more responsive, and more resource-efficient while maintaining or improving coding-agent correctness and reliability.