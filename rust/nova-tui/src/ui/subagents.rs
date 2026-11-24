//! The live subagents and Ralph dock below the transcript.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::state::AppState;
use crate::theme::Theme;
use crate::ui::statusbar::context_summary;

const VISIBLE_TASKS: usize = 4;

/// Reserve only as much space as the current work needs.
pub fn height(state: &AppState) -> u16 {
    let workers = &state.workers;
    let tasks = if workers.expanded {
        2 * workers.tasks.len().min(VISIBLE_TASKS)
    } else {
        0
    };
    u16::from(state.ralph.is_some()) + u16::from(!workers.tasks.is_empty()) + tasks as u16
}

/// Draw progress and context usage without adding messages to the transcript.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let mut lines = Vec::new();
    if let Some((label, active)) = &state.ralph {
        let window = state
            .status
            .composer
            .as_ref()
            .map_or(0, |mode| mode.context_window);
        lines.push(Line::from(vec![
            Span::styled("Ralph · ", theme.heading()),
            Span::styled(label.clone(), theme.body()),
            Span::styled(
                format!(
                    " · {}",
                    context_summary(state.status.context_tokens, window)
                ),
                theme.muted(),
            ),
            Span::styled(
                if *active {
                    " · Alt+P pause · Esc stop"
                } else {
                    ""
                },
                theme.muted(),
            ),
        ]));
    }
    let workers = &state.workers;
    if !workers.tasks.is_empty() {
        let running = workers
            .tasks
            .iter()
            .filter(|task| task.status == "running")
            .count();
        let marker = if workers.expanded { "▾" } else { "▸" };
        lines.push(Line::from(vec![
            Span::styled(format!("{marker} Subagents"), theme.heading()),
            Span::styled(
                format!(
                    " · {running} running · {} total · Alt+W",
                    workers.tasks.len()
                ),
                theme.muted(),
            ),
        ]));
        if workers.expanded {
            for task in workers.tasks.iter().rev().take(VISIBLE_TASKS) {
                let (marker, style) = match task.status.as_str() {
                    "running" => ("●", theme.accent_style()),
                    "done" => ("✓", theme.success()),
                    "failed" => ("✗", theme.error()),
                    _ => ("■", theme.muted()),
                };
                let description = task.label.lines().next().unwrap_or("");
                let detail = if task.error.is_empty() {
                    description
                } else {
                    &task.error
                };
                lines.push(Line::from(vec![
                    Span::styled(format!("  {marker} {} · ", task.name), style),
                    Span::styled(detail.to_owned(), theme.body()),
                ]));
                let context = context_summary(task.context_used, task.context_window);
                let preview = workers
                    .previews
                    .get(&task.id)
                    .and_then(|text| text.lines().next())
                    .unwrap_or("");
                lines.push(Line::from(Span::styled(
                    if preview.is_empty() {
                        format!("    {context}")
                    } else {
                        format!("    {context} · {preview}")
                    },
                    theme.muted(),
                )));
            }
        }
    }
    frame.render_widget(Paragraph::new(lines), area);
}
