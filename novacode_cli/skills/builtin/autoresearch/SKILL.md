---
name: autoresearch
description: Run bounded experiments to improve a measurable result and preserve reproducible evidence.
---

# Autoresearch

1. Define the objective, metric direction, measurement command, files in scope, and stopping condition. Use the supplied brief and clarify material gaps before making changes. Record any constraints such as a runtime limit or a correctness check.
2. Run the measurement command on the starting state. Repeat noisy measurements under the same conditions and record the baseline and its variability.
3. Pick one hypothesis and make one reversible change. Use an isolated checkout when needed to protect concurrent work. Revert only changes introduced by the experiment. Preserve the user's work and obtain authorization for commits or environment changes when required.
4. Use the same measurement command and settings for each candidate. Run the correctness checks as well as the primary metric. Record the hypothesis, changed files, command, measurement, check result, and whether the candidate was kept. Retain candidates that meet the acceptance criteria and improve the metric beyond measurement noise.
5. Stop at the agreed target, iteration or time limit, or when the next experiment requires input or authorization. Repeat the final checks on the best candidate. Report baseline versus final measurements, the changes retained, and how to reproduce the result.
