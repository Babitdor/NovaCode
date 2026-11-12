# Command Center

Open `/command-center` or `/cc` to browse Nova conversations across projects.
The layout follows `assets/screenshot/CommandCenter.png`: project groups on the
left, status filters above them, and selected task details on the right.

| Key | Action |
| --- | --- |
| Up / Down | Select a conversation |
| Enter | Open its live tab, or resume a saved conversation in a separate tab |
| Tab / Shift+Tab | Cycle All, Needs you, Working, Ready and Inactive |
| g | Group by project, group by status, or remove grouping |
| Ctrl+F | Search task, project, model or session ID |
| d | Expand details; also makes details available in narrow terminals |
| r | Refresh saved sessions |
| Esc | Return from details/search, then close |

The current project appears first. Live tab state updates once a second without
rereading saved conversations. Saved sessions load 25 at a time; select **Show
more saved sessions** to load another page. Filter counts cover the loaded
sessions and live tabs, with duplicate saved/live identities merged.

**Working** requires a live running or starting tab in this Nova instance.
**Needs you** indicates a pending approval/question or a recorded saved blocker.
**Ready** means an open idle tab. Stored conversations without a live tab appear
**Inactive**, even when their older metadata says `active`. This view does not
claim to monitor other Nova processes or external coding assistants.

Details show the recorded model, project and session identity, live reasoning,
branch and measured input/output tokens when available, and bounded human and
assistant text previews. Older sessions do not store every screenshot field;
missing facts say **Not recorded**. Tool output and hidden reasoning do not appear
in the message preview. Histories are read only for the selected conversation,
off the UI thread, and late results cannot overwrite another selection's details.

Browsing does not save, delete, start, cancel or approve work. Opening a saved
conversation reuses Nova's existing resume-in-a-tab flow, including capacity,
workspace approval and duplicate-session checks, and preserves the current turn.
Opening a live conversation switches to its tab; any pending approval stays in
its existing approval view.
