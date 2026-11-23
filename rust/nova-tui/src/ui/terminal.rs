//! The terminal pane: a shell command's output, and the keys that go back.
//!
//! The server owns the pseudo-terminal and sends plain text, so this module has
//! two jobs: paint the lines that arrived, and translate a key press into the
//! bytes a terminal program expects. Both are pure functions of state, which is
//! what the replay tests exercise.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::Rect;
use ratatui::text::Line;
use ratatui::widgets::{Block, Borders, Paragraph, Widget, Wrap};
use ratatui::Frame;

use crate::frames;
use crate::state::{AppState, Effect, Terminal};
use crate::theme::Theme;

/// Map a key press to what the child should receive.
///
/// Only the keys that mean something to a line-oriented program are translated.
/// Everything else is dropped rather than guessed at: sending a wrong escape
/// sequence is worse than sending none.
pub fn keys(key: &KeyEvent) -> Option<String> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Esc => None,
        KeyCode::Enter => Some("\r".into()),
        KeyCode::Tab => Some("\t".into()),
        KeyCode::Backspace => Some("\u{7f}".into()),
        KeyCode::Up => Some("\u{1b}[A".into()),
        KeyCode::Down => Some("\u{1b}[B".into()),
        KeyCode::Right => Some("\u{1b}[C".into()),
        KeyCode::Left => Some("\u{1b}[D".into()),
        KeyCode::Char('c') | KeyCode::Char('C') if ctrl => Some("\u{3}".into()),
        KeyCode::Char('d') | KeyCode::Char('D') if ctrl => Some("\u{4}".into()),
        KeyCode::Char('l') | KeyCode::Char('L') if ctrl => Some("\u{c}".into()),
        KeyCode::Char(glyph) if !ctrl => Some(glyph.to_string()),
        _ => None,
    }
}

/// Turn a key press into a frame while a terminal is open.
///
/// Escape closes the pane rather than clearing the composer, because a live
/// child owns the keyboard; a finished child keeps its output readable until it
/// is dismissed.
pub fn handle_key(state: &mut AppState, key: &KeyEvent) -> Option<Effect> {
    let terminal = state.terminal.as_ref()?;
    if key.code == KeyCode::Esc {
        let id = terminal.id.clone();
        state.terminal = None;
        return Some(Effect::Send(frames::pty_close(&id)));
    }
    if !terminal.live() {
        return None;
    }
    let keys = keys(key)?;
    Some(Effect::Send(frames::pty_input(&terminal.id, &keys)))
}

/// Draw the pane over the conversation area.
///
/// The last lines win: the pane shows the tail, the way a terminal does, and the
/// header carries the command and its exit status so a closed pane still says
/// what ran.
pub fn draw(frame: &mut Frame, area: Rect, terminal: &Terminal, theme: &Theme) {
    let status = match terminal.exit {
        Some(0) => "exit 0".to_string(),
        Some(code) => format!("exit {code}"),
        None => "running · Esc closes".to_string(),
    };
    let title = format!(" !!{} ", terminal.command);
    let block = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .title(Line::from(status).right_aligned().style(theme.muted()))
        .border_style(theme.border());
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let lines = terminal.view();
    let rows = usize::from(inner.height);
    let shown: Vec<Line> = lines
        .iter()
        .skip(lines.len().saturating_sub(rows))
        .map(|text| Line::from(text.as_str()))
        .collect();
    Paragraph::new(shown)
        .wrap(Wrap { trim: false })
        .render(inner, frame.buffer_mut());
}

/// A one-row strip used by tests to prove the pane is on screen.
#[must_use]
pub fn header(terminal: &Terminal) -> String {
    match terminal.exit {
        Some(code) => format!("!!{} — exit {code}", terminal.command),
        None => format!("!!{} — running", terminal.command),
    }
}
