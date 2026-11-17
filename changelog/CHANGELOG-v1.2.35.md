# NovaCode 1.2.35

## Oracle reliability on large changes

- Split long file lists across Git commands to prevent Windows error 206.
  Preserve literal filenames, including spaces, brackets and Unicode.
- Bound Git output and file-body collection against the Oracle evidence limit.
  Oversized evidence pauses review before reviewers receive a partial diff.
- Preserve the original binary classification in workspace snapshots. Deleting
  an untracked binary or replacing it with text now requires manual review.

## Validation

Windows tests cover command length, filename handling, process cleanup, evidence
limits and binary deletions. A real Git comparison checked the output for 400
changed files. The review evidence limit is 64,000 characters.

See [Oracle mode](https://github.com/Babitdor/NovaCode/blob/v1.2.35/docs/oracle-mode.md)
for review behavior and controls.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.35
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.35"
```
