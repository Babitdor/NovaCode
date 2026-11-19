# NovaCode 1.2.47

## Conversation history

- Older messages load as you scroll up. Scroll back down to continue toward
  the latest messages. Nova keeps your place as it loads each page.
- Past calls in Normal and Code Mode use the same tool cards as live calls.
  Expand a card to inspect its arguments or JavaScript and recorded output.
  File edit cards retain their colored diff when the preview was captured.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.47
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.47"
```
