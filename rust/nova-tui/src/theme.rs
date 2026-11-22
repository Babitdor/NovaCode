//! Colours and styles in one place, so the ratatui client can grow a theme
//! system later without every widget holding its own constants.
//!
//! Increment 1 has a single dark palette: the Textual UI's 14 selectable
//! interface styles are a wave-5 feature (see `rust/LEDGER.md`).

use ratatui::style::{Color, Modifier, Style};

use crate::frames::ThemePalette;

/// The palette and the styles derived from it.
#[derive(Debug, Clone, Copy)]
pub struct Theme {
    pub accent: Color,
    pub text: Color,
    pub dim: Color,
    pub error: Color,
    pub warning: Color,
    pub success: Color,
    pub border: Color,
    pub code: Color,
}

impl Default for Theme {
    fn default() -> Self {
        Self {
            accent: Color::Rgb(0xff, 0x87, 0x5f),
            text: Color::Rgb(0xe6, 0xe6, 0xe6),
            dim: Color::Rgb(0x8a, 0x8a, 0x8a),
            error: Color::Rgb(0xff, 0x5f, 0x5f),
            warning: Color::Rgb(0xff, 0xc8, 0x5f),
            success: Color::Rgb(0x6f, 0xd1, 0x8f),
            border: Color::Rgb(0x4a, 0x4a, 0x4a),
            code: Color::Rgb(0x9c, 0xdc, 0xfe),
        }
    }
}

impl Theme {
    /// Base text style.
    pub fn body(&self) -> Style {
        Style::default().fg(self.text)
    }

    /// Secondary text: reasoning, hints, metadata.
    pub fn muted(&self) -> Style {
        Style::default().fg(self.dim)
    }

    /// Assistant prose.
    pub fn assistant(&self) -> Style {
        Style::default().fg(self.text)
    }

    /// The user's own messages.
    pub fn user(&self) -> Style {
        Style::default()
            .fg(self.accent)
            .add_modifier(Modifier::BOLD)
    }

    /// Errors and failed tools.
    pub fn error(&self) -> Style {
        Style::default().fg(self.error)
    }

    /// Notices and provider warnings.
    pub fn warning(&self) -> Style {
        Style::default().fg(self.warning)
    }

    /// Successful tool results.
    pub fn success(&self) -> Style {
        Style::default().fg(self.success)
    }

    /// Inline code and code blocks.
    pub fn code(&self) -> Style {
        Style::default().fg(self.code)
    }

    /// Headings inside assistant markdown.
    pub fn heading(&self) -> Style {
        Style::default()
            .fg(self.accent)
            .add_modifier(Modifier::BOLD)
    }

    /// Composer and panel borders.
    pub fn border(&self) -> Style {
        Style::default().fg(self.border)
    }

    /// The accent as a foreground style (cursor, emphasis).
    pub fn accent_style(&self) -> Style {
        Style::default().fg(self.accent)
    }

    /// A border that draws attention (an open modal).
    pub fn accent_border(&self) -> Style {
        Style::default().fg(self.accent)
    }
}

/// Parse `#rrggbb` into a colour, falling back when the value is absent or bad.
///
/// A theme the server sends is data, not a guarantee: a malformed colour must
/// leave the previous one in place rather than paint garbage.
fn colour(value: Option<&String>, fallback: Color) -> Color {
    let Some(text) = value else {
        return fallback;
    };
    let hex = text.strip_prefix('#').unwrap_or(text);
    if hex.len() != 6 {
        return fallback;
    }
    let channel = |range: std::ops::Range<usize>| u8::from_str_radix(&hex[range], 16).ok();
    match (channel(0..2), channel(2..4), channel(4..6)) {
        (Some(red), Some(green), Some(blue)) => Color::Rgb(red, green, blue),
        _ => fallback,
    }
}

impl Theme {
    /// Build a palette from a `ui_theme` frame, over `base`'s colours.
    ///
    /// Colours the frame omits keep their `base` value, which is what a theme
    /// that defines no `text-muted` or `border` needs.
    pub fn from_palette(palette: &ThemePalette, base: Theme) -> Self {
        let border = colour(palette.border.as_ref(), base.border);
        Self {
            accent: colour(palette.primary.as_ref(), base.accent),
            text: colour(palette.foreground.as_ref(), base.text),
            dim: colour(palette.muted.as_ref(), base.dim),
            error: colour(palette.error.as_ref(), base.error),
            warning: colour(palette.warning.as_ref(), base.warning),
            success: colour(palette.success.as_ref(), base.success),
            border: colour(palette.panel.as_ref(), border),
            code: base.code,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn palette(primary: Option<&str>) -> ThemePalette {
        ThemePalette {
            name: "test".to_string(),
            dark: true,
            primary: primary.map(str::to_string),
            background: None,
            foreground: Some("#eeeeee".to_string()),
            surface: None,
            panel: None,
            muted: None,
            border: None,
            success: None,
            warning: None,
            error: None,
        }
    }

    #[test]
    fn a_palette_replaces_the_colours_it_carries() {
        let theme = Theme::from_palette(&palette(Some("#f4723f")), Theme::default());
        assert_eq!(theme.accent, Color::Rgb(0xf4, 0x72, 0x3f));
        assert_eq!(theme.text, Color::Rgb(0xee, 0xee, 0xee));
    }

    #[test]
    fn a_missing_colour_keeps_the_previous_one() {
        let base = Theme::default();
        let theme = Theme::from_palette(&palette(None), base);
        assert_eq!(theme.accent, base.accent);
        assert_eq!(theme.dim, base.dim);
    }

    #[test]
    fn a_malformed_colour_is_ignored() {
        let base = Theme::default();
        let theme = Theme::from_palette(&palette(Some("#12345")), base);
        assert_eq!(theme.accent, base.accent, "five digits is not a colour");
    }
}
