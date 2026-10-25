# NOVA.md

**Marketing/documentation site for Nova (Babitdor/NovaCode), a terminal coding agent — static HTML/CSS/JS published on the repo's gh-pages branch at `https://babitdor.github.io/NovaCode/`.**

## Commands

| Task | Command |
|------|---------|
| Preview | `python -m http.server 4400` → http://localhost:4400/ |
| Site regression | `python tools/verify_site.py` (needs `pip install playwright` + Microsoft Edge; add `--live` to check real main branch) |
| Changelog unit tests | `python -m unittest tools.test_changelog_generator` |
| Regenerate changelog | `python tools/make_changelog_page.py --source-dir ../NovaCode/changelog` (omit `--source-dir` to fetch from GitHub API) |
| Optional a11y | `npm install --no-save --no-package-lock --ignore-scripts axe-core` (axe runs only if present) |

There is no build step and no framework. Python 3.11+ stdlib only for tools.

## Architecture

- **Two-branch contract:** site files live on **gh-pages** (this checkout); release Markdown sources (`CHANGELOG-v*.md`) live on **main**. This checkout contains only the *generated* `changelog/index.html`, not the sources.
- `changelog/index.html` embeds the full release archive between `<!-- generated:changelog-releases -->` markers. On page load, `assets/changelog.js` checks GitHub's main branch for new/edited releases (max 4 downloads) and falls back to the saved archive on API failure. Native `<details>` works without JS; `#v1-0-30` and `#v1.0.30` both resolve.
- `tools/verify_site.py` serves the repo itself under a `/NovaCode/` prefix (mimicking Pages) and checks links, deep links, mobile layout, reduced-motion, and no-JS rendering; screenshots go to `.shots/repair/` (gitignored).
- `.github/workflows/changelog.yml` regenerates the archive from main on push to gh-pages (when generator/workflow changes) or manual dispatch, committing to gh-pages.

## Code Conventions

- **All paths relative** — the site is served under `/NovaCode/`; absolute `/assets/...` links break. Never add a leading slash to `href`/`src`.
- Vanilla HTML/CSS/JS, no dependencies, no bundler. Landing page is a single 1800-line `index.html` with inline `<style>`/`<script>`; keep edits inline there.
- Design system (see DESIGN.md): Geist + Geist Mono self-hosted in `assets/fonts/`, one accent color, radius 999px on pressables / 14px on panels, theme follows system (no section-level inversion), motion is entrance/feedback only and disabled under `prefers-reduced-motion`.
- Python tools: `pathlib.Path` everywhere, `from __future__ import annotations`, stdlib only.
- Changelog Markdown release files with prefixes `HEADLESS-`, `PIPE-`, `TABS-` are review files and skipped by the generator.

## Development Workflow

- Landing/docs changes: edit `index.html` / `docs/index.html` directly, then run `python tools/verify_site.py`.
- Changelog content: edit the `CHANGELOG-v*.md` file on **main** (other repo checkout), never here. Here, only regenerate the archive via `tools/make_changelog_page.py`.
- Run a single unittest: `python -m unittest tools.test_changelog_generator.ChangelogGeneratorTests.test_old_and_new_release_headers`.

## Task Context

- For changelog bugs, ask for the release version and the exact `CHANGELOG-v*.md` content from main (formats A and B differ; both parsers are in `tools/make_changelog_page.py`).
- For visual issues, ask which page/section and light vs dark; `verify_site.py` screenshots land in `.shots/repair/`.

## Definition of Done

- `python tools/verify_site.py` passes (links, layouts, no-JS, deep links) — report its result.
- `python -m unittest tools.test_changelog_generator` passes for any generator change.
- Preview under `http://localhost:4400/NovaCode/`-style relative paths still resolves (verify_site enforces the prefix).

## Guardrails

- **NEVER** hand-edit `changelog/index.html` between the `<!-- generated:changelog-* -->` markers — CI regenerates and overwrites it.
- **NEVER** write release-note content in this checkout — sources belong on main's `changelog/`.
- **NEVER** use absolute (`/`-prefixed) asset or nav paths — GitHub Pages serves under `/NovaCode/`.
- **NEVER** commit `.shots/`, `node_modules/`, `__pycache__/` (gitignored) or `package-lock.json` — dependency installs here are deliberately `--no-save --no-package-lock`.

## Key Files

| File | Purpose |
|------|---------|
| `index.html` | Landing page: markup, CSS, and JS in one zero-build file |
| `changelog/index.html` | Generated release archive; edit only via the generator |
| `docs/index.html` | Documentation page |
| `tools/make_changelog_page.py` | Parses main's release Markdown → archive HTML (both header formats) |
| `tools/verify_site.py` | Playwright regression suite; serves site under `/NovaCode/` |
| `assets/changelog.js` | Live refresh from GitHub main; degrades to saved archive |
| `DESIGN.md` | Binding design constraints (fonts, radii, motion) |
