# Hero artwork

Generated with the built-in image generation tool on 2026-10-25. The original
references are preserved as `/Nova-Hero.png` and `/Nova-Hero-light.png`.

- `dark.png`: generated night edition, based on both original references.
- `light.png`: daylight edit of the generated night edition, using the supplied
  light original as its lighting reference.

The current website uses four separately generated planes per theme in
[`layers/`](layers/README.md), extracted from these original matched scenes.
Run `python tools/make_hero_assets.py` to encode the planes and their neutral
composite. The browser translates image planes without warping their pixels.
Text and controls belong to the HTML.

## Dark prompt

Create a production hero illustration, DARK NIGHT EDITION, of the supplied Nova coding-workspace artwork. Image 1 is the exact composition and character reference; image 2 is a supporting reference for the matching day edition to be made next. Preserve the identity, face, hairstyle, black hoodie, relaxed pose with chin resting on hand, crescent-moon laptop, smiling glowing cat, books, plants, windows, city and floating code panels. Preserve the original composition and approximate image coordinates: coder at left center, head around x32% y34%, laptop lower center, cat bottom left; open sky and crescent moon in the right half. Wide 16:9 landscape, premium polished anime illustration, richly detailed but calm. Night palette: deep navy and violet sky, warm amber interior light, cyan and lavender code-panel glows, warm skin tones. Maintain natural clean silhouettes with no smeared textures, no duplicate outlines. The large right-hand sky is airy and quiet; no large lettering or logos baked into the image, no NOVA.CODE wordmark, no tagline, no watermark. This is the actual web hero artwork for subtle depth parallax, not a website screenshot, not a design board: no UI, buttons, nav or frames. The next light edition must be the same scene in daylight, so keep a stable, physically plausible composition.

## Light prompt

Create the LIGHT DAYTIME EDITION of image 1 for a matched website hero pair. Use case: lighting-weather edit. Image 1 is the edit target: preserve its exact composition, camera, framing, dimensions/aspect ratio, every object position and contour, character identity, face, expression, hairstyle and hair silhouette, black hoodie, hand resting against chin, laptop and its crescent emblem, smiling glowing cat, books, code panes, plants, shelves, window mullions, city towers, crescent moon, orbital lines and planets. Image 2 is ONLY the lighting and palette reference, not the layout or lettering reference. Change illumination and atmosphere only: bright warm natural daylight fills the room, soft cream and sage interior, clear powder-blue sky with delicate white clouds over a softly sunlit city, gentle lavender/cyan code panes, luminous pearl-white crescent in daytime sky. Preserve dark readable character features and hoodie. Premium polished anime illustration. Keep right-hand sky spacious. Do not add any text, headline, NOVA.CODE wordmark, tagline, watermark, website UI, buttons, navigation, borders or frames. This must register with the night image during a crossfade: do not move the character, laptop, cat, moon or architectural objects; do not redesign the pose or scene.
