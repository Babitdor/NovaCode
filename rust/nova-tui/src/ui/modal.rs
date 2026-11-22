//! Modals: the three human-in-the-loop interrupts, plus the key hints that make
//! them answerable without documentation.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;

use crate::state::{AppState, Modal};
use crate::theme::Theme;

/// A rectangle *percent_x* by *percent_y* of *area*, centred.
pub fn centered(area: Rect, percent_x: u16, percent_y: u16) -> Rect {
    let width = area.width.saturating_mul(percent_x) / 100;
    let height = area.height.saturating_mul(percent_y) / 100;
    Rect {
        x: area.x + (area.width.saturating_sub(width)) / 2,
        y: area.y + (area.height.saturating_sub(height)) / 2,
        width: width.max(20),
        height: height.max(7),
    }
}

/// Draw the active modal, if any, over the whole screen area.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let Some(modal) = &state.modal else {
        return;
    };
    let popup = centered(area, 76, 60);
    frame.render_widget(Clear, popup);
    let mut lines: Vec<Line<'static>> = Vec::new();
    match modal {
        Modal::Approval {
            kind,
            headline,
            detail,
            ..
        } => {
            lines.push(Line::from(Span::styled(headline.clone(), theme.heading())));
            lines.push(Line::from(""));
            for line in detail.iter().take(12) {
                lines.push(Line::from(Span::styled(line.clone(), theme.body())));
            }
            lines.push(Line::from(""));
            let hints = if kind == "question" {
                "Enter sends · Esc closes"
            } else {
                "y approve · n reject · a always allow · Esc closes"
            };
            lines.push(Line::from(Span::styled(hints, theme.muted())));
        }
    }
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.accent_border())
        .title(Span::styled(" approval ", theme.muted()));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        popup,
    );
}
