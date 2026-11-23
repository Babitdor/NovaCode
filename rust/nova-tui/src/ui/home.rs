//! The home banner: Nova's portrait and wordmark, over the code rain.
//!
//! The terminal UI offers fourteen home layouts (`novacode_cli/tui/home.py`).
//! A socket client cannot take Textual widget classes, so the client draws the
//! one identity every layout shares — the portrait, the wordmark, the version —
//! sized to the terminal the way `brand.art_for` sizes it, with the rain behind
//! it when the saved preference enables animation. The style name still arrives
//! from the server so a future layout can be selected without a protocol change.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::Frame as TerminalFrame;
use unicode_width::UnicodeWidthChar;

use crate::theme::Theme;
use crate::ui::brand_art::{
    COLUMN_GAP, COLUMN_WIDTH, COMPACT_MARK, NAME_LINE, PORTRAIT, PORTRAIT_WIDTH, TALL_MIN_HEIGHT,
    TRANSCRIPT_INSET, WORDMARK_LINES, WORDMARK_WIDTH,
};
use crate::ui::matrix::Rain;

/// The home layouts that show a banner at all, from `tui/home.py`.
const BANNER_STYLES: [&str; 3] = ["dashboard", "classic", "signal"];

/// The banner, its animation clock and the terminal it was sized for.
#[derive(Debug, Clone)]
pub struct Home {
    /// The saved layout name (`dashboard`, `classic`, `signal`, …).
    pub style: String,
    /// Whether the rain animates.
    pub animation: bool,
    /// Nova's version, for the caption under the wordmark.
    pub version: String,
    rain: Rain,
    sized: (u16, u16),
}

impl Home {
    /// Build the banner for *style*; the rain starts immediately when animated.
    pub fn new(style: &str, animation: bool, version: &str) -> Self {
        Self {
            style: style.to_string(),
            animation,
            version: version.to_string(),
            rain: Rain::new(0, 0, seed(style, version)),
            sized: (0, 0),
        }
    }

    /// Whether this layout shows a banner at all.
    pub fn shows_banner(&self) -> bool {
        BANNER_STYLES.contains(&self.style.as_str())
    }

    /// Advance the animation clock; a paused banner keeps its last frame.
    pub fn advance(&mut self, seconds: f64) {
        if self.animation {
            self.rain.advance(seconds);
        }
    }

    /// Resize the rain grid when the banner's area changes.
    fn fit(&mut self, area: Rect) {
        if self.sized != (area.width, area.height) {
            self.rain.resize(area.width, area.height);
            self.sized = (area.width, area.height);
        }
    }

    /// Draw the banner, rain first, into *area*.
    pub fn draw(&mut self, frame: &mut TerminalFrame, area: Rect, theme: &Theme) {
        self.fit(area);
        if self.animation {
            let buffer = frame.buffer_mut();
            self.rain.draw(area, buffer, theme.accent);
        }
        let rows = art_rows(area.width, area.height, &self.version);
        let width = rows.iter().map(|row| cells(&row.text)).max().unwrap_or(0);
        if width == 0 || usize::from(area.width) < width {
            return;
        }
        let left = area.x + u16::try_from((usize::from(area.width) - width) / 2).unwrap_or(0);
        let height = u16::try_from(rows.len()).unwrap_or(0);
        let top = area.y + area.height.saturating_sub(height) / 2;
        let buffer = frame.buffer_mut();
        for (index, row) in rows.iter().enumerate() {
            let Ok(offset) = u16::try_from(index) else {
                continue;
            };
            let y = top + offset;
            if y >= area.bottom() {
                continue;
            }
            paint_row(buffer, row, left, y, area, theme);
        }
    }
}

/// One banner row: its text, and whether it is the version caption.
#[derive(Debug, Clone)]
struct Row {
    text: String,
    caption: bool,
}

/// Rows of the banner, per `brand.art_for(width, height, version)`.
fn art_rows(width: u16, height: u16, version: &str) -> Vec<Row> {
    let usable = width.saturating_sub(TRANSCRIPT_INSET);
    let short = height < TALL_MIN_HEIGHT;
    let caption = |text: String| Row {
        text,
        caption: true,
    };
    let plain = |text: String| Row {
        text,
        caption: false,
    };
    if usable >= PORTRAIT_WIDTH + COLUMN_GAP + COLUMN_WIDTH && !short {
        return lockup_rows(version);
    }
    if usable >= WORDMARK_WIDTH {
        let mut rows: Vec<Row> = WORDMARK_LINES
            .iter()
            .map(|row| plain((*row).to_string()))
            .collect();
        if !version.is_empty() {
            rows.push(plain(" ".repeat(usize::from(WORDMARK_WIDTH))));
            rows.push(caption(pad(&format!("v{version}"), WORDMARK_WIDTH, true)));
        }
        return rows;
    }
    if usable >= PORTRAIT_WIDTH && !short {
        let mut rows: Vec<Row> = PORTRAIT
            .iter()
            .map(|row| plain((*row).to_string()))
            .collect();
        rows.push(plain(" ".repeat(usize::from(PORTRAIT_WIDTH))));
        rows.push(plain(pad(NAME_LINE, PORTRAIT_WIDTH, true)));
        if !version.is_empty() {
            rows.push(caption(pad(&format!("v{version}"), PORTRAIT_WIDTH, true)));
        }
        return rows;
    }
    vec![plain(COMPACT_MARK.to_string())]
}

/// Portrait and wordmark side by side, with the version under the wordmark.
fn lockup_rows(version: &str) -> Vec<Row> {
    let mut column: Vec<Row> = WORDMARK_LINES
        .iter()
        .map(|row| Row {
            text: pad(row, COLUMN_WIDTH, false),
            caption: false,
        })
        .collect();
    column.push(Row {
        text: " ".repeat(usize::from(COLUMN_WIDTH)),
        caption: false,
    });
    column.push(Row {
        text: if version.is_empty() {
            " ".repeat(usize::from(COLUMN_WIDTH))
        } else {
            pad(&format!("v{version}"), COLUMN_WIDTH, false)
        },
        caption: !version.is_empty(),
    });
    column.resize(
        PORTRAIT.len(),
        Row {
            text: " ".repeat(usize::from(COLUMN_WIDTH)),
            caption: false,
        },
    );
    PORTRAIT
        .iter()
        .zip(column)
        .map(|(portrait, caption)| Row {
            text: format!(
                "{portrait}{gap}{text}",
                gap = " ".repeat(usize::from(COLUMN_GAP)),
                text = caption.text
            ),
            caption: caption.caption,
        })
        .collect()
}

/// Right-pad *text* to *width* cells; optionally centre it first.
fn pad(text: &str, width: u16, centre: bool) -> String {
    let width = usize::from(width);
    let mut row = text.to_string();
    let used = cells(&row);
    if centre && used < width {
        let left = (width - used) / 2;
        row = format!("{}{}", " ".repeat(left), row);
    }
    let used = cells(&row);
    if used < width {
        row.push_str(&" ".repeat(width - used));
    }
    row
}

/// Terminal cells a string occupies; a character that cannot be measured is 1.
fn cells(text: &str) -> usize {
    text.chars()
        .map(|glyph| UnicodeWidthChar::width(glyph).unwrap_or(1))
        .sum()
}

/// Paint one banner row; spaces stay transparent so the rain shows through.
fn paint_row(buffer: &mut Buffer, row: &Row, left: u16, y: u16, area: Rect, theme: &Theme) {
    let style = if row.caption {
        Style::default().fg(theme.dim)
    } else {
        Style::default()
            .fg(theme.accent)
            .add_modifier(Modifier::BOLD)
    };
    let mut x = left;
    for glyph in row.text.chars() {
        let width = u16::try_from(UnicodeWidthChar::width(glyph).unwrap_or(1)).unwrap_or(1);
        if x + width > area.right() || y >= area.bottom() {
            return;
        }
        if glyph != ' ' {
            if let Some(cell) = buffer.cell_mut((x, y)) {
                cell.set_char(glyph).set_style(style);
            }
            // A wide glyph owns the next cell; clear it so no rain shows through
            // the middle of the portrait.
            for offset in 1..width {
                if let Some(cell) = buffer.cell_mut((x + offset, y)) {
                    cell.set_char(' ').set_style(style);
                }
            }
        }
        x += width;
    }
}

/// A stable seed per layout, so the rain does not reshuffle on every redraw.
fn seed(style: &str, version: &str) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in style.bytes().chain(version.bytes()) {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash | 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_art_follows_the_terminal_size() {
        let texts =
            |rows: Vec<Row>| -> Vec<String> { rows.into_iter().map(|row| row.text).collect() };
        assert_eq!(texts(art_rows(32, 40, "")), vec![COMPACT_MARK.to_string()]);
        let portrait = texts(art_rows(40, 40, ""));
        assert_eq!(portrait.len(), PORTRAIT.len() + 2);
        assert!(portrait.last().expect("rows").contains("NOVA"));
        // A short window drops the portrait tier even when it would fit.
        assert_eq!(texts(art_rows(40, 10, "")), vec![COMPACT_MARK.to_string()]);
        let wordmark = texts(art_rows(60, 40, "1.2.3"));
        assert_eq!(wordmark[0], WORDMARK_LINES[0]);
        assert!(wordmark.iter().any(|row| row.contains("v1.2.3")));
        let lockup = art_rows(100, 40, "1.2.3");
        assert_eq!(lockup.len(), PORTRAIT.len());
        assert!(lockup[0].text.starts_with(PORTRAIT[0]));
        let caption = lockup.iter().filter(|row| row.caption).count();
        assert_eq!(caption, 1, "only the version row is muted");
    }

    #[test]
    fn every_lockup_row_is_one_width() {
        let rows = art_rows(120, 40, "1.2.3");
        let widths: Vec<usize> = rows.iter().map(|row| cells(&row.text)).collect();
        assert!(
            widths.windows(2).all(|pair| pair[0] == pair[1]),
            "{widths:?}"
        );
    }

    #[test]
    fn only_the_banner_layouts_show_a_banner() {
        assert!(Home::new("signal", true, "1.2.3").shows_banner());
        assert!(Home::new("dashboard", false, "1.2.3").shows_banner());
        assert!(!Home::new("studio", true, "1.2.3").shows_banner());
    }

    #[test]
    fn the_rain_seed_is_stable_per_layout() {
        assert_eq!(seed("signal", "1.0"), seed("signal", "1.0"));
        assert_ne!(seed("signal", "1.0"), seed("signal", "2.0"));
    }

    #[test]
    fn a_paused_banner_keeps_its_frame() {
        let mut home = Home::new("classic", false, "1.0");
        home.advance(10.0);
        let moving = Home::new("classic", true, "1.0");
        assert!(!home.animation);
        assert!(moving.animation);
    }
}
