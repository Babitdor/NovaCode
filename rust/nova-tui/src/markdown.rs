//! Render CommonMark and GitHub-flavored Markdown as terminal lines.

use pulldown_cmark::{Alignment, CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

use crate::theme::Theme;
use crate::ui::diff;

pub fn render(source: &str, theme: &Theme) -> Vec<Line<'static>> {
    let mut view = Markdown::new(theme);
    let options =
        Options::ENABLE_TABLES | Options::ENABLE_TASKLISTS | Options::ENABLE_STRIKETHROUGH;
    for event in Parser::new_ext(source, options) {
        match event {
            Event::Start(tag) => view.start(tag),
            Event::End(tag) => view.end(tag),
            Event::Text(text) => {
                if let Some((_, code)) = &mut view.code {
                    code.push_str(&text);
                } else {
                    view.spans
                        .push(Span::styled(text.into_string(), view.style));
                }
            }
            Event::Code(code) => view
                .spans
                .push(Span::styled(code.into_string(), view.theme.code_panel())),
            Event::SoftBreak => view.spans.push(Span::raw(" ")),
            Event::HardBreak => view.flush(),
            Event::TaskListMarker(checked) => {
                view.spans.push(Span::styled(
                    if checked { "☑ " } else { "☐ " },
                    if checked {
                        view.theme.success()
                    } else {
                        view.theme.muted()
                    },
                ));
            }
            Event::Rule => {
                view.flush();
                view.lines
                    .push(Line::from(Span::styled("────────", view.theme.muted())));
                view.space();
            }
            Event::Html(text) => {
                view.flush();
                for line in text.lines() {
                    view.spans
                        .push(Span::styled(line.to_owned(), view.theme.muted()));
                    view.flush();
                }
                view.space();
            }
            Event::InlineHtml(text) => {
                view.spans
                    .push(Span::styled(text.into_string(), view.theme.muted()));
            }
            _ => {}
        }
    }
    view.flush();
    while view.lines.last().is_some_and(|line| line.spans.is_empty()) {
        view.lines.pop();
    }
    view.lines
}

struct Markdown<'a> {
    theme: &'a Theme,
    lines: Vec<Line<'static>>,
    spans: Vec<Span<'static>>,
    style: Style,
    styles: Vec<Style>,
    list: Vec<Option<u64>>,
    quote_depth: usize,
    links: Vec<(String, usize)>,
    code: Option<(String, String)>,
    table: Option<Table>,
}

impl<'a> Markdown<'a> {
    fn new(theme: &'a Theme) -> Self {
        Self {
            theme,
            lines: Vec::new(),
            spans: Vec::new(),
            style: theme.body(),
            styles: Vec::new(),
            list: Vec::new(),
            quote_depth: 0,
            links: Vec::new(),
            code: None,
            table: None,
        }
    }

    fn flush(&mut self) {
        if !self.spans.is_empty() {
            let mut spans = std::mem::take(&mut self.spans);
            if self.quote_depth != 0 {
                spans.insert(
                    0,
                    Span::styled("│ ".repeat(self.quote_depth), self.theme.muted()),
                );
            }
            self.lines.push(Line::from(spans));
        }
    }

    fn space(&mut self) {
        if self.lines.last().is_some_and(|line| !line.spans.is_empty()) {
            self.lines.push(Line::default());
        }
    }

    fn styled(&mut self, style: Style) {
        self.styles.push(self.style);
        self.style = style;
    }

    fn start(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Heading { .. } => {
                self.flush();
                self.space();
                self.styled(self.theme.heading());
            }
            Tag::BlockQuote(_) => {
                self.flush();
                self.quote_depth += 1;
            }
            Tag::Emphasis => self.styled(self.style.add_modifier(Modifier::ITALIC)),
            Tag::Strong => self.styled(self.style.add_modifier(Modifier::BOLD)),
            Tag::Strikethrough => self.styled(self.style.add_modifier(Modifier::CROSSED_OUT)),
            Tag::CodeBlock(kind) => {
                self.flush();
                let language = match kind {
                    CodeBlockKind::Fenced(info) => {
                        info.split_whitespace().next().unwrap_or("").to_owned()
                    }
                    CodeBlockKind::Indented => String::new(),
                };
                self.code = Some((language, String::new()));
            }
            Tag::List(first) => {
                self.flush();
                self.list.push(first);
            }
            Tag::Item => {
                let indent = "  ".repeat(self.list.len().saturating_sub(1));
                let marker = self.list.last_mut().and_then(Option::as_mut).map_or_else(
                    || "•".to_owned(),
                    |number| {
                        let marker = format!("{number}.");
                        *number = number.saturating_add(1);
                        marker
                    },
                );
                self.spans.push(Span::styled(
                    format!("{indent}{marker} "),
                    self.theme.muted(),
                ));
            }
            Tag::Link { dest_url, .. } | Tag::Image { dest_url, .. } => {
                self.links.push((dest_url.into_string(), self.spans.len()));
                self.styled(self.theme.accent_style().add_modifier(Modifier::UNDERLINED));
            }
            Tag::Table(alignments) => self.table = Some(Table::new(alignments)),
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Heading(_) => {
                self.flush();
                self.space();
                self.style = self.styles.pop().unwrap_or_else(|| self.theme.body());
            }
            TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => {
                self.style = self.styles.pop().unwrap_or_else(|| self.theme.body());
            }
            TagEnd::Link | TagEnd::Image => {
                self.style = self.styles.pop().unwrap_or_else(|| self.theme.body());
                if let Some((url, start)) = self.links.pop() {
                    let already_visible = self
                        .spans
                        .iter()
                        .skip(start)
                        .map(|span| span.content.as_ref())
                        .collect::<String>()
                        == url;
                    if !already_visible && !url.is_empty() {
                        self.spans
                            .push(Span::styled(format!(" ({url})"), self.theme.muted()));
                    }
                }
            }
            TagEnd::CodeBlock => {
                if let Some((language, code)) = self.code.take() {
                    self.lines.push(Line::from(Span::styled(
                        format!(
                            "  {}",
                            if language.is_empty() {
                                "Code"
                            } else {
                                &language
                            }
                        ),
                        self.theme.heading().bg(self.theme.surface),
                    )));
                    if matches!(language.as_str(), "diff" | "patch") {
                        self.lines.extend(diff::lines(&code, true, self.theme));
                    } else {
                        let digits = code.lines().count().max(1).to_string().len();
                        for (index, line) in code.lines().enumerate() {
                            self.lines.push(Line::from(vec![
                                Span::styled(
                                    format!("  {:>digits$} │ ", index + 1),
                                    self.theme.muted().bg(self.theme.surface),
                                ),
                                Span::styled(line.replace('\t', "    "), self.theme.code_panel()),
                            ]));
                        }
                    }
                    self.space();
                }
            }
            TagEnd::List(_) => {
                self.list.pop();
                if self.list.is_empty() {
                    self.space();
                }
            }
            TagEnd::BlockQuote(_) => {
                self.flush();
                self.quote_depth = self.quote_depth.saturating_sub(1);
                self.space();
            }
            TagEnd::Item | TagEnd::Paragraph => {
                self.flush();
                if self.list.is_empty() {
                    self.space();
                }
            }
            TagEnd::TableCell => {
                if let Some(table) = &mut self.table {
                    table
                        .cells
                        .push(Line::from(std::mem::take(&mut self.spans)));
                }
            }
            TagEnd::TableHead | TagEnd::TableRow => {
                if let Some(table) = &mut self.table {
                    table.rows.push(std::mem::take(&mut table.cells));
                }
            }
            TagEnd::Table => {
                if let Some(table) = self.table.take() {
                    table.finish(self.theme, &mut self.lines);
                    self.space();
                }
            }
            _ => {}
        }
    }
}

struct Table {
    alignments: Vec<Alignment>,
    rows: Vec<Vec<Line<'static>>>,
    cells: Vec<Line<'static>>,
}

impl Table {
    fn new(alignments: Vec<Alignment>) -> Self {
        Self {
            alignments,
            rows: Vec::new(),
            cells: Vec::new(),
        }
    }

    fn finish(self, theme: &Theme, lines: &mut Vec<Line<'static>>) {
        let widths: Vec<_> = (0..self.alignments.len())
            .map(|index| {
                self.rows
                    .iter()
                    .filter_map(|row| row.get(index))
                    .map(Line::width)
                    .max()
                    .unwrap_or(1)
                    .min(48)
            })
            .collect();
        for (row_index, row) in self.rows.into_iter().enumerate() {
            let mut spans = Vec::new();
            for (index, cell) in row.into_iter().enumerate() {
                let Some(width) = widths.get(index) else {
                    break;
                };
                if index != 0 {
                    spans.push(Span::styled(" │ ", theme.muted()));
                }
                let padding = width.saturating_sub(cell.width());
                let left = match self.alignments[index] {
                    Alignment::Right => padding,
                    Alignment::Center => padding / 2,
                    _ => 0,
                };
                spans.push(Span::raw(" ".repeat(left)));
                spans.extend(cell.spans.into_iter().map(|span| {
                    if row_index == 0 {
                        span.patch_style(theme.heading())
                    } else {
                        span
                    }
                }));
                spans.push(Span::raw(" ".repeat(padding - left)));
            }
            lines.push(Line::from(spans));
            if row_index == 0 {
                let rule = widths
                    .iter()
                    .map(|width| "─".repeat(*width))
                    .collect::<Vec<_>>()
                    .join("─┼─");
                lines.push(Line::from(Span::styled(rule, theme.muted())));
            }
        }
    }
}
