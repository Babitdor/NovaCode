//! Unified diffs from file-operation records and Markdown diff fences.

use ratatui::text::{Line, Span};

use crate::state::FileChange;
use crate::theme::Theme;

const PREVIEW_CHANGES: usize = 6;

pub fn file(change: &FileChange, expanded: bool, theme: &Theme, out: &mut Vec<Line<'static>>) {
    let mut heading = vec![
        Span::styled(
            if change.failed { "  ✗ " } else { "  ↳ " },
            if change.failed {
                theme.error()
            } else {
                theme.muted()
            },
        ),
        Span::styled(change.path.clone(), theme.body()),
    ];
    if change.added != 0 || change.removed != 0 {
        heading.push(Span::styled(
            format!("  +{}", change.added),
            theme.diff_line(true),
        ));
        heading.push(Span::styled(
            format!(" −{}", change.removed),
            theme.diff_line(false),
        ));
    } else if !change.operation.is_empty() {
        heading.push(Span::styled(
            format!("  {}", change.operation),
            theme.muted(),
        ));
    }
    out.push(Line::from(heading));
    if !change.diff.is_empty() {
        out.extend(lines(&change.diff, expanded, theme));
    }
}

fn line_start(range: &str, prefix: char) -> Option<u64> {
    range.strip_prefix(prefix)?.split(',').next()?.parse().ok()
}

fn line_number(counter: &mut Option<u64>) -> String {
    counter.map_or_else(String::new, |number| {
        *counter = Some(number.saturating_add(1));
        number.to_string()
    })
}

pub fn lines(patch: &str, expanded: bool, theme: &Theme) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    let (mut old, mut new) = (None, None);
    let mut changes = 0;
    for line in patch.lines() {
        if line.starts_with("diff --git ") {
            (old, new) = (None, None);
        } else if let Some(hunk) = line.strip_prefix("@@ ") {
            let mut ranges = hunk.split_whitespace();
            old = ranges.next().and_then(|range| line_start(range, '-'));
            new = ranges.next().and_then(|range| line_start(range, '+'));
            if expanded {
                out.push(Line::from(Span::styled(
                    format!("    {line}"),
                    theme.heading(),
                )));
            }
            continue;
        }
        let (marker, body) = line.split_at(line.floor_char_boundary(1));
        let (before, after, style) = match marker {
            "+" if new.is_some() || !line.starts_with("+++ ") => {
                changes += 1;
                (String::new(), line_number(&mut new), theme.diff_line(true))
            }
            "-" if old.is_some() || !line.starts_with("--- ") => {
                changes += 1;
                (line_number(&mut old), String::new(), theme.diff_line(false))
            }
            " " if old.is_some() || new.is_some() => {
                let before = line_number(&mut old);
                let after = line_number(&mut new);
                if !expanded {
                    continue;
                }
                (before, after, theme.code_panel())
            }
            _ => {
                if expanded {
                    out.push(Line::from(Span::styled(
                        format!("    {line}"),
                        theme.muted(),
                    )));
                }
                continue;
            }
        };
        if expanded || changes <= PREVIEW_CHANGES {
            out.push(Line::from(Span::styled(
                format!("    {before:>4} {after:>4} {marker} {body}"),
                style,
            )));
        }
    }
    if !expanded {
        let hidden = changes.saturating_sub(PREVIEW_CHANGES);
        let label = if hidden == 0 {
            "    Ctrl+O for full diff".to_owned()
        } else {
            let unit = if hidden == 1 { "change" } else { "changes" };
            format!("    … {hidden} more {unit} · Ctrl+O for full diff")
        };
        out.push(Line::from(Span::styled(label, theme.muted())));
    }
    out
}
