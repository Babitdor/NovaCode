# Nova avatar source

`NovaGirl-HD.png` is the detailed remaster generated with the built-in image generation tool from the user's original `NovaGirl.png`. It retains the original 1536 × 1024 layout and alpha transparency; this is newly rendered artwork, not a larger pixel canvas. The generation prompt is in `generation-prompt.txt`.

Rebuild the embedded terminal animation with:

```powershell
.venv/Scripts/python.exe scripts/build_avatar.py assets/nova/NovaGirl-HD.png
```

All 91 frames across 12 animation groups use one shared 64 × 64 canvas: 32 columns by 16 rows, drawn with character cells (octants where the terminal has them, half-blocks elsewhere) on every platform. The build writes the frames to `novacode_cli/assets/nova-avatar.png` (one row per animation, 256 colours with alpha) and their layout to `novacode_cli/tui/avatar_frames.py`. Pass `--height 48` for a smaller avatar (24 × 12 cells); below that her face is no longer readable.

A native Windows overlay that drew 128-pixel RGBA frames in a separate window was removed: it crashed Windows Terminal.
