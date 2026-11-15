# Nova avatar source

`NovaGirl-simple-loops.png` was generated with the built-in image generation tool,
using `NovaGirl-HD.png` as the identity and style reference. It keeps Nova's
illustrated face, brown bob, gold earrings and black outfit. The prompt is in
`simple-loops-prompt.txt`. The transparent source has four columns and six rows;
each adjacent pair is a two-frame animation.

Rebuild the embedded terminal animation with:

```powershell
.venv/Scripts/python.exe scripts/build_avatar.py assets/nova/NovaGirl-simple-loops.png --simple-grid --height 32
```

Both renderers occupy 16 columns by 8 rows, anchored at the bottom right just
above the composer, down from 32 × 16 cells. Terminals
reporting Sixel or supported Kitty graphics receive the original 256 × 256
frames from the packaged `novacode_cli/assets/nova-avatar-hq.png`. This displays
real image pixels rather than enlarging text glyphs. Terminal capabilities and
cell pixel size are queried before Textual starts; redirected/headless input
uses the character renderer. Inline rendering uses `textual-image==0.12.0`, the
Python 3.11-compatible release. `avatar_images.py` corrects that release's Sixel
frame clearing and cursor restoration. Its compatibility tests must pass when
changing the dependency pin. Sixel occupies a small rectangular region over the
conversation; hiding or moving the avatar redraws that region.

Sixel rows carry their graphics through Textual's strip splitting and cropping.
Each row draws a cached image band, so chat layout boundaries cannot discard the
image and partial chat repaints restore the affected pixels. The cache includes
screen position so resizing and a growing composer keep the image anchored.

The character fallback's 24 frames use one shared 32 × 32 canvas. It draws octants where
the terminal supports them and standard quadrant glyphs elsewhere. Quadrants
retain both horizontal samples in each cell, keeping more facial and outline
detail than the previous half-block fallback. Lanczos downsampling reduces
jagged edges without increasing the avatar's terminal size. The build writes
`novacode_cli/assets/nova-avatar.png` (one row per animation, 256 colours with
alpha), the high-resolution sheet, and `novacode_cli/tui/avatar_frames.py`. Idle blinking, walking, running,
sitting, sleeping, cursor reactions, thinking, talking, happy, confused, window
and playful states remain connected to the existing activity and interaction
logic. Reactions use slower frame rates suited to the short loops.

The original detailed source and its `generation-prompt.txt` are retained. The
builder still supports that source's reference layout without `--simple-grid`.

A native Windows overlay that drew 128-pixel RGBA frames in a separate window was removed: it crashed Windows Terminal.
