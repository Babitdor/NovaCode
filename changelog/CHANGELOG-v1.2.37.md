# NovaCode 1.2.37

## Create reusable agents with /proactive

- Create named agents with standing instructions, a workspace, and optional
  read access to committed Git files, GitHub Actions, and PyPI.
- Run an agent on demand with context such as a release or commit. Run reports
  are saved in the panel and NovaCode notifies you when a run finishes.
- Enable or pause cron and GitHub tag triggers independently of manual runs.
  Agents and reports persist across restarts. Triggers run while NovaCode is open.

See the [Proactive agent guide](https://github.com/Babitdor/NovaCode/blob/v1.2.37/docs/proactive.md)
for setup, supported tools, and trigger behavior.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.37
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.37"
```
