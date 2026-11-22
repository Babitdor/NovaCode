//! The prompt composer: the input line, its history and the working indicator.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use crate::state::AppState;
use crate::theme::Theme;

/// Draw the composer into *area*.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let title = if state.turn_running {
        " working — Esc cancels "
    } else {
        " prompt "
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(if state.turn_running {
            theme.warning()
        } else {
            theme.border()
        })
        .title(Span::styled(title, theme.muted()));

    let text = line(state, theme);
    frame.render_widget(Paragraph::new(text).block(block), area);
}

/// The composer's content as one line, cursor included.
pub fn line(state: &AppState, theme: &Theme) -> Line<'static> {
    if state.composer.is_empty() {
        return Line::from(Span::styled(
            "▏ type a prompt, / for commands, Alt+Enter for a newline",
            theme.muted(),
        ));
    }
    Line::from(vec![
        Span::styled(state.composer.clone(), theme.body()),
        Span::styled("▏", theme.accent_style()),
    ])
}
