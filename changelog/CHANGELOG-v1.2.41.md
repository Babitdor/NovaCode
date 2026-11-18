# NovaCode 1.2.41

## Code Mode cancellation

- Stopping a Code Mode turn cancels its nested tools and gives them a short
  cleanup window. Slow tool cleanup can continue in the background while Nova
  releases the turn. External operations already underway may still finish.
- MCP tool adapters propagate cancellation back to Nova.

## Oracle reviews

- Reviewer model calls can run for up to ten minutes. Paged reviews get a total
  budget of ten minutes plus one minute per evidence page. The Oracle panel
  displays the applicable deadline.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.41
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.41"
```
