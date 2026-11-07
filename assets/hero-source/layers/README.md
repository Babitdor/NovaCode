# Aligned hero planes

Generated with the built-in image generation tool from the parent `dark.png`
and `light.png` illustrations. The original full scenes remain untouched.
Each dark layer was edited from the night scene; its light counterpart was
edited from that extracted layer, with the full day scene as lighting reference.

Every PNG keeps the original full 1672 × 941 composition. Two generated light
PNGs are 1671px wide: the build pads their far edge by one transparent pixel,
preserving registration without stretching any object.

The active `*-ink.png` planes redraw the sky, room, subject, and plants in the
coin's inked style while keeping the original day/night colors, pose, objects,
and placement. Some generated transparent planes are one or two pixels narrower;
the builder pads their far edge without stretching them. The original planes are
kept alongside the new set.

The active subject is now `subject-ink.png`, one regenerated master reused by
both themes and both static composites. It has balanced warm lighting; no
theme-specific face is generated or selected. The older light/dark subject
sources remain archived. Rebuild with `python tools/make_hero_assets.py --ink`.

Generated with the built-in image tool using the light subject as the composition
reference and `assets/coin.png` as the facial/style reference. Prompt: regenerate
one transparent 1672 x 941 subject plane in the coin's inked style, retaining the
warm brown hair, peach skin, navy hoodie, lavender laptop, wood desk, cat, books,
pen cup, exact placement and chin-on-hand pose; balanced warm neutral light;
no background, crop, recentering, text or watermark. A corrective edit removed
an extra arm, requiring exactly two arms/hands in the original pose while
preserving the regenerated face and all other objects.

| Plane | Contents | Transparency | Pointer depth | Scroll speed |
| --- | --- | --- | --- | --- |
| `sky` | Moon, planets, orbital lines, complete sky and city | Opaque | 0.15 | 0.26 |
| `room` | Walls, shelves, window frames, upper plants and code panes | True alpha through the windows | 0.5 | 0.18 |
| `subject` | Coder, hoodie, laptop, cat, books, pen cup and tabletop | True alpha around the group | 1.0 | 0.10 |
| `plants` | Soft near foliage at the bottom corners | True soft alpha | 1.8 | 0.02 |

Run `python tools/make_hero_assets.py --ink` to export the active planes and
matching full-size and 960px static composites. Run without `--ink` to rebuild
the original set. Loading failures and reduced motion retain the matching complete
image, and the original artwork is not overwritten by the ink build.

## Generation prompt set

All eight prompts required the original full canvas, exact object coordinates
and scale, no cropping/recentering, the same anime style and character identity,
no new objects, no watermark, no embedded large lettering and no website UI.
Opaque output was used for sky; genuine transparent RGBA output for the rest.

1. **Dark sky:** Remove all interior room, window frames, plants, code panes,
   woman and desk objects. Preserve the original moon, planets, orbital lines,
   cloud bands and visible city coordinates. Reconstruct continuous complete
   night sky and city behind every removed object across the entire canvas,
   with no silhouette, hole, smeared fill or empty border.
2. **Light sky:** Change only the dark sky plate's lighting into bright daytime.
   Preserve its moon, orbital curves, planets, buildings, horizon and framing.
   Use the original light scene's blue sky, delicate clouds and sunlit city.
3. **Dark room:** Retain walls, shelves, upper hanging plants, window mullions
   and floating code panes at their original coordinates. Remove the woman,
   desk, cat, laptop, books and near corner foliage. Make the window's sky/city
   genuinely transparent. Reconstruct room surfaces and complete code panels
   hidden behind the woman without a person-shaped fill.
4. **Light room:** Change only the extracted room's illumination to soft daylight,
   preserving full canvas, silhouettes, alpha, window frames and code-pane positions.
   Cream walls, natural wood, sage plants and subtle lavender/cyan panes;
   all window sky/city remains transparent.
5. **Dark subject:** Extract the exact woman's face, hair, chin-on-hand pose,
   hoodie and hands, together with her laptop, keyboard, smiling cat, books,
   pen cup and continuous tabletop. Remove all room, code panes, sky and
   near plants. Preserve fine hair and glow edges without a backing rectangle,
   fringe, duplicate outline or smear. Keep the original full-frame registration.
6. **Light subject:** Change only the extracted group's illumination to daylight,
   preserving identity, pose, alpha silhouette, coordinates and scale. Naturally
   lit hoodie and skin, silver-violet laptop, warm wood and gentle cyan glow.
7. **Dark plants:** Extract only the original near blurred foliage at the lower
   corners, preserving position, scale and soft alpha. Leave the entire center
   and top transparent; no connecting strip, tabletop or additional objects.
8. **Light plants:** Change only the extracted corner plants' lighting to sunlit
   sage/olive green with warm highlights, preserving their exact soft silhouette,
   transparent center and full-frame registration.

The runtime applies only fixed uniform scale plus translation to each plane.
It has no depth shader, pixel displacement, mesh or dynamically stretched shapes.
