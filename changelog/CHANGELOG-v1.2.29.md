# NovaCode 1.2.29

## Keep the Nova avatar visible over chat

- Fix the inline Sixel avatar disappearing when a populated transcript splits
  its drawing rows. Preserve image data through Textual strip division and cropping.
- Draw cached image bands per terminal row so partial chat redraws restore the
  affected avatar pixels instead of leaving blank areas.
- Include screen position in the rendering cache so resizing and a growing
  composer keep the image at its bottom-right anchor.
- Preserve the 16-column by 8-row size, high-resolution artwork, simple animations
  and hiding behind dialogs. No new dependency is required.

## Validation

Regressions reproduce the missing image with a populated transcript, check
single-cell partial redraws across every avatar row, and verify cursor placement
through strip division/cropping, resizing and dialog visibility. The actual Sixel
output was inspected in a browser terminal emulator with chat messages present.
Live Windows Terminal rendering has not been visually verified.

See [avatar sources and rebuilding](../assets/nova/README.md) for details.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.29
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Preserve any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.29"
```
