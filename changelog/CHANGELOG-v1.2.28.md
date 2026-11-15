# NovaCode 1.2.28

## Smaller, sharper Nova avatar

- Keep the original illustrated Nova character, with simpler two-frame loops
  for all 12 activity and reaction states.
- Shrink `/nova` to 16 columns by 8 rows and keep it anchored at the bottom right,
  just above the composer, including after terminal resizing.
- Display source-resolution artwork inline on terminals reporting Sixel or
  supported Kitty graphics, preserving much more detail than character cells.
  Keep the existing character renderer for other terminals and redirected input.
- Probe graphics support before Textual takes ownership of keyboard input.
  Preserve hiding behind menus/dialogs, the avatar toggle and activity routing.
- Correct Sixel animation repainting and cursor restoration for the pinned
  image library. Fall back to character rendering if the image cannot be loaded.
- Improve the fallback with smoother asset downsampling and standard quadrant
  glyphs that retain left/right detail when octants are unavailable.
- Package both the compact atlas and high-resolution artwork. Retain the source
  sheet, generation prompt and reproducible build instructions.

The new runtime dependency is `textual-image==0.12.0`, pinned for Python 3.11
compatibility and the tested Sixel adapter. Inline image support depends on the
terminal's capability response. Unsupported terminals remain visibly pixelated.

## Rooms refresh synchronization

- Make explicit conversation refreshes wait behind an in-progress refresh instead
  of returning early with stale messages, channel names or task controls.
- Stabilize settings interactions by scrolling actions into view before simulated
  clicks. Add a regression for clearing messages during an in-progress refresh.

## Validation

Avatar regressions cover both character rendering paths, native Sixel output,
frame clearing, cursor placement, animation routing, bottom-right positioning,
resizing, dialog visibility and image-load fallback. Package checks verify the
high-resolution artwork and widget are included. The preview used actual Sixel
output in a browser terminal emulator; live Windows Terminal rendering has not
been visually verified.

See [avatar sources and rebuilding](../assets/nova/README.md) for details.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.28
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading and use `/nova` to toggle the avatar. Preserve
any optional extras you use:

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.28"
```
