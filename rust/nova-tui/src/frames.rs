//! Wire frames, decoded by matching on `"t"` and `"c"`.
//!
//! See `rust/PROTOCOL.md`. Two rules from that document are load-bearing here:
//! an unknown `"t"` becomes [`Inbound::Unknown`] and an unknown `"c"` becomes
//! [`UiEvent::Other`], so a server from a newer build never kills the client.
//! Every field read is optional-with-default for the same reason.

use serde_json::Value;

/// The home banner: which layout, whether the rain animates, and the version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HomeConfig {
    /// The saved layout name (`dashboard`, `classic`, `signal`, …).
    pub style: String,
    /// Whether the saved preference enables animation.
    pub animation: bool,
    /// Nova's version, for the banner caption.
    pub version: String,
}

/// Protocol version this build speaks.
pub const PROTOCOL_VERSION: u64 = 1;

/// One rendered block of command output.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub kind: String,
    pub text: String,
}

/// One command the server offers, from a `catalogue` frame.
///
/// `route` says where the behaviour lives (`passthrough`, `screen`, `client`)
/// and `served` says whether the server answers it today, so the palette can
/// show a command the client cannot run yet instead of hiding it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandHelp {
    pub name: String,
    pub help: String,
    pub aliases: Vec<String>,
    pub wants_text: bool,
    pub route: String,
    pub served: bool,
}

/// One session in a `sessions` reply.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionInfo {
    pub id: String,
    pub name: String,
    pub cwd: Option<String>,
    pub status: String,
    pub model: Option<String>,
}

/// One council seat as the planner introduced it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CouncilAgent {
    pub id: String,
    pub name: String,
    pub avatar: String,
    pub color: String,
}

/// One folded progress event from `/council <task>`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CouncilUpdate {
    pub prompt: String,
    pub run_id: String,
    pub event: String,
    pub phase: String,
    pub agent_id: String,
    pub agent_name: String,
    pub reason: String,
    pub proposals: u64,
    pub critiques: u64,
    pub ballots: u64,
    pub entropy: Option<f64>,
    pub agents: Vec<CouncilAgent>,
    pub outcome: String,
}

/// One row of a todo update.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Todo {
    pub content: String,
    pub status: String,
}

/// A decoded `ui_events` dataclass. Only the classes the client renders are
/// modelled; the rest keep their name so they can be logged and skipped.
#[derive(Debug, Clone, PartialEq)]
pub enum UiEvent {
    TextDelta(String),
    ReasoningDelta(String),
    TextDiscard,
    AssistantMessage {
        text: String,
        agent_name: String,
        agent_color: Option<String>,
        is_subagent: bool,
    },
    SubagentTask(SubagentTask),
    SubagentPreview {
        id: String,
        text: String,
    },
    ToolCall {
        name: String,
        display_str: String,
        icon: String,
        call_id: Option<String>,
        args: Value,
    },
    ToolResult {
        preview: String,
        full_output: String,
        is_error: bool,
        call_id: Option<String>,
    },
    FileOp {
        call_id: Option<String>,
        parent_call_id: Option<String>,
        operation: String,
        path: Option<String>,
        full_output: String,
        diff: String,
        added: u64,
        removed: u64,
        failed: bool,
    },
    TodoUpdate(Vec<Todo>),
    StatusUpdate(Option<String>),
    UsageUpdate {
        input_tokens: u64,
        output_tokens: u64,
        session_tokens: Option<u64>,
        context_reported: bool,
    },
    ContextUsage(u64),
    ErrorOutput(String),
    Error {
        message: String,
        is_provider_notice: bool,
    },
    Done,
    Cancelled,
    ContextMessage {
        message: String,
        icon: String,
    },
    Council(CouncilUpdate),
    Other(String),
}

/// Session-owned state displayed in the prompt composer's controls.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ComposerState {
    pub agent: String,
    pub color: Option<String>,
    pub prime: bool,
    pub passing: bool,
    pub plan: bool,
    pub browser: bool,
    pub serena: bool,
    pub lite: String,
    pub code: bool,
    pub oracle: bool,
    pub context_window: u64,
}

/// A row of the live subagents panel.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SubagentTask {
    pub id: String,
    pub name: String,
    pub label: String,
    pub status: String,
    pub phase_kind: String,
    pub error: String,
    pub context_used: u64,
    pub context_window: u64,
}

/// The palette the server chose, from a `ui_theme` frame.
///
/// Named `ThemePalette` rather than `Palette` because `state::Palette` is the
/// `/` autocomplete overlay: two unrelated meanings, one short word.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ThemePalette {
    pub name: String,
    pub dark: bool,
    pub primary: Option<String>,
    pub background: Option<String>,
    pub foreground: Option<String>,
    pub surface: Option<String>,
    pub panel: Option<String>,
    pub muted: Option<String>,
    pub activity: Option<String>,
    pub border: Option<String>,
    pub success: Option<String>,
    pub warning: Option<String>,
    pub error: Option<String>,
}

/// One inbound frame.
#[derive(Debug, Clone, PartialEq)]
pub enum Inbound {
    Hello {
        protocol: u64,
    },
    Ready {
        session_id: Option<String>,
        cwd: Option<String>,
        model: Option<String>,
        assistant_id: Option<String>,
        /// The home banner the server asked for, if it sent one.
        home: Option<HomeConfig>,
        composer: Option<ComposerState>,
        theme: Option<Box<ThemePalette>>,
    },
    Composer(ComposerState),
    Ralph {
        label: String,
        active: bool,
    },
    Event(UiEvent),
    Interrupt {
        id: String,
        kind: String,
        payload: Value,
    },
    TurnDone {
        id: String,
        ok: bool,
    },
    Error {
        message: String,
        code: Option<String>,
    },
    Notice {
        level: String,
        message: String,
    },
    CommandResult {
        id: String,
        ok: bool,
        message: String,
        blocks: Vec<Block>,
        action: Option<String>,
        /// Text the command wants run as a turn (`/init`, `/goal`, `/skill:x`).
        prompt: Option<String>,
        /// A modal the command produced, as its raw `{kind, title, payload}`.
        screen: Option<Value>,
        /// A session id the client should attach to (`/tabs`), if any.
        attach: Option<String>,
        /// A session name the client should ask the server to spawn.
        create: Option<String>,
        /// A saved session id the spawned child should continue (`/resume`).
        resume: Option<String>,
        /// The `/nova` sprite sheet, when the command turned it on.
        avatar: Option<Value>,
        /// A session id to close (`""` means the active session).
        close: Option<String>,
    },
    /// Every command the server offers, in `/help` order.
    Catalogue {
        commands: Vec<CommandHelp>,
    },
    /// One chunk of a shell command's output, as plain lines.
    Pty {
        id: String,
        command: String,
        text: String,
    },
    /// A shell command finished; `code` is its exit status.
    PtyExit {
        id: String,
        code: i32,
    },
    /// A modal to present; the answer travels back as `screen_result`.
    Screen {
        id: String,
        kind: String,
        title: Option<String>,
        payload: Value,
    },
    /// The palette @@/theme@@ selected; the client repaints with it.
    UiTheme(ThemePalette),
    Sessions {
        sessions: Vec<SessionInfo>,
        active: Option<String>,
        composer: Option<ComposerState>,
    },
    Unknown {
        kind: String,
    },
}

/// A string field, or `""` when absent or not a string.
fn text(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// An optional string field.
fn opt_text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_string)
}

/// A bool field, `false` when absent.
fn flag(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

/// An unsigned field, `0` when absent.
fn count(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

/// Decode one `ev` payload body (`{"c": .., "d": ..}`).
pub fn decode_ui_event(doc: &Value) -> UiEvent {
    let name = text(doc, "c");
    let data = doc.get("d").cloned().unwrap_or(Value::Null);
    match name.as_str() {
        "TextDelta" => UiEvent::TextDelta(text(&data, "text")),
        "ReasoningDelta" => UiEvent::ReasoningDelta(text(&data, "text")),
        "TextDiscard" => UiEvent::TextDiscard,
        "AssistantMessage" => UiEvent::AssistantMessage {
            text: text(&data, "text"),
            agent_name: text(&data, "agent_name"),
            agent_color: opt_text(&data, "agent_color"),
            is_subagent: flag(&data, "is_subagent"),
        },
        "SubagentTask" => UiEvent::SubagentTask(SubagentTask {
            id: text(&data, "task_id"),
            name: text(&data, "subagent_type"),
            label: opt_text(&data, "label")
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| text(&data, "description")),
            status: text(&data, "status"),
            phase_kind: text(&data, "phase_kind"),
            error: text(&data, "error"),
            context_used: count(&data, "context_used"),
            context_window: count(&data, "context_window"),
        }),
        "SubagentPreview" => UiEvent::SubagentPreview {
            id: text(&data, "task_id"),
            text: text(&data, "text"),
        },
        "ToolCall" => UiEvent::ToolCall {
            name: text(&data, "name"),
            display_str: text(&data, "display_str"),
            icon: text(&data, "icon"),
            call_id: opt_text(&data, "call_id"),
            args: data.get("args").cloned().unwrap_or(Value::Null),
        },
        "ToolResult" => UiEvent::ToolResult {
            preview: text(&data, "preview"),
            full_output: text(&data, "full_output"),
            is_error: flag(&data, "is_error"),
            call_id: opt_text(&data, "call_id"),
        },
        "FileOp" => {
            let record = &data["record"];
            UiEvent::FileOp {
                call_id: opt_text(&data, "call_id"),
                parent_call_id: opt_text(&data, "parent_call_id")
                    .or_else(|| opt_text(record, "parent_call_id")),
                operation: opt_text(record, "operation")
                    .unwrap_or_else(|| text(record, "tool_name")),
                path: opt_text(record, "display_path")
                    .or_else(|| opt_text(record, "physical_path")),
                full_output: text(&data, "full_output"),
                diff: text(record, "diff"),
                added: count(&record["metrics"], "lines_added"),
                removed: count(&record["metrics"], "lines_removed"),
                failed: text(record, "status") == "error" || opt_text(record, "error").is_some(),
            }
        }
        "TodoUpdate" => {
            let todos = data
                .get("todos")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| Todo {
                            content: text(row, "content"),
                            status: text(row, "status"),
                        })
                        .collect()
                })
                .unwrap_or_default();
            UiEvent::TodoUpdate(todos)
        }
        "StatusUpdate" => UiEvent::StatusUpdate(opt_text(&data, "message")),
        "UsageUpdate" => UiEvent::UsageUpdate {
            input_tokens: count(&data, "input_tokens"),
            output_tokens: count(&data, "output_tokens"),
            session_tokens: data.get("session_tokens").and_then(Value::as_u64),
            context_reported: flag(&data, "context_reported"),
        },
        "ContextUsage" => UiEvent::ContextUsage(count(&data, "input_tokens")),
        "ErrorOutput" => UiEvent::ErrorOutput(text(&data, "text")),
        "Error" => UiEvent::Error {
            message: text(&data, "message"),
            is_provider_notice: flag(&data, "is_provider_notice"),
        },
        "Done" => UiEvent::Done,
        "Cancelled" => UiEvent::Cancelled,
        "ContextMessage" => UiEvent::ContextMessage {
            message: text(&data, "message"),
            icon: text(&data, "icon"),
        },
        "CouncilUpdate" => UiEvent::Council(CouncilUpdate {
            prompt: text(&data, "prompt"),
            run_id: text(&data, "run_id"),
            event: text(&data, "event"),
            phase: text(&data, "phase"),
            agent_id: text(&data, "agent_id"),
            agent_name: text(&data, "agent_name"),
            reason: text(&data, "reason"),
            proposals: count(&data, "proposals"),
            critiques: count(&data, "critiques"),
            ballots: count(&data, "ballots"),
            entropy: data.get("entropy").and_then(Value::as_f64),
            agents: data
                .get("agents")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| CouncilAgent {
                            id: text(row, "id"),
                            name: text(row, "name"),
                            avatar: text(row, "avatar"),
                            color: text(row, "color"),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            outcome: text(&data, "outcome"),
        }),
        other => UiEvent::Other(other.to_string()),
    }
}

/// Decode one `catalogue` entry.
fn decode_command_help(doc: &Value) -> CommandHelp {
    CommandHelp {
        name: text(doc, "name"),
        help: text(doc, "help"),
        aliases: doc
            .get("aliases")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default(),
        wants_text: doc
            .get("wants_text")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        route: text(doc, "route"),
        // Absent means "unknown", and unknown is not offered as runnable.
        served: doc.get("served").and_then(Value::as_bool).unwrap_or(false),
    }
}

fn decode_composer(doc: &Value) -> ComposerState {
    ComposerState {
        agent: text(doc, "agent"),
        color: opt_text(doc, "color"),
        prime: flag(doc, "prime"),
        passing: flag(doc, "passing"),
        plan: flag(doc, "plan"),
        browser: flag(doc, "browser"),
        serena: flag(doc, "serena"),
        lite: text(doc, "lite"),
        code: flag(doc, "code"),
        oracle: flag(doc, "oracle"),
        context_window: count(doc, "context_window"),
    }
}

fn decode_theme(doc: &Value) -> ThemePalette {
    ThemePalette {
        name: text(doc, "name"),
        dark: flag(doc, "dark"),
        primary: opt_text(doc, "primary"),
        background: opt_text(doc, "background"),
        foreground: opt_text(doc, "foreground"),
        surface: opt_text(doc, "surface"),
        panel: opt_text(doc, "panel"),
        muted: opt_text(doc, "muted"),
        activity: opt_text(doc, "activity"),
        border: opt_text(doc, "border"),
        success: opt_text(doc, "success"),
        warning: opt_text(doc, "warning"),
        error: opt_text(doc, "error"),
    }
}

/// Decode one line into an [`Inbound`], or `None` for noise.
///
/// `None` is used for blank lines, non-JSON and JSON that is not an object —
/// the same cases `protocol.loads` skips on the Python side.
pub fn decode(line: &str) -> Option<Inbound> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let doc: Value = serde_json::from_str(trimmed).ok()?;
    if !doc.is_object() {
        return None;
    }
    let kind = text(&doc, "t");
    Some(match kind.as_str() {
        "hello" => Inbound::Hello {
            protocol: count(&doc, "protocol"),
        },
        "ready" => Inbound::Ready {
            session_id: opt_text(&doc, "session_id"),
            cwd: opt_text(&doc, "cwd"),
            model: opt_text(&doc, "model"),
            assistant_id: opt_text(&doc, "assistant_id"),
            home: doc.get("home").map(|home| HomeConfig {
                style: text(home, "style"),
                animation: flag(home, "animation"),
                version: text(home, "version"),
            }),
            composer: doc.get("composer").map(decode_composer),
            theme: doc
                .get("theme")
                .filter(|value| value.is_object())
                .map(|value| Box::new(decode_theme(value))),
        },
        "composer" => Inbound::Composer(decode_composer(&doc)),
        "ralph" => Inbound::Ralph {
            label: match doc["kind"].as_str() {
                Some("RalphStarted") => text(&doc, "task"),
                Some("IterationStarted") => format!(
                    "Iteration {}/{}",
                    count(&doc, "iteration"),
                    count(&doc, "max_iterations")
                ),
                Some("IterationFinished") => {
                    format!("Iteration {} finished", count(&doc, "iteration"))
                }
                _ => text(&doc, "detail"),
            },
            active: doc["kind"].as_str() != Some("RalphFinished"),
        },
        "ev" => Inbound::Event(decode_ui_event(&doc)),
        "ui_theme" => Inbound::UiTheme(decode_theme(&doc)),
        "interrupt" => Inbound::Interrupt {
            id: text(&doc, "id"),
            kind: text(&doc, "kind"),
            payload: doc.get("payload").cloned().unwrap_or(Value::Null),
        },
        "turn_done" => Inbound::TurnDone {
            id: text(&doc, "id"),
            ok: flag(&doc, "ok"),
        },
        "error" => Inbound::Error {
            message: text(&doc, "message"),
            code: opt_text(&doc, "code"),
        },
        "pty" => Inbound::Pty {
            id: text(&doc, "id"),
            command: text(&doc, "command"),
            text: text(&doc, "text"),
        },
        "pty_exit" => Inbound::PtyExit {
            id: text(&doc, "id"),
            code: doc.get("code").and_then(Value::as_i64).unwrap_or(0) as i32,
        },
        "notice" => Inbound::Notice {
            level: text(&doc, "level"),
            message: text(&doc, "message"),
        },
        "command_result" => Inbound::CommandResult {
            id: text(&doc, "id"),
            ok: flag(&doc, "ok"),
            message: text(&doc, "message"),
            blocks: doc
                .get("blocks")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| Block {
                            kind: text(row, "kind"),
                            text: text(row, "text"),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            action: opt_text(&doc, "action"),
            prompt: opt_text(&doc, "prompt"),
            screen: doc.get("screen").cloned().filter(|value| !value.is_null()),
            attach: opt_text(&doc, "attach"),
            create: opt_text(&doc, "create"),
            resume: opt_text(&doc, "resume"),
            avatar: doc.get("avatar").cloned().filter(|value| !value.is_null()),
            close: opt_text(&doc, "close"),
        },
        "catalogue" => Inbound::Catalogue {
            commands: doc
                .get("commands")
                .and_then(Value::as_array)
                .map(|rows| rows.iter().map(decode_command_help).collect())
                .unwrap_or_default(),
        },
        "screen" => Inbound::Screen {
            id: text(&doc, "id"),
            kind: text(&doc, "kind"),
            title: opt_text(&doc, "title"),
            payload: doc.get("payload").cloned().unwrap_or(Value::Null),
        },
        "sessions" => Inbound::Sessions {
            composer: doc.get("composer").map(decode_composer),
            sessions: doc
                .get("sessions")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| SessionInfo {
                            id: text(row, "id"),
                            name: text(row, "name"),
                            cwd: opt_text(row, "cwd"),
                            status: text(row, "status"),
                            model: opt_text(row, "model"),
                        })
                        .collect()
                })
                .unwrap_or_default(),
            active: opt_text(&doc, "active"),
        },
        other => Inbound::Unknown {
            kind: other.to_string(),
        },
    })
}

/// Serialize a frame as one newline-terminated line.
fn line(kind: &str, mut fields: serde_json::Map<String, Value>) -> String {
    fields.insert("t".to_string(), Value::String(kind.to_string()));
    let body = serde_json::to_string(&Value::Object(fields)).unwrap_or_else(|_| "{}".into());
    format!("{body}\n")
}

/// Build the `hello` frame.
pub fn hello() -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("protocol".into(), Value::from(PROTOCOL_VERSION));
    fields.insert("client".into(), Value::from("nova-tui/0.1"));
    line("hello", fields)
}

/// Build the `auth` frame.
pub fn auth(token: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("token".into(), Value::from(token));
    line("auth", fields)
}

/// Build a `prompt` frame.
pub fn prompt(id: &str, text: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(id));
    fields.insert("text".into(), Value::from(text));
    line("prompt", fields)
}

/// Build a `command` frame.
pub fn command(id: &str, name: &str, args: Option<&str>) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(id));
    fields.insert("name".into(), Value::from(name));
    if let Some(args) = args {
        fields.insert("args".into(), Value::from(args));
    }
    line("command", fields)
}

/// Build a `screen_result` frame: the answer to a `screen`.
pub fn screen_result(id: &str, value: Value) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(id));
    fields.insert("result".into(), value);
    line("screen_result", fields)
}

/// Build an `attach` frame: switch this connection to another session.
///
/// The server answers `notice` then `sessions`, so the tab bar repaints from
/// the same reducer path a manual refresh uses.
pub fn attach(session_id: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("session_id".into(), Value::from(session_id));
    line("attach", fields)
}
/// Build a `create_session` frame: spawn a child and attach to it.
pub fn create_session(name: &str, resume: Option<&str>) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("name".into(), Value::from(name));
    if let Some(id) = resume.filter(|id| !id.is_empty()) {
        // `/resume` continues a saved session rather than starting an empty one.
        fields.insert("resume".into(), Value::from(id));
    }
    line("create_session", fields)
}

/// Build a `close_session` frame: stop a child this connection spawned.
pub fn close_session(session_id: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("session_id".into(), Value::from(session_id));
    line("close_session", fields)
}

/// Build a `catalogue` frame: ask the server for its command list.
pub fn catalogue() -> String {
    line("catalogue", serde_json::Map::new())
}

/// Build a `pty_input` frame: keystrokes for the open terminal.
pub fn pty_input(terminal_id: &str, keys: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(terminal_id));
    fields.insert("text".into(), Value::from(keys));
    line("pty_input", fields)
}

/// Build a `pty_close` frame: stop the open terminal.
pub fn pty_close(terminal_id: &str) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(terminal_id));
    line("pty_close", fields)
}

/// Build a `cancel` frame.
pub fn cancel() -> String {
    line("cancel", serde_json::Map::new())
}

/// Build an `interrupt_reply` frame.
pub fn interrupt_reply(id: &str, result: Value) -> String {
    let mut fields = serde_json::Map::new();
    fields.insert("id".into(), Value::from(id));
    fields.insert("result".into(), result);
    line("interrupt_reply", fields)
}

/// Build a `sessions` frame.
pub fn sessions() -> String {
    line("sessions", serde_json::Map::new())
}

/// Build a `shutdown` frame.
pub fn shutdown() -> String {
    line("shutdown", serde_json::Map::new())
}
