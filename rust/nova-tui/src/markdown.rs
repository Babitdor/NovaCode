//! Markdown to ratatui lines.
//!
//! Scope for increment 1 (see `rust/LEDGER.md`): headings, emphasis, inline
//! code, fenced code blocks, lists and links. Syntax highlighting needs
//! `syntect` and belongs to a later wave, so code blocks render unhighlighted.

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

use crate::theme::Theme;

/// Render *markdown* into styled lines.
pub fn render(markdown: &str, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut style = theme.body();
    let mut list_depth = 0usize;
    let mut in_code_block = false;

    let push_line = |lines: &mut Vec<Line<'static>>, spans: &mut Vec<Span<'static>>| {
        lines.push(Line::from(std::mem::take(spans)));
    };

    for event in Parser::new_ext(markdown, Options::ENABLE_STRIKETHROUGH) {
        match event {
            Event::Start(tag) => match tag {
                Tag::Heading { .. } => {
                    style = theme.heading();
                    if !spans.is_empty() {
                        push_line(&mut lines, &mut spans);
                    }
                }
                Tag::Emphasis => style = style.add_modifier(Modifier::ITALIC),
                Tag::Strong => style = style.add_modifier(Modifier::BOLD),
                Tag::CodeBlock(_) => {
                    if !spans.is_empty() {
                        push_line(&mut lines, &mut spans);
                    }
                    in_code_block = true;
                    style = theme.code();
                }
                Tag::List(_) => list_depth += 1,
                Tag::Item => {
                    let indent = "  ".repeat(list_depth.saturating_sub(1));
                    spans.push(Span::styled(format!("{indent}• "), theme.muted()));
                }
                Tag::Link { dest_url, .. } => {
                    spans.push(Span::styled(format!("{dest_url} "), theme.muted()));
                }
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Heading(_) => {
                    push_line(&mut lines, &mut spans);
                    style = theme.body();
                }
                TagEnd::Emphasis | TagEnd::Strong => style = theme.body(),
                TagEnd::CodeBlock => {
                    in_code_block = false;
                    style = theme.body();
                }
                TagEnd::List(_) => list_depth = list_depth.saturating_sub(1),
                TagEnd::Item => push_line(&mut lines, &mut spans),
                TagEnd::Paragraph => push_line(&mut lines, &mut spans),
                _ => {}
            },
            Event::Text(text) => {
                if in_code_block {
                    for line in text.lines() {
                        lines.push(Line::from(Span::styled(line.to_string(), theme.code())));
                    }
                } else {
                    spans.push(Span::styled(text.to_string(), style));
                }
            }
            Event::Code(code) => spans.push(Span::styled(code.to_string(), theme.code())),
            Event::SoftBreak => spans.push(Span::raw(" ")),
            Event::HardBreak => push_line(&mut lines, &mut spans),
            Event::Rule => lines.push(Line::from(Span::styled("─".repeat(8), theme.muted()))),
            _ => {}
        }
    }
    if !spans.is_empty() {
        push_line(&mut lines, &mut spans);
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(String::new(), Style::default())));
    }
    lines
}
