# NovaCode v1.0.58

## Added

- NotebookEditTool for inserting, replacing, and deleting Jupyter notebook cells
  without executing code. Edits preserve unrelated cells and metadata, clear
  stale outputs from changed code cells, and use atomic file replacement.
- Workspace-bound notebook editing with approval checks, Cowork write grants,
  and plan-mode restrictions.
- Optional native bfs traversal and ugrep content search, with ripgrep and Python
  fallbacks. Imported agent definitions recognize GlobTool and GrepTool aliases.
- Animated terminal-title spinner while the active session is working, including
  single-session mode. The plain title returns when work finishes.
- CI coverage for notebook editing, native search, and terminal-title activity.

## Fixed

- Python grep fallback now matches basename globs in nested directories, searches
  only the requested file, treats literal patterns literally, and skips binary data.
- Renamed CI documentation so GitHub displays the main NovaCode README.

## Verification

- 266 focused regression tests passed before the version bump, with one optional
  native-search test skipped on Windows. Real bfs and ugrep checks passed on Linux.
- Broader session tests still have pre-existing banner-layout failures.
