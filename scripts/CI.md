# CI and releases

CI runs boundary checks, four regression shards per operating system, and the
Windows wheel installation/reinstallation smoke test independently. `CI result`
is the aggregate check to require in branch protection; it fails when any required
job fails, is cancelled, or is skipped. Existing branch-protection rules referring
to the old regression job names need to be updated in repository settings.

`ci-regression-tests.json` preserves the regression suite's selectors, including
directory and individual-test selections. Values are estimated seconds, initially
the larger Linux/Windows per-selector duration from successful run 43401907990.
Add new offline regression selectors here with a positive estimated duration.
The runner greedily balances the longest selectors first, then preserves manifest
order within each shard. Shards run in separate jobs, avoiding shared configuration
and terminal state races. The per-test timeout remains unchanged.

Run a shard locally with:

```sh
uv run --no-sync python scripts/ci_regression.py --shard 1 --shards 4
```

Use `--list` to inspect its selection. Every shard uploads JUnit results and a
flushed test log, including on failure. Use those reports to revise estimates
when the suite changes. Four shards reduce test execution's critical path but
repeat dependency setup and can use more runner minutes; actual speed depends on
available GitHub runner concurrency.

Before tagging a release, wait for CI on the exact main-branch commit to succeed.
The publication workflow checks that commit's latest push CI run before allowing
the job with PyPI publishing credentials to start. A failed, missing, or unfinished
CI run blocks publication. If tagged too early, rerun the publication workflow
after CI passes. Metadata validation, fresh builds, exact-version artifact checks,
and PyPI trusted publishing remain in place.
