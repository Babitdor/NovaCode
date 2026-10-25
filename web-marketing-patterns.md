# Landing Page Feature Integration Pattern

## Section template
When integrating new features from FEATURES.md into index.html, create a showcase section matching the established structure:
- `<section id="slug" data-chapter="~/...">` with `.wrap`, `.head rv`, `cmdline`, `h2`, `body`
- UI mockup uses inline styles + `.window`/`.win` classes
- Footer note: `<p class="note">` for supplementary detail
- Update sidebar TOC (`<dt>/<dd>` pairs) and FAQ (new `<details>`)

## Features added 2026-01-xx
Web Council (debate cards), Cowork (split-window workspace mockup), Remote Sessions (service comparison table).

## Lesson
Always edit top-to-bottom to avoid offset drift on large single-file sites. Inline styles in HTML templates are normal here (zero-build static site). Grid layouts use `repeat(auto-fit, minmax(200px, 1fr))`.
