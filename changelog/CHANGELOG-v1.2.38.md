# NovaCode 1.2.38

## Oracle reviews the current task by default

- **This task** compares the workspace at the start of the turn with the
  changes made during the turn. It includes edits to already-dirty files and
  changes committed during the task, even when a large unrelated diff is pending.
- Choose **Workspace changes** in Oracle Settings to include existing edits
  and nonignored untracked files. This scope still uses Oracle’s evidence budget.
- Both scopes work in local Git repositories before the first commit.

See the [Oracle mode guide](https://github.com/Babitdor/NovaCode/blob/v1.2.38/docs/oracle-mode.md)
for scope settings and review limits.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.38
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.38"
```
