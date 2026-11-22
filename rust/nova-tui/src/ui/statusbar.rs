//! One line of live facts: model, workspace, context gauge, activity.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

use crate::state::AppState;
use crate::theme::Theme;

/// Draw the status bar into *area*.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let line = line(state, theme, area.width as usize);
    frame.render_widget(Paragraph::new(line), area);
}

/// Build the status line, truncated to *width* so it never wraps.
pub fn line(state: &AppState, theme: &Theme, width: usize) -> Line<'static> {
    let mut spans = vec![
        Span::styled(
            if state.status.connected {
                "● "
            } else {
                "○ "
            },
            theme.success(),
        ),
        Span::styled(state.status.model_label(), theme.body()),
        Span::styled("  ", theme.muted()),
        Span::styled(state.status.cwd_label(), theme.muted()),
    ];
    if state.turn_running {
        spans.push(Span::styled("  working", theme.warning()));
    }
    if !state.status.theme.is_empty() {
        spans.push(Span::styled(
            format!("  {}", state.status.theme),
            theme.muted(),
        ));
    }
    if state.status.context_tokens > 0 {
        spans.push(Span::styled(
            format!("  ctx {}", short_tokens(state.status.context_tokens)),
            theme.muted(),
        ));
    }
    if !state.todos.is_empty() {
        let done = state
            .todos
            .iter()
            .filter(|todo| todo.status == "completed")
            .count();
        spans.push(Span::styled(
            format!("  todos {done}/{}", state.todos.len()),
            theme.muted(),
        ));
    }
    if let Some(error) = &state.last_error {
        spans.push(Span::styled(
            format!("  ! {}", first_line(error)),
            theme.error(),
        ));
    }

    let mut used = 0usize;
    let mut kept = Vec::new();
    for span in spans {
        let width_of = UnicodeWidthStr::width(span.content.as_ref());
        if used + width_of > width {
            break;
        }
        used += width_of;
        kept.push(span);
    }
    Line::from(kept)
}

fn short_tokens(tokens: u64) -> String {
    if tokens >= 1_000_000 {
        format!("{:.1}M", tokens as f64 / 1_000_000.0)
    } else if tokens >= 1_000 {
        format!("{:.0}k", tokens as f64 / 1_000.0)
    } else {
        tokens.to_string()
    }
}

fn first_line(text: &str) -> &str {
    text.lines().next().unwrap_or(text)
}
