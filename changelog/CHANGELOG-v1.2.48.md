# NovaCode 1.2.48

## Conversation layout

- Restored shell calls start collapsed in Normal and Code Mode, with compact
  spacing between cards. Expanded shell output fits its content and scrolls
  when it reaches the height limit.
- **Jump to latest** is centered above the composer and resizes as the line
  count changes.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.48
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.48"
```
