# Project onboarding

**New to a project? Start with `/onboard`.** Inspired by
[Understand Anything](https://github.com/Egonex-AI/Understand-Anything), it parses
the local workspace with the already installed Graphify language parsers and
saves a persistent graph and guide in `.nova/onboard/`. The local artifact
dashboard provides file/symbol search, directory filters, a clickable dependency
map, a learning tour and incoming dependency impact tracing. It needs no Node
installation, browser credentials or external dashboard assets.

Use `/onboard open` to reopen the dashboard, `/onboard tour` for a conversational
tour, `/onboard ask <question>` for source-grounded Q&A, and
`/onboard impact <symbol or path>` before changing code. The dashboard's **Ask
Nova** tab prepares a command to paste into the conversation. Nova explains the
architecture and business flows using its normal model and tool permissions;
the initial deterministic guide contains structural facts only. Reopen the
dashboard after Nova enriches the guide. Model calls use your configured provider.

`/onboard refresh` rebuilds after changes. File-content fingerprints detect
new, edited and deleted inputs; saved analyses display a staleness notice.
Scans respect Git ignore rules and skip links, generated directories and common
secret filenames. Static parsing can miss dynamic dependencies; directory
groups are not semantic architecture layers. Limits: 5,000 source/Markdown
files, 1 MiB per file, 64 MiB total input and 32 MiB graph. The parsing deadline
scales with file count and input size from two to ten minutes; Escape cancels it.
Refresh replaces generated graph/guide files, so copy edited guides
elsewhere before rebuilding. This command currently requires a local workspace;
Plan Mode prevents building or refreshing the saved analysis.

