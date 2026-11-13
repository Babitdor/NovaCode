# Response modes: Caveman and Ponytail


The composer selector cycles **Off → Caveman → Ponytail → Off**. Options saves
mode, independent intensities and Caveman feature switches together; Escape discards
edits. Preferences persist across restarts. Existing Caveman settings migrate automatically.
Mode changes use temporary notifications and never submit your draft.

- `/caveman on|off|lite|full|ultra|wenyan-lite|wenyan-full|wenyan-ultra`
- `/ponytail` activates Ponytail without resetting its selected intensity.
- `/ponytail lite|full|ultra|off|settings|status`
- `/ponytail-review [scope]`, `/ponytail-audit [scope]`, `/ponytail-debt [scope]`,
  `/ponytail-gain`, `/ponytail-help` run the corresponding one-shot helper without
  changing the selected mode. Review and audit request reports, not edits.
- A standalone `stop ponytail` or `normal mode` disables Ponytail. Those phrases
  inside an ordinary task do not change preferences.

Caveman compresses prose and optionally tool views. Ponytail applies the upstream
coding rules: solve the complete problem with the least new code, preserve necessary
checks, and name skipped work and risks. Ponytail leaves tool results intact and
also applies to delegated model requests. Neither mode changes tool permissions.
Savings depend on the task and model; shorter output is not guaranteed.

Ponytail's MIT-licensed skill bodies and JavaScript instruction builder are vendored
from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) at
`09c434ab73ca43761c5d8b0c5d341926c2fba710`. Nova's Python builder is checked
against golden hashes from that JavaScript builder. Nova owns the persistent selector;
upstream Claude-specific band, board, install hooks and default-mode files are not used.
There is no Node runtime requirement.

To validate every compression intensity against your configured LLM (20 paid/provider
calls, concurrency two, 60-second timeout each), run:

```sh
uv run python -m scripts.verify_compression_live
```

This opt-in check preserves exact failure facts/commands and validates generated
Python on boundary and invalid-bound cases. It writes synthetic outputs and reported
usage (or `unknown`) to `test-results/compression-live.json`, without changing your
preferences. Offline tests never call an LLM. Golden parity proves prompt equivalence;
it cannot guarantee identical stochastic model responses.
