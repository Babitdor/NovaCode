//! Tool calls, short result previews, and attached file operations.

use ratatui::style::Style;
use ratatui::text::{Line, Span};

use crate::state::ToolCard;
use crate::theme::Theme;
use crate::ui::{activity, diff};

const PREVIEW_HEAD: usize = 2;

pub fn lines(
    card: &ToolCard,
    theme: &Theme,
    expand_all: bool,
    running: bool,
    tick: u64,
    out: &mut Vec<Line<'static>>,
) {
    let (marker, status) = if card.is_error {
        ("✗", theme.error())
    } else if card.finished {
        ("✓", theme.success())
    } else if running {
        (activity::spinner(tick), Style::default().fg(theme.activity))
    } else {
        ("–", theme.muted())
    };
    let label = if !card.display.is_empty() {
        card.display.as_str()
    } else if !card.name.is_empty() {
        card.name.as_str()
    } else {
        "Tool result"
    };
    let expanded = card.expanded.unwrap_or(expand_all);
    let mut heading = vec![
        Span::styled(format!("  {marker} "), status),
        Span::styled(label.to_owned(), theme.body()),
    ];
    if !card.full_output.is_empty() || card.files.iter().any(|file| !file.diff.is_empty()) {
        heading.push(Span::styled(
            if expanded { "  ▾" } else { "  ▸" },
            theme.muted(),
        ));
    }
    out.push(Line::from(heading));

    let preview = card.preview.trim_start_matches(['✓', '✗', ' ']).trim();
    let output: Vec<_> = card.full_output.lines().collect();
    let tail = output.iter().rposition(|line| !line.trim().is_empty());
    if !preview.is_empty() && !tail.is_some_and(|last| output[last].trim() == preview) {
        for line in preview.lines() {
            out.push(Line::from(Span::styled(
                format!("    ↳ {line}"),
                if card.is_error {
                    theme.error()
                } else {
                    theme.muted()
                },
            )));
        }
    }
    for change in &card.files {
        diff::file(change, expanded, theme, out);
    }
    let Some(tail) = tail else { return };
    for (index, line) in output.into_iter().take(tail + 1).enumerate() {
        if expanded || index < PREVIEW_HEAD || index == tail {
            out.push(Line::from(Span::styled(
                format!("    {line}"),
                theme.code(),
            )));
        } else if index == PREVIEW_HEAD {
            let hidden = tail - PREVIEW_HEAD;
            let label = if hidden == 1 { "line" } else { "lines" };
            out.push(Line::from(Span::styled(
                format!("    … {hidden} hidden {label} · Ctrl+O"),
                theme.muted(),
            )));
        }
    }
}
