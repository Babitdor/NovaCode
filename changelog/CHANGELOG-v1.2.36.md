# NovaCode 1.2.36

## Oracle reviewer agents for larger changes

- Oracle automatically switches to read-only reviewer agents for evidence above
  64,000 characters. Each agent requests pages of the captured diffs, changed
  text files, and test output. The panel shows page coverage as each reviewer
  works.
- A reviewer must receive every page before Oracle accepts its score. Edits
  during review invalidate the scores. Paged reviews require a model with tool
  calling and support up to 512,000 characters of evidence.
- Reviewers score distinct aspects in both review paths. The new default lineup
  covers Correctness, Regressions, and Security. A fourth reviewer adds
  Verification, and a fifth adds Maintainability. Saved reviewer counts still
  apply.

See [Oracle mode](https://github.com/Babitdor/NovaCode/blob/v1.2.36/docs/oracle-mode.md)
for review controls and limits.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.36
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.36"
```
