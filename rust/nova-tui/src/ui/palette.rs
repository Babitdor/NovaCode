//! The @@/@@ autocomplete overlay: the server's command catalogue, filtered.
//!
//! The list is not hard-coded here - @@catalogue@@ frames carry it, so the client
//! offers exactly what the server can answer, including the aliases and the
//! one-line help that @@/help@@ prints. Rows the server cannot serve yet are
//! still listed, marked, because hiding them would make a ported command look
//! like a missing one.

use crate::frames;
use crate::state::{AppState, Effect};
use crate::theme::Theme;
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;

/// How many rows the overlay shows at once.
const VISIBLE_ROWS: usize = 12;

/// Handle one key while the palette is open.
///
/// Returns:
///     @@Some(Effect::Send)@@ when a command was chosen, else @@None@@; the caller
///     keeps the palette open for filtering keys.
pub fn handle_key(state: &mut AppState, key: &KeyEvent) -> Option<Effect> {
    match key.code {
        KeyCode::Esc => {
            state.palette.open = false;
            state.palette.query.clear();
            state.palette.matches.clear();
            state.palette.selected = 0;
            None
        }
        KeyCode::Up => {
            state.palette.step(-1);
            None
        }
        KeyCode::Down => {
            state.palette.step(1);
            None
        }
        KeyCode::Backspace => {
            if state.palette.query.pop().is_none() {
                state.palette.open = false;
                state.palette.matches.clear();
            } else {
                let commands = state.commands.clone();
                state.palette.refilter(&commands);
            }
            None
        }
        KeyCode::Tab => {
            complete(state);
            None
        }
        KeyCode::Enter => run(state),
        KeyCode::Char(ch) => {
            state.palette.query.push(ch);
            let commands = state.commands.clone();
            state.palette.refilter(&commands);
            None
        }
        _ => None,
    }
}

/// Put the highlighted command into the composer, ready for arguments.
fn complete(state: &mut AppState) {
    let Some(index) = state.palette.current() else {
        return;
    };
    let command = &state.commands[index];
    let mut text = format!("/{}", command.name);
    if command.wants_text {
        text.push(' ');
    }
    state.composer = text;
    state.palette.open = false;
    state.palette.query.clear();
    state.palette.matches.clear();
}

/// Submit the highlighted command, or complete it when it wants arguments.
fn run(state: &mut AppState) -> Option<Effect> {
    let index = state.palette.current()?;
    let command = state.commands[index].clone();
    state.palette.open = false;
    state.palette.query.clear();
    state.palette.matches.clear();
    if command.wants_text {
        // Running it empty would hide the arguments it needs; hand it over.
        state.composer = format!("/{} ", command.name);
        return None;
    }
    state.composer.clear();
    let id = state.next_request_id();
    state.turn_running = true;
    Some(Effect::Send(frames::command(&id, &command.name, None)))
}

/// Draw the palette, if open, over the client area.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    if !state.palette.open {
        return;
    }
    let height = (VISIBLE_ROWS as u16 + 4).min(area.height);
    let popup = Rect {
        x: area.x,
        y: area.y + area.height.saturating_sub(height + 3),
        width: area.width,
        height,
    };
    frame.render_widget(Clear, popup);
    let mut lines: Vec<Line<'static>> = Vec::new();
    let matches = &state.palette.matches;
    let first = state.palette.selected.saturating_sub(VISIBLE_ROWS - 1);
    for (offset, index) in matches.iter().enumerate().skip(first).take(VISIBLE_ROWS) {
        let command = &state.commands[*index];
        let marker = if offset == state.palette.selected {
            "▸ "
        } else {
            "  "
        };
        let suffix = if command.served {
            ""
        } else {
            "  (not served yet)"
        };
        let mut alias = String::new();
        if !command.aliases.is_empty() {
            alias = format!("  [{}]", command.aliases.join(", "));
        }
        let text = format!("{marker}/{}  {}{alias}{suffix}", command.name, command.help);
        lines.push(Line::from(Span::styled(
            text,
            if offset == state.palette.selected {
                theme.heading()
            } else {
                theme.body()
            },
        )));
    }
    if matches.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("no command matches \"/{}\"", state.palette.query),
            theme.muted(),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        format!(
            "/{}  ·  ↑↓ move · Tab complete · Enter run · Esc close",
            state.palette.query
        ),
        theme.muted(),
    )));
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.accent_border())
        .title(Span::styled(" commands ", theme.muted()));
    frame.render_widget(Paragraph::new(lines).block(block), popup);
}
