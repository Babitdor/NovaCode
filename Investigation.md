Exactly—then I’d keep **Textual as the stable host** and make your coding agent capable of modifying a declarative layer *above* Textual.

If your current harness is roughly `Textual App → fixed widgets → coding agent`, evolve it toward:

```text
Textual App (stable kernel)
        │
        ▼
┌─────────────────────────────────────┐
│ Dynamic Harness Runtime             │
│                                     │
│ Layout tree     Component registry  │
│ Commands        Keybindings         │
│ Themes          Workspace state     │
└──────────────┬──────────────────────┘
               │ Harness API
               ▼
          Coding Agent
               │
     ┌─────────┴─────────┐
     ▼                   ▼
 coding tools       harness tools
 read_file()        ui.inspect()
 edit_file()        ui.patch()
 shell()            ui.preview()
 git_diff()         command.add()
 ...                theme.patch()
```

The important part is that I **wouldn't have the agent directly edit your Textual Python source for routine customization**. Give it structured tools such as:

```python
ui.inspect()
ui.get_tree()

ui.add_widget(
    type="LogPanel",
    id="agent_logs",
    parent="right_sidebar",
)

ui.set_property(
    id="agent_logs",
    property="height",
    value="30%",
)

ui.move("terminal", after="agent_logs")
ui.hide("file_tree")
ui.show("file_tree")

ui.set_css("#terminal { height: 40%; }")

ui.bind("ctrl+t", "toggle_terminal")

ui.preview()
ui.commit()
ui.rollback()
```

Textual is actually well suited to this because its widgets, reactive state, CSS, screens, bindings, and compose model already give you many of the primitives.

The extra layer I'd add is an **agent-editable UI specification**:

```yaml
workspace: coding

layout:
  - type: Header

  - type: Horizontal
    id: main
    children:
      - type: FileTree
        id: files
        width: 22%

      - type: Vertical
        id: center
        children:
          - type: Conversation
            id: agent
          - type: Terminal
            id: terminal
            height: 35%

  - type: StatusBar

bindings:
  ctrl+t: terminal.toggle
  ctrl+b: files.toggle
  ctrl+l: logs.toggle
```

Your Textual application becomes a **renderer/interpreter of this specification**, rather than the specification being buried in `compose()`.

Then something neat becomes possible. You tell your coding agent:

> "We're spending a lot of time running tests. Optimize your harness for this project."

The agent can inspect its current UI and decide:

```text
Current
────────────────
Files | Agent
      | Terminal

       ↓ agent mutation

Files | Agent    | Tests
      |          | ✓ 138
      |----------| ✗ 2
      | Terminal | 4.2s
```

It might also create a `/test` command, bind `Ctrl+Shift+T`, add a test-output watcher, and save the configuration as `python-debugging`.

But I'd go **one step further** for a coding agent.

Give it an `extension.create()` capability.

Since it can already write Python, when existing UI primitives aren't sufficient, it could generate a new Textual widget:

```python
class TestPanel(Static):

    DEFAULT_CSS = """
    TestPanel {
        width: 30%;
        border: solid $accent;
    }
    """

    def update_results(self, results):
        ...
```

Then:

```text
agent
  │
  │ extension.create(...)
  ▼
sandbox/extensions/test_panel.py
  │
  ▼
validate
  │
  ├── syntax check
  ├── import check
  ├── permission check
  └── Textual compatibility check
  │
  ▼
load extension
  │
  ▼
ui.mount("TestPanel")
```

So there are **three levels of agency**:

```text
1. CONFIGURATION
   Agent changes CSS, layout, bindings, visibility.

2. COMPOSITION
   Agent adds/removes/rearranges registered Textual widgets.

3. CREATION
   Agent writes entirely new widgets/tools/extensions.
```

I'd let level 1 happen quite freely, make level 2 transactional, and put level 3 behind validation + rollback.

And I'd give the user one extremely important keybinding:

```text
Ctrl+Shift+Backspace

RESTORE SAFE UI
```

That bypasses the agent configuration entirely and renders a hard-coded minimal Textual screen. Otherwise the agent will eventually invent the world's first UI where the button needed to repair the UI is hidden by the UI it broke.

The broader idea is that **your coding agent's TUI becomes part of its tool environment**. Editing files changes the target project; calling the harness API changes the environment through which the agent works.

If you show me your current **Textual `App`/`compose()` code and project structure**, I can design this around your existing harness rather than proposing a new architecture from scratch.