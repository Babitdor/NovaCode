# Nova website

Static HTML, CSS and JavaScript. No build is required.

The published files belong on `Babitdor/NovaCode`'s **gh-pages** branch.
Release sources belong on **main**, in `changelog/CHANGELOG-v*.md`.
All site navigation and asset paths are relative, including under `/NovaCode/`.

Preview with `python -m http.server 4400`, then visit `http://localhost:4400/`.

The changelog embeds the complete saved archive and checks GitHub's main branch for
new and edited releases on each visit. It downloads only changed entries, at most four
at a time. GitHub API limits or network failures leave the saved archive available.
Native release disclosure controls also work without JavaScript. Existing release
links such as `#v1-0-30` and `#v1.0.30` open the matching entry.

Refresh the saved archive before publishing:

```powershell
python tools/make_changelog_page.py --source-dir ../NovaCode/changelog
```

Omit `--source-dir` to fetch the Markdown from GitHub instead. The Changelog workflow
also checks out main as its source and commits generated changes to gh-pages.
It runs when the generator/workflow changes, or by manual dispatch on gh-pages.
Scheduled workflows require a copy on the repository's default branch; a schedule
on gh-pages alone does not run. GitHub does not trigger a Pages build from a bot's
`GITHUB_TOKEN` commit, so publish the refreshed archive through your regular Pages
deployment. Browser updates do not depend on rebuilding the archive.

Regression checks require Python Playwright (`python -m pip install playwright`)
and installed Microsoft Edge:

```powershell
python tools/verify_site.py
python -m unittest tools.test_changelog_generator
```

Add `--live` to `tools/verify_site.py` to check the real main branch instead of
the deterministic network fixtures.

This checks every local link/asset, documentation topics and history, release
permalinks and keyboard controls, simulated live updates and network failures,
mobile layouts, reduced motion and reading without JavaScript. Screenshots and a
report are written to `.shots/repair/`.

Install optional accessibility checks with:

```powershell
npm install --no-save --no-package-lock --ignore-scripts axe-core
```

The browser check includes axe when it is present.
