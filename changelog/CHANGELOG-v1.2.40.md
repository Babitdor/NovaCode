# NovaCode 1.2.40

## Oracle reviews

- Reviewer threads show scores, summaries, and findings as Markdown reports.
  The original evidence remains visible as plain text.
- Reviewer model calls can run for up to four minutes. Oracle logs the round,
  reviewer, elapsed time, and evidence pages read when a call times out.
  The ten-minute limit for a paged review still applies.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.40
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.40"
```
