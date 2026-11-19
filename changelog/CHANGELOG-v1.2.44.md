# NovaCode 1.2.44

## Image generation

- Ask Nova to make an image and optionally specify a new project-relative
  `.png` path. Generated files default to `generated-images/`.
- The local `image_gen` tool invokes `codex exec` with the built-in
  `$imagegen` skill. Nova verifies the PNG and preserves existing files.
- Install or update Codex with `npm install -g @openai/codex`, then run
  `codex login` with an active ChatGPT subscription that includes image
  generation. Nova provides these steps when the CLI or sign-in is missing.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.44
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.44"
```
