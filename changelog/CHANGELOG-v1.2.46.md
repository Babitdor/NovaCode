# NovaCode 1.2.46

## Conversation history

- Use Page Up, scroll up at the top, or choose **Load older messages** to
  browse earlier conversation history in live and resumed terminal tabs.
- Nova keeps a small window of older messages on screen and preserves your
  scroll position as you load a page. Historical tool output appears as
  read-only text.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.46
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.46"
```
