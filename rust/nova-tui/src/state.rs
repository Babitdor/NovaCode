//! `AppState` and the pure reducer that turns frames into transcript state.
//!
//! `apply` is a pure function of (`AppState`, `Inbound`) → [`Effect`] so the frame
//! replay test needs no socket, no terminal and no clock. Anything that has to
//! touch the outside world (writing a frame, clearing the screen, quitting) is
//! returned as an [`Effect`] for the caller to perform.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::frames::{
    Block, CommandHelp, CouncilAgent, CouncilUpdate, Inbound, SessionInfo, ThemePalette, Todo,
    UiEvent,
};
use crate::ui::avatar::Avatar;
use crate::ui::home::Home;

/// What the caller must do after a frame is applied.
///
/// `Answer` carries JSON, which is why this is `PartialEq` but not `Eq`.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// Write this already-serialized line to the server.
    Send(String),
    /// Copy text to the system clipboard (`/copy`, `/copy all`).
    Copy { text: String, all: bool },
    /// The server accepted a quit; stop and restore the terminal.
    Quit,
    /// Drop the transcript (the `/clear` action).
    ClearTranscript,
    /// Answer the open modal with this JSON, in the shape its kind documents.
    Answer { screen_id: String, value: Value },
}

/// One shell command the server is streaming into a terminal pane.
///
/// Lines, not cells: the server strips colour and hands over whole lines, so a
/// cursor-addressed program is still rendered as text. `partial` holds the line
/// that has not ended yet, and `pending_cr` remembers a carriage return until
/// the next character decides whether it began a line break or rewrote the line.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Terminal {
    pub id: String,
    pub command: String,
    pub lines: Vec<String>,
    pub partial: String,
    pub pending_cr: bool,
    pub exit: Option<i32>,
}

/// The most lines a terminal keeps; older lines scroll out of reach.
pub const TERMINAL_LINES: usize = 4000;

impl Terminal {
    fn newline(&mut self) {
        let line = std::mem::take(&mut self.partial);
        self.lines.push(line);
        if self.lines.len() > TERMINAL_LINES {
            let drop = self.lines.len() - TERMINAL_LINES;
            self.lines.drain(..drop);
        }
    }

    /// Fold one chunk of output into lines.
    pub fn push(&mut self, text: &str) {
        for glyph in text.chars() {
            if self.pending_cr {
                self.pending_cr = false;
                if glyph == '\n' {
                    self.newline();
                    continue;
                }
                self.partial.clear();
            }
            match glyph {
                '\r' => self.pending_cr = true,
                '\n' => self.newline(),
                '\u{8}' => {
                    self.partial.pop();
                }
                '\0' => {}
                other => self.partial.push(other),
            }
        }
    }

    /// The lines to draw: everything completed plus the unfinished line.
    pub fn view(&self) -> Vec<String> {
        let mut lines = self.lines.clone();
        if !self.partial.is_empty() {
            lines.push(self.partial.clone());
        }
        lines
    }

    /// Whether the pane should still take keystrokes.
    pub fn live(&self) -> bool {
        self.exit.is_none()
    }
}

/// A rendering-relevant status snapshot, driven by `ready`, `status` and usage.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Status {
    pub session_id: Option<String>,
    pub cwd: Option<String>,
    pub model: Option<String>,
    pub assistant_id: Option<String>,
    pub activity: Option<String>,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub session_tokens: Option<u64>,
    pub context_tokens: u64,
    pub connected: bool,
    /// The @@/theme@@ in force; empty until the server sends a palette.
    pub theme: String,
}

impl Status {
    /// The model, or a placeholder before `ready`.
    pub fn model_label(&self) -> String {
        self.model
            .clone()
            .unwrap_or_else(|| "connecting".to_string())
    }

    /// The last path component of the working directory, or `~`.
    pub fn cwd_label(&self) -> String {
        self.cwd
            .as_deref()
            .and_then(|path| {
                std::path::Path::new(path)
                    .file_name()
                    .map(|name| name.to_string_lossy().to_string())
            })
            .unwrap_or_else(|| "~".to_string())
    }
}

/// A tool call rendered as one collapsible card.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ToolCard {
    pub call_id: Option<String>,
    pub name: String,
    pub display: String,
    pub icon: String,
    pub preview: String,
    pub full_output: String,
    pub is_error: bool,
    pub finished: bool,
    pub expanded: bool,
}

/// One line of transcript.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BlockKind {
    User,
    Assistant,
    Reasoning,
    Tool(ToolCard),
    Command {
        ok: bool,
        message: String,
        blocks: Vec<Block>,
    },
    Notice {
        level: String,
        text: String,
    },
    Context {
        icon: String,
    },
    Error,
    FileOp {
        operation: String,
        path: String,
        call_id: Option<String>,
    },
}

/// A transcript entry plus the text it carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub kind: BlockKind,
    pub text: String,
    pub agent: String,
}

/// A modal the client is showing. The three kinds mirror `InterruptRequest`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Modal {
    Approval {
        id: String,
        kind: String,
        headline: String,
        detail: Vec<String>,
    },
}

/// The vocabulary a `screen` frame may use.
///
/// One closed set of shapes instead of one widget per screen: a new command
/// that needs a modal picks a kind, not a renderer. An unknown kind fails
/// closed (answered `null`) rather than leaving the server waiting.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScreenKind {
    List,
    Table,
    Detail,
    Form,
    Markdown,
    Progress,
    Confirm,
    Split,
}

impl ScreenKind {
    /// Parse a kind name from the wire.
    pub fn parse(kind: &str) -> Option<Self> {
        match kind {
            "list" => Some(Self::List),
            "table" => Some(Self::Table),
            "detail" => Some(Self::Detail),
            "form" => Some(Self::Form),
            "markdown" => Some(Self::Markdown),
            "progress" => Some(Self::Progress),
            "confirm" => Some(Self::Confirm),
            "split" => Some(Self::Split),
            _ => None,
        }
    }

    /// The name shown in the modal's border.
    pub fn label(self) -> &'static str {
        match self {
            Self::List => "list",
            Self::Table => "table",
            Self::Detail => "detail",
            Self::Form => "form",
            Self::Markdown => "markdown",
            Self::Progress => "progress",
            Self::Confirm => "confirm",
            Self::Split => "split",
        }
    }
}

/// One selectable row: a list item, a table row, or a detail action.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScreenRow {
    /// Identifier the answer quotes (\`{"id": ...}\`).
    pub id: String,
    /// Cell text, already stringified, one per column.
    pub cells: Vec<String>,
    /// Secondary text shown under a table row or list entry.
    pub detail: String,
}

impl ScreenRow {
    /// The text shown for this row in a single-column list.
    pub fn label(&self) -> &str {
        self.cells.first().map(String::as_str).unwrap_or_default()
    }
}

/// One form field: free text, or a toggle when @@toggle@@ is set.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ScreenField {
    pub key: String,
    pub label: String,
    /// The text typed so far, or @@\"on\"@@ / @@\"off\"@@ for a toggle.
    pub value: String,
    /// A toggle flips with Space instead of accepting typed text.
    pub toggle: bool,
    /// A fixed set of values, cycled with the arrow keys; empty for free text.
    pub options: Vec<String>,
}

/// A modal the server asked for, decoded into something drawable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScreenView {
    /// The `screen` frame's id; empty for a screen attached to a command result,
    /// which is informational and answers nothing.
    pub id: String,
    pub kind: ScreenKind,
    pub title: String,
    pub body: String,
    /// Left pane of a `split` screen (empty for every other kind).
    pub body_left: String,
    /// Right pane of a `split` screen (empty for every other kind).
    pub body_right: String,
    pub columns: Vec<String>,
    pub rows: Vec<ScreenRow>,
    pub fields: Vec<ScreenField>,
    pub actions: Vec<ScreenRow>,
    pub selected: usize,
    pub focused: usize,
    /// First visible content row, so a form taller than the popup scrolls.
    pub scroll: u16,
    pub done: u64,
    pub total: u64,
}

/// A string field of a JSON object, or `""` when absent.
fn body_text(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(|field| match field {
            Value::String(text) => Some(text.clone()),
            Value::Null => None,
            other => Some(other.to_string()),
        })
        .unwrap_or_default()
}

/// A boolean field of a JSON object, or false.
fn body_flag(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

/// An unsigned field of a JSON object, or zero.
fn body_count(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

impl ScreenView {
    /// Decode a `screen` payload into the shape the renderer draws.
    ///
    /// Args:
    ///     id: The frame's id, or `""` for a command-attached screen.
    ///     kind: The kind name from the wire.
    ///     title: Heading, empty when the server sent none.
    ///     payload: The screen body, as documented in `novacode_cli/ui/port.py`.
    ///
    /// Returns:
    ///     The view. An unknown kind is kept as `None` by the caller, which
    ///     answers `null` instead of waiting out `SCREEN_TIMEOUT`.
    pub fn decode(id: &str, kind: ScreenKind, title: &str, payload: &Value) -> Self {
        let mut view = Self {
            id: id.to_string(),
            kind,
            title: title.to_string(),
            body: String::new(),
            body_left: String::new(),
            body_right: String::new(),
            columns: Vec::new(),
            rows: Vec::new(),
            fields: Vec::new(),
            actions: Vec::new(),
            selected: 0,
            focused: 0,
            scroll: 0,
            done: 0,
            total: 0,
        };
        match kind {
            ScreenKind::List => {
                view.rows = payload
                    .get("items")
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .map(|item| ScreenRow {
                                id: body_text(item, "id"),
                                cells: vec![body_text(item, "label")],
                                detail: body_text(item, "detail"),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
            }
            ScreenKind::Table => {
                view.columns = payload
                    .get("columns")
                    .and_then(Value::as_array)
                    .map(|columns| {
                        columns
                            .iter()
                            .map(|column| body_text(column, "title"))
                            .collect()
                    })
                    .unwrap_or_default();
                let keys: Vec<String> = payload
                    .get("columns")
                    .and_then(Value::as_array)
                    .map(|columns| {
                        columns
                            .iter()
                            .map(|column| body_text(column, "key"))
                            .collect()
                    })
                    .unwrap_or_default();
                view.rows = payload
                    .get("rows")
                    .and_then(Value::as_array)
                    .map(|rows| {
                        rows.iter()
                            .map(|row| ScreenRow {
                                id: body_text(row, "id"),
                                cells: keys.iter().map(|key| body_text(row, key)).collect(),
                                detail: body_text(row, "detail"),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
            }
            ScreenKind::Detail | ScreenKind::Markdown => {
                view.body = body_text(payload, "markdown");
                view.actions = payload
                    .get("actions")
                    .and_then(Value::as_array)
                    .map(|actions| {
                        actions
                            .iter()
                            .map(|action| ScreenRow {
                                id: body_text(action, "id"),
                                cells: vec![body_text(action, "label")],
                                detail: String::new(),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
            }
            ScreenKind::Form => {
                view.fields = payload
                    .get("fields")
                    .and_then(Value::as_array)
                    .map(|fields| {
                        fields
                            .iter()
                            .map(|field| ScreenField {
                                key: body_text(field, "key"),
                                label: body_text(field, "label"),
                                value: body_text(field, "value"),
                                toggle: body_flag(field, "toggle"),
                                options: field
                                    .get("options")
                                    .and_then(Value::as_array)
                                    .map(|options| {
                                        options
                                            .iter()
                                            .map(|option| match option {
                                                Value::String(text) => text.clone(),
                                                other => other.to_string(),
                                            })
                                            .collect()
                                    })
                                    .unwrap_or_default(),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
            }
            ScreenKind::Progress => {
                view.body = body_text(payload, "label");
                view.done = body_count(payload, "done");
                view.total = body_count(payload, "total");
            }
            ScreenKind::Confirm => {
                view.body = body_text(payload, "message");
            }
            ScreenKind::Split => {
                let left = payload.get("left").cloned().unwrap_or(Value::Null);
                let right = payload.get("right").cloned().unwrap_or(Value::Null);
                view.body_left = body_text(&left, "markdown");
                view.body_right = body_text(&right, "markdown");
                view.actions = payload
                    .get("actions")
                    .and_then(Value::as_array)
                    .map(|actions| {
                        actions
                            .iter()
                            .map(|action| ScreenRow {
                                id: body_text(action, "id"),
                                cells: vec![body_text(action, "label")],
                                detail: body_text(action, "detail"),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
            }
        }
        view
    }

    /// Whether this modal has an answer to send.
    pub fn answerable(&self) -> bool {
        !self.id.is_empty()
    }

    /// The answer for `Enter`: the selection, the form values, or `true`.
    pub fn accept(&self) -> Value {
        match self.kind {
            ScreenKind::List | ScreenKind::Table => serde_json::json!({
                "id": self.rows.get(self.selected).map(|row| row.id.clone()).unwrap_or_default(),
            }),
            ScreenKind::Detail => serde_json::json!({
                "action": self.actions.get(self.selected).map(|row| row.id.clone()),
            }),
            ScreenKind::Form => {
                let mut values = serde_json::Map::new();
                for field in &self.fields {
                    // A toggle answers true/false, so the server reads a decision
                    // rather than having to interpret a string it never asked for.
                    let value = if field.toggle {
                        Value::Bool(field.value != "off")
                    } else {
                        Value::String(field.value.clone())
                    };
                    values.insert(field.key.clone(), value);
                }
                Value::Object(values)
            }
            ScreenKind::Confirm => Value::Bool(true),
            ScreenKind::Split => serde_json::json!({
                "action": self.actions.get(self.selected).map(|row| row.id.clone()),
            }),
            ScreenKind::Markdown | ScreenKind::Progress => Value::Null,
        }
    }

    /// The answer for `Esc`: a refusal for a confirm, else `null`.
    pub fn cancel(&self) -> Value {
        match self.kind {
            ScreenKind::Confirm => Value::Bool(false),
            _ => Value::Null,
        }
    }
}

/// The `/` autocomplete overlay.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Palette {
    pub open: bool,
    pub query: String,
    /// Indices into `AppState::commands` that match the query, in table order.
    pub matches: Vec<usize>,
    pub selected: usize,
}

impl Palette {
    /// Recompute the match list for the current query.
    ///
    /// Args:
    ///     commands: The catalogue the server sent.
    pub fn refilter(&mut self, commands: &[CommandHelp]) {
        let needle = self.query.to_lowercase();
        self.matches = commands
            .iter()
            .enumerate()
            .filter(|(_, command)| {
                needle.is_empty()
                    || command.name.to_lowercase().contains(&needle)
                    || command
                        .aliases
                        .iter()
                        .any(|alias| alias.to_lowercase().contains(&needle))
            })
            .map(|(index, _)| index)
            .collect();
        if self.selected >= self.matches.len() {
            self.selected = self.matches.len().saturating_sub(1);
        }
    }

    /// Move the highlight by one row, clamped to the list.
    ///
    /// Args:
    ///     delta: \`-1\` up, \`1\` down.
    pub fn step(&mut self, delta: isize) {
        if self.matches.is_empty() {
            return;
        }
        let last = self.matches.len() - 1;
        let next = self.selected as isize + delta;
        self.selected = next.clamp(0, last as isize) as usize;
    }

    /// The highlighted command's index into `AppState::commands`.
    pub fn current(&self) -> Option<usize> {
        self.matches.get(self.selected).copied()
    }
}

/// One live (or finished) council run, folded from `CouncilUpdate` events.
#[derive(Debug, Clone, Default)]
pub struct CouncilState {
    pub prompt: String,
    pub phase: String,
    pub agents: Vec<CouncilAgent>,
    pub done: HashSet<(String, String)>,
    pub failed: HashMap<String, (String, String)>,
    pub proposals: u64,
    pub critiques: u64,
    pub ballots: u64,
    pub entropy: Option<f64>,
    pub outcome: String,
    pub message: String,
}

impl CouncilState {
    /// Fold one planner event into the card.
    pub fn apply(&mut self, update: &CouncilUpdate) {
        if update.event == "council.started" {
            self.done.clear();
            self.failed.clear();
            self.outcome.clear();
            self.message.clear();
            self.proposals = 0;
            self.critiques = 0;
            self.ballots = 0;
            self.entropy = None;
        }
        if !update.prompt.is_empty() {
            self.prompt = update.prompt.clone();
        }
        if !update.agents.is_empty() {
            self.agents = update.agents.clone();
        }
        if !update.phase.is_empty() {
            self.phase = update.phase.clone();
        }
        match update.event.as_str() {
            "agent.completed" => {
                self.done
                    .insert((update.agent_id.clone(), update.phase.clone()));
            }
            "proposal.critiqued" => {
                self.done
                    .insert((update.agent_id.clone(), "critiquing".to_string()));
            }
            "agent.failed" => {
                self.failed.insert(
                    update.agent_id.clone(),
                    (update.phase.clone(), update.reason.clone()),
                );
            }
            "plans.selected" => {
                self.outcome = "done".to_string();
            }
            "council.failed" => {
                self.outcome = "failed".to_string();
                self.message = update.reason.clone();
            }
            _ => {}
        }
        self.proposals = update.proposals;
        self.critiques = update.critiques;
        self.ballots = update.ballots;
        if update.entropy.is_some() {
            self.entropy = update.entropy;
        }
    }

    /// Whether a phase is finished for every seat.
    pub fn elapsed_phases(&self) -> Vec<String> {
        let mut phases = Vec::new();
        for phase in ["brainstorming", "critiquing", "voting", "judging"] {
            if self.ballots > 0 && phase == "voting" {
                phases.push(phase.to_string());
            }
            if self.phase == phase {
                break;
            }
            if phase != "voting" && !phases.iter().any(|seen| seen == phase) {
                phases.push(phase.to_string());
            }
        }
        phases
    }
}

/// One session's transcript and view state.
///
/// A tab switch must not mix conversations: the client keeps what each session
/// painted and restores it when that tab becomes active again. `Status` is
/// snapshotted too, because model, cwd and token counters belong to the session,
/// while the palette and connection stay global.
#[derive(Debug, Clone, Default)]
pub struct SessionSnapshot {
    pub entries: Vec<Entry>,
    pub streaming: String,
    pub reasoning: String,
    pub status: Status,
    pub todos: Vec<Todo>,
    pub composer: String,
    pub scroll: u16,
    pub turn_running: bool,
    pub last_error: Option<String>,
    pub council: Option<CouncilState>,
}

/// Everything the UI draws, plus the input state behind the composer.
#[derive(Debug, Clone, Default)]
pub struct AppState {
    pub entries: Vec<Entry>,
    pub streaming: String,
    pub reasoning: String,
    pub status: Status,
    pub composer: String,
    pub history: Vec<String>,
    pub history_cursor: Option<usize>,
    pub modal: Option<Modal>,
    pub todos: Vec<Todo>,
    pub sessions: Vec<SessionInfo>,
    pub active_session: Option<String>,
    /// The live council card, if one is running or just finished.
    pub council: Option<CouncilState>,
    /// Transcripts kept for sessions this client has already viewed.
    pub session_views: HashMap<String, SessionSnapshot>,
    /// The command catalogue, as last sent by the server.
    pub commands: Vec<CommandHelp>,
    /// The `/` autocomplete overlay.
    pub palette: Palette,
    /// The modal the server asked for, if any.
    pub screen: Option<ScreenView>,
    /// The palette the last @@ui_theme@@ frame carried, until the caller takes it.
    pub pending_theme: Option<ThemePalette>,
    pub scroll: u16,
    pub turn_running: bool,
    pub quit: bool,
    pub next_prompt: u64,
    pub last_error: Option<String>,
    /// Frame counter for lightweight animation (council spinner and cursor).
    pub tick: u64,
    /// The `/nova` sprite, once the server has shipped its sheet.
    pub avatar: Option<Avatar>,
    /// The shell command whose output is being streamed, if any.
    pub terminal: Option<Terminal>,
    /// The home banner, once the server has named the saved layout.
    pub home: Option<Home>,
    /// ctrl+o: expand every tool card for this session until pressed again.
    pub expand_all: bool,
}

impl AppState {
    /// Build an empty state.
    pub fn new() -> Self {
        Self::default()
    }

    /// Capture everything that belongs to the active session.
    fn snapshot(&self) -> SessionSnapshot {
        SessionSnapshot {
            entries: self.entries.clone(),
            streaming: self.streaming.clone(),
            reasoning: self.reasoning.clone(),
            status: self.status.clone(),
            todos: self.todos.clone(),
            composer: self.composer.clone(),
            scroll: self.scroll,
            turn_running: self.turn_running,
            last_error: self.last_error.clone(),
            council: self.council.clone(),
        }
    }

    /// Stash the active session before another one paints over it.
    fn stash_active(&mut self) {
        if let Some(id) = self.active_session.clone() {
            self.session_views.insert(id, self.snapshot());
        }
    }

    /// Switch to *id*, restoring its transcript or starting that tab empty.
    ///
    /// The palette and connection state stay global; a pending approval or
    /// server screen is cleared because it belongs to the session we left.
    pub fn activate(&mut self, id: Option<&str>) {
        self.stash_active();
        self.active_session = id.map(str::to_string);
        self.modal = None;
        self.screen = None;
        let Some(id) = id else {
            return;
        };
        if let Some(snapshot) = self.session_views.remove(id) {
            let theme = self.status.theme.clone();
            let connected = self.status.connected;
            self.entries = snapshot.entries;
            self.streaming = snapshot.streaming;
            self.reasoning = snapshot.reasoning;
            self.status = snapshot.status;
            self.status.theme = theme;
            self.status.connected = connected;
            self.todos = snapshot.todos;
            self.composer = snapshot.composer;
            self.scroll = snapshot.scroll;
            self.turn_running = snapshot.turn_running;
            self.last_error = snapshot.last_error;
            self.council = snapshot.council;
        } else {
            self.entries.clear();
            self.streaming.clear();
            self.reasoning.clear();
            self.todos.clear();
            self.composer.clear();
            self.scroll = 0;
            self.turn_running = false;
            self.last_error = None;
            self.council = None;
        }
    }

    /// Allocate the id the next prompt or command will use.
    pub fn next_request_id(&mut self) -> String {
        self.next_prompt += 1;
        format!("p{}", self.next_prompt)
    }

    /// Push one entry with an explicit agent label.
    pub fn push(&mut self, kind: BlockKind, text: impl Into<String>, agent: &str) {
        self.entries.push(Entry {
            kind,
            text: text.into(),
            agent: agent.to_string(),
        });
    }

    /// Move to the previous composer history entry.
    pub fn history_previous(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let cursor = match self.history_cursor {
            Some(index) if index > 0 => index - 1,
            Some(_) => 0,
            None => self.history.len() - 1,
        };
        self.history_cursor = Some(cursor);
        self.composer = self.history[cursor].clone();
    }

    /// Move to the next composer history entry, clearing at the end.
    pub fn history_next(&mut self) {
        match self.history_cursor {
            Some(index) if index + 1 < self.history.len() => {
                self.history_cursor = Some(index + 1);
                self.composer = self.history[index + 1].clone();
            }
            Some(_) => {
                self.history_cursor = None;
                self.composer.clear();
            }
            None => {}
        }
    }

    /// Record submitted text in the composer history.
    pub fn remember(&mut self, text: &str) {
        if !text.trim().is_empty() {
            self.history.push(text.to_string());
        }
        self.history_cursor = None;
    }

    /// The transcript text the tests assert on: every prose line, in order.
    pub fn prose(&self) -> Vec<String> {
        self.entries
            .iter()
            .filter(|entry| {
                matches!(
                    entry.kind,
                    BlockKind::User
                        | BlockKind::Assistant
                        | BlockKind::Error
                        | BlockKind::Context { .. }
                )
            })
            .map(|entry| entry.text.clone())
            .collect()
    }

    /// Find a tool card by call id and return a mutable handle to it.
    pub fn tool_mut(&mut self, call_id: Option<&str>) -> Option<&mut ToolCard> {
        let wanted = call_id?;
        self.entries
            .iter_mut()
            .find_map(|entry| match &mut entry.kind {
                BlockKind::Tool(card) if card.call_id.as_deref() == Some(wanted) => Some(card),
                _ => None,
            })
    }

    /// The last tool card in the transcript, useful when a result has no id.
    pub fn last_tool_mut(&mut self) -> Option<&mut ToolCard> {
        self.entries
            .iter_mut()
            .rev()
            .find_map(|entry| match &mut entry.kind {
                BlockKind::Tool(card) => Some(card),
                _ => None,
            })
    }
}

fn entry(kind: BlockKind, text: impl Into<String>) -> Entry {
    Entry {
        kind,
        text: text.into(),
        agent: "Nova".to_string(),
    }
}

/// The text a copy action gathers: the last Nova reply, or the whole transcript.
fn copy_source(state: &AppState, all: bool) -> String {
    if all {
        let mut parts = Vec::new();
        for entry in &state.entries {
            let who = match entry.kind {
                BlockKind::User => "You",
                BlockKind::Assistant => "Nova",
                _ => continue,
            };
            parts.push(format!(
                "## {who}
{}",
                entry.text
            ));
        }
        return parts.join(
            "

",
        );
    }
    state
        .entries
        .iter()
        .rev()
        .find_map(|entry| match entry.kind {
            BlockKind::Assistant => Some(entry.text.clone()),
            _ => None,
        })
        .unwrap_or_default()
}

/// Apply one inbound frame, returning the side effects the caller must perform.
pub fn apply(state: &mut AppState, frame: &Inbound) -> Vec<Effect> {
    let mut effects = Vec::new();
    match frame {
        Inbound::Hello { .. } => {}
        Inbound::Ready {
            session_id,
            cwd,
            model,
            assistant_id,
            home,
        } => {
            state.home = home
                .as_ref()
                .map(|config| Home::new(&config.style, config.animation, &config.version));
            state.status.session_id = session_id.clone();
            state.status.cwd = cwd.clone();
            state.status.model = model.clone();
            state.status.assistant_id = assistant_id.clone();
            state.status.connected = true;
            if state.active_session.is_none() {
                state.active_session = session_id.clone();
            }
        }
        Inbound::Event(event) => apply_event(state, event),
        Inbound::Interrupt { id, kind, payload } => {
            let (headline, detail) = describe_interrupt(kind, payload);
            state.modal = Some(Modal::Approval {
                id: id.clone(),
                kind: kind.clone(),
                headline,
                detail,
            });
        }
        Inbound::TurnDone { ok, .. } => {
            state.turn_running = false;
            state.status.activity = None;
            if *ok {
                flush_streaming(state);
            } else {
                state.streaming.clear();
                state.reasoning.clear();
            }
        }
        Inbound::Error { message, .. } => {
            state.turn_running = false;
            state.last_error = Some(message.clone());
            state.entries.push(entry(BlockKind::Error, message.clone()));
        }
        Inbound::Pty { id, command, text } => {
            let terminal = state.terminal.get_or_insert_with(Terminal::default);
            terminal.id.clone_from(id);
            terminal.command.clone_from(command);
            terminal.push(text);
        }
        Inbound::PtyExit { id, code } => {
            if let Some(terminal) = state.terminal.as_mut() {
                if terminal.id == *id {
                    terminal.exit = Some(*code);
                    if !terminal.partial.is_empty() {
                        let tail = std::mem::take(&mut terminal.partial);
                        terminal.lines.push(tail);
                    }
                }
            }
        }
        Inbound::Notice { level, message } => {
            state.entries.push(entry(
                BlockKind::Notice {
                    level: level.clone(),
                    text: message.clone(),
                },
                message.clone(),
            ));
        }
        Inbound::CommandResult {
            ok,
            message,
            blocks,
            action,
            screen,
            attach,
            create,
            resume,
            avatar,
            close,
            // `prompt` needs no client action: the server already started that
            // turn, and echoing it here would duplicate the transcript.
            ..
        } => {
            // `clear` drops what the transcript held, then this result draws into
            // the empty transcript: a rewind rewrites the conversation it replaced.
            // A clear that replays nothing (`/clear`) leaves no line behind, which
            // is what emptying the transcript means.
            let cleared = action.as_deref() == Some("clear");
            if cleared {
                state.entries.clear();
                effects.push(Effect::ClearTranscript);
            }
            if !blocks.is_empty() || (!message.is_empty() && !cleared) {
                state.entries.push(entry(
                    BlockKind::Command {
                        ok: *ok,
                        message: message.clone(),
                        blocks: blocks.clone(),
                    },
                    "",
                ));
            }
            match action.as_deref() {
                Some("clear") => {}
                Some("quit") => {
                    state.quit = true;
                    effects.push(Effect::Quit);
                }
                Some("cancel") => {
                    // `/stop` cannot cancel from the server side: the turn belongs
                    // to the host, so the client sends the same frame Esc does.
                    effects.push(Effect::Send(crate::frames::cancel()));
                }
                Some("copy") | Some("copy-all") => {
                    // The clipboard belongs to the terminal, so the server names
                    // what to copy and the client gathers the text.
                    let all = action.as_deref() == Some("copy-all");
                    let text = copy_source(state, all);
                    if !text.is_empty() {
                        effects.push(Effect::Copy { text, all });
                    }
                }
                Some("avatar") => {
                    // `/nova`: the sheet rides the frame that turns it on, and a
                    // command without one is the instruction to hide it. A sheet
                    // the client cannot read is refused rather than half drawn.
                    match avatar.as_ref().map(Avatar::decode) {
                        Some(Some(sprite)) => state.avatar = Some(sprite),
                        Some(None) => state.entries.push(entry(
                            BlockKind::Notice {
                                level: "warning".to_string(),
                                text: "This terminal cannot draw the avatar sheet.".to_string(),
                            },
                            "",
                        )),
                        None => state.avatar = None,
                    }
                }
                _ => {}
            }
            if let Some(session_id) = attach {
                // Only the connection's transport can move its sink, so the
                // server names the target and the client sends `attach`.
                if !session_id.is_empty() {
                    effects.push(Effect::Send(crate::frames::attach(session_id)));
                }
            }
            if let Some(name) = create {
                // Spawning belongs to the connection: the command names the tab,
                // the client asks the server to start it. `/resume` also names the
                // saved session, so the child continues it instead of starting new.
                effects.push(Effect::Send(crate::frames::create_session(
                    name,
                    resume.as_deref(),
                )));
            }
            if let Some(session_id) = close {
                let target = if session_id.is_empty() {
                    state.active_session.clone().unwrap_or_default()
                } else {
                    session_id.clone()
                };
                if !target.is_empty() {
                    effects.push(Effect::Send(crate::frames::close_session(&target)));
                }
            }
            if let Some(spec) = screen {
                // A command that could not present its own modal attaches the
                // spec to its result; show it, but there is nothing to answer.
                let kind = body_text(spec, "kind");
                let title = body_text(spec, "title");
                let payload = spec.get("payload").cloned().unwrap_or(Value::Null);
                if let Some(kind) = ScreenKind::parse(&kind) {
                    state.screen = Some(ScreenView::decode("", kind, &title, &payload));
                }
            }
            state.turn_running = false;
        }
        Inbound::UiTheme(palette) => {
            // The theme is a drawing fact, not transcript content: park it for the
            // render loop and change nothing the user can scroll back to.
            state.pending_theme = Some((*palette).clone());
        }
        Inbound::Sessions { sessions, active } => {
            let next = active.clone().or_else(|| state.active_session.clone());
            if next != state.active_session {
                state.activate(next.as_deref());
            }
            state.sessions = sessions.clone();
            if let Some(id) = state.active_session.as_deref() {
                if let Some(row) = sessions.iter().find(|row| row.id == id) {
                    state.status.session_id = Some(row.id.clone());
                    if row.cwd.is_some() {
                        state.status.cwd = row.cwd.clone();
                    }
                    if row.model.is_some() {
                        state.status.model = row.model.clone();
                    }
                }
            }
        }
        Inbound::Catalogue { commands } => {
            state.commands = commands.clone();
            state.palette.refilter(&state.commands);
        }
        Inbound::Screen {
            id,
            kind,
            title,
            payload,
        } => match ScreenKind::parse(kind) {
            Some(kind) => {
                let heading = title.as_deref().unwrap_or_default();
                state.screen = Some(ScreenView::decode(id, kind, heading, payload));
            }
            None => {
                // An unknown kind is a version skew, and the server is waiting.
                // Answer the documented safe default instead of timing out.
                state.entries.push(entry(
                    BlockKind::Notice {
                        level: "warning".to_string(),
                        text: format!("unknown screen kind: {kind}"),
                    },
                    "",
                ));
                effects.push(Effect::Answer {
                    screen_id: id.clone(),
                    value: Value::Null,
                });
            }
        },
        Inbound::Unknown { kind } => {
            // Version skew: a newer server may send frames this build predates.
            state.entries.push(entry(
                BlockKind::Notice {
                    level: "info".to_string(),
                    text: format!("skipped unknown frame: {kind}"),
                },
                "",
            ));
        }
    }
    effects
}

fn apply_event(state: &mut AppState, event: &UiEvent) {
    match event {
        UiEvent::TextDelta(text) => state.streaming.push_str(text),
        UiEvent::ReasoningDelta(text) => state.reasoning.push_str(text),
        UiEvent::TextDiscard => {
            state.streaming.clear();
        }
        UiEvent::AssistantMessage {
            text,
            agent_name,
            is_subagent,
        } => {
            state.streaming.clear();
            let mut new_entry = entry(BlockKind::Assistant, text.clone());
            if !agent_name.is_empty() {
                new_entry.agent = agent_name.clone();
            }
            if *is_subagent {
                new_entry.kind = BlockKind::Context {
                    icon: "•".to_string(),
                };
            }
            state.entries.push(new_entry);
            state.turn_running = true;
        }
        UiEvent::ToolCall {
            name,
            display_str,
            icon,
            call_id,
            ..
        } => {
            state.entries.push(entry(
                BlockKind::Tool(ToolCard {
                    call_id: call_id.clone(),
                    name: name.clone(),
                    display: display_str.clone(),
                    icon: icon.clone(),
                    ..ToolCard::default()
                }),
                display_str.clone(),
            ));
            state.turn_running = true;
        }
        UiEvent::ToolResult {
            preview,
            full_output,
            is_error,
            call_id,
        } => {
            let card = match state.tool_mut(call_id.as_deref()) {
                Some(card) => Some(card),
                None => state.last_tool_mut(),
            };
            if let Some(card) = card {
                card.preview = preview.clone();
                if !full_output.is_empty() {
                    card.full_output = full_output.clone();
                }
                card.is_error = *is_error;
                card.finished = true;
            } else {
                state.entries.push(entry(
                    BlockKind::Tool(ToolCard {
                        call_id: call_id.clone(),
                        preview: preview.clone(),
                        full_output: full_output.clone(),
                        is_error: *is_error,
                        finished: true,
                        ..ToolCard::default()
                    }),
                    preview.clone(),
                ));
            }
        }
        UiEvent::FileOp {
            call_id,
            operation,
            path,
            full_output,
            ..
        } => {
            if let Some(card) = state.tool_mut(call_id.as_deref()) {
                if !full_output.is_empty() {
                    card.full_output = full_output.clone();
                }
            }
            let label = path.clone().unwrap_or_else(|| "(no path)".to_string());
            state.entries.push(entry(
                BlockKind::FileOp {
                    operation: operation.clone(),
                    path: label.clone(),
                    call_id: call_id.clone(),
                },
                label,
            ));
        }
        UiEvent::TodoUpdate(todos) => state.todos = todos.clone(),
        UiEvent::StatusUpdate(message) => {
            state.status.activity = message.clone();
        }
        UiEvent::UsageUpdate {
            input_tokens,
            output_tokens,
            session_tokens,
            context_reported,
        } => {
            state.status.input_tokens = *input_tokens;
            state.status.output_tokens = *output_tokens;
            state.status.session_tokens = *session_tokens;
            if *context_reported && state.status.context_tokens == 0 {
                state.status.context_tokens = *input_tokens;
            }
        }
        UiEvent::ContextUsage(input_tokens) => state.status.context_tokens = *input_tokens,
        UiEvent::ErrorOutput(text) => state.entries.push(entry(BlockKind::Error, text.clone())),
        UiEvent::Error {
            message,
            is_provider_notice,
        } => {
            state.last_error = Some(message.clone());
            let kind = if *is_provider_notice {
                BlockKind::Notice {
                    level: "warning".to_string(),
                    text: message.clone(),
                }
            } else {
                BlockKind::Error
            };
            state.entries.push(entry(kind, message.clone()));
        }
        UiEvent::Done => {
            state.turn_running = false;
            state.status.activity = None;
            flush_streaming(state);
        }
        UiEvent::Cancelled => {
            state.turn_running = false;
            state.streaming.clear();
            state.entries.push(entry(
                BlockKind::Notice {
                    level: "warning".to_string(),
                    text: "cancelled".to_string(),
                },
                "cancelled",
            ));
        }
        UiEvent::ContextMessage { message, icon } => {
            state.entries.push(entry(
                BlockKind::Context { icon: icon.clone() },
                message.clone(),
            ));
        }
        UiEvent::Council(update) => {
            state
                .council
                .get_or_insert_with(CouncilState::default)
                .apply(update);
        }
        UiEvent::Other(name) => {
            state.entries.push(entry(
                BlockKind::Notice {
                    level: "info".to_string(),
                    text: format!("skipped unknown event: {name}"),
                },
                "",
            ));
        }
    }
}

/// Commit the streamed buffer as an assistant entry.
fn flush_streaming(state: &mut AppState) {
    if !state.streaming.trim().is_empty() {
        let text = std::mem::take(&mut state.streaming);
        state.entries.push(entry(BlockKind::Assistant, text));
    } else {
        state.streaming.clear();
    }
    state.reasoning.clear();
}

/// Turn an interrupt payload into a headline and printable detail lines.
fn describe_interrupt(kind: &str, payload: &Value) -> (String, Vec<String>) {
    let mut detail = Vec::new();
    match kind {
        "tool" => {
            if let Some(requests) = payload.get("action_requests").and_then(Value::as_array) {
                for request in requests {
                    let name = request
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("tool");
                    let description = request
                        .get("description")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    detail.push(format!("{name}: {description}"));
                }
            }
        }
        "plan" => {
            if let Some(plan) = payload.get("plan").and_then(Value::as_str) {
                detail.extend(plan.lines().map(str::to_string));
            }
        }
        _ => {
            if let Some(question) = payload.get("question").and_then(Value::as_str) {
                detail.push(question.to_string());
            }
        }
    }
    let headline = match kind {
        "tool" => "Approve tool call?".to_string(),
        "plan" => "Approve this plan?".to_string(),
        "question" => "Nova has a question".to_string(),
        other => format!("{other} request"),
    };
    (headline, detail)
}
