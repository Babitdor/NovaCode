//! `/nova`'s sprite: the frames the server shipped, played on the client clock.
//!
//! The Python side owns the pixels-to-glyph reduction (`novacode_cli/ui/avatar.py`),
//! so a cell's character and colours arrive ready to draw. The rules here mirror
//! the Textual widget: a reaction mood wins, an active turn shows talking or
//! thinking, a minute of quiet falls asleep, and the idle cycle walks through
//! idle/sitting/playful/walking every eight seconds.
//!
//! The sprite floats over the conversation the way it does in the terminal UI:
//! transparent cells are never painted, so the transcript shows through.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::Frame as TerminalFrame;
use serde_json::Value;

/// The overlay needs room before it appears, exactly like the terminal UI.
pub const MIN_WIDTH: u16 = 100;
/// See [`MIN_WIDTH`].
pub const MIN_HEIGHT: u16 = 32;
/// How long an end-of-turn reaction plays before the idle cycle resumes.
pub const REACTION_SECONDS: f64 = 4.0;
/// A minute of quiet puts Nova to sleep, as in the terminal UI.
const SLEEP_AFTER: f64 = 60.0;
/// The idle cycle, one state per eight seconds.
const IDLE_CYCLE: [&str; 4] = ["idle", "sitting", "playful", "walking"];

/// One animation frame: its state, glyph rows and per-cell palette indexes.
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    /// The animation state this frame belongs to (`idle`, `talking`, …).
    pub state: String,
    /// One string per character row, already `width` characters wide.
    pub rows: Vec<String>,
    /// One palette index per cell, row-major.
    pub cells: Vec<Vec<usize>>,
}

/// The sprite the server shipped, plus the clock the client plays it on.
#[derive(Debug, Clone, PartialEq)]
pub struct Avatar {
    /// Character columns in the sprite.
    pub width: u16,
    /// Character rows in the sprite.
    pub height: u16,
    /// Foreground colour per palette index; `None` is transparent.
    pub foreground: Vec<Option<Color>>,
    /// Background colour per palette index; `None` leaves the cell alone.
    pub background: Vec<Option<Color>>,
    /// Frames per second per state.
    pub fps: Vec<(String, f64)>,
    /// Every frame, in sheet order.
    pub frames: Vec<Frame>,
    /// Seconds this sprite has been on screen.
    pub shown: f64,
    /// The moment the last busy tick was seen, for the idle cycle.
    pub idle_since: f64,
    /// A reaction that outranks the activity state until `until` seconds.
    pub mood: Option<(String, f64)>,
}

impl Avatar {
    /// Decode the `/nova` payload, or `None` when it is missing or malformed.
    ///
    /// A client must never draw a half-decoded sprite, so every part is required
    /// and any miss returns `None` instead of a partial avatar.
    pub fn decode(value: &Value) -> Option<Self> {
        let width = u16::try_from(value.get("width")?.as_u64()?).ok()?;
        let height = u16::try_from(value.get("height")?.as_u64()?).ok()?;
        let foreground = colours(value.get("foreground")?);
        let background = colours(value.get("background")?);
        let fps = value
            .get("fps")?
            .as_object()?
            .iter()
            .map(|(state, rate)| Some((state.clone(), rate.as_f64()?)))
            .collect::<Option<Vec<_>>>()?;
        let frames = value
            .get("frames")?
            .as_array()?
            .iter()
            .map(|frame| {
                Some(Frame {
                    state: frame.get("state")?.as_str()?.to_string(),
                    rows: frame
                        .get("rows")?
                        .as_array()?
                        .iter()
                        .map(|row| row.as_str().map(str::to_string))
                        .collect::<Option<Vec<_>>>()?,
                    cells: frame
                        .get("cells")?
                        .as_array()?
                        .iter()
                        .map(|row| {
                            row.as_array()?
                                .iter()
                                .map(|index| usize::try_from(index.as_u64()?).ok())
                                .collect::<Option<Vec<_>>>()
                        })
                        .collect::<Option<Vec<_>>>()?,
                })
            })
            .collect::<Option<Vec<_>>>()?;
        if frames.is_empty() || foreground.is_empty() || width == 0 || height == 0 {
            return None;
        }
        let mut avatar = Self {
            width,
            height,
            foreground,
            background,
            fps,
            frames,
            shown: 0.0,
            idle_since: 0.0,
            mood: None,
        };
        avatar.react("happy");
        Some(avatar)
    }

    /// Play *mood* for [`REACTION_SECONDS`], restarting the idle cycle.
    pub fn react(&mut self, mood: &str) {
        self.idle_since = self.shown;
        self.mood = Some((mood.to_string(), self.shown + REACTION_SECONDS));
    }

    /// Advance the clock by the loop's tick; a busy tick also restarts the idle
    /// cycle, so the sprite starts from `idle` the moment a turn ends.
    pub fn advance(&mut self, seconds: f64, busy: bool) {
        self.shown += seconds;
        if busy {
            self.idle_since = self.shown;
        }
    }

    /// Frames per second for *state*, falling back to the frame count's rate.
    fn rate(&self, state: &str) -> f64 {
        self.fps
            .iter()
            .find(|(name, _)| name == state)
            .map_or(2.0, |(_, rate)| *rate)
    }

    /// The state to draw right now, given what the client knows locally.
    ///
    /// Read-only so a frame can be produced from a `&AppState`: an expired
    /// reaction simply stops outranking the activity state.
    pub fn state_at(&self, busy: bool, talking: bool, working: bool) -> String {
        if let Some((mood, until)) = &self.mood {
            if self.shown < *until {
                return mood.clone();
            }
        }
        if busy {
            return if talking {
                "talking".to_string()
            } else if working {
                "running".to_string()
            } else {
                "thinking".to_string()
            };
        }
        let idle = self.shown - self.idle_since;
        if idle >= SLEEP_AFTER {
            return "sleeping".to_string();
        }
        IDLE_CYCLE[(idle / 8.0) as usize % IDLE_CYCLE.len()].to_string()
    }

    /// The frame to draw for *state*, or the first frame of that state.
    pub fn frame_for(&self, state: &str) -> Option<&Frame> {
        let matching: Vec<&Frame> = self.frames.iter().filter(|f| f.state == state).collect();
        if matching.is_empty() {
            return self.frames.first();
        }
        let count = matching.len();
        let index = (self.shown * self.rate(state)) as usize % count;
        Some(matching[index])
    }

    /// Paint one frame above the composer, inside *area*.
    pub fn render(&self, area: Rect, frame: &Frame, buffer: &mut Buffer) {
        let Some(left) = area.right().checked_sub(self.width + 1) else {
            return;
        };
        let Some(top) = area.bottom().checked_sub(self.height + 1) else {
            return;
        };
        for (row, line) in frame.rows.iter().enumerate().take(usize::from(self.height)) {
            for (column, glyph) in line.chars().enumerate().take(usize::from(self.width)) {
                let Some(indexes) = frame.cells.get(row) else {
                    continue;
                };
                let Some(&index) = indexes.get(column) else {
                    continue;
                };
                let foreground = self.foreground.get(index).copied().flatten();
                let background = self.background.get(index).copied().flatten();
                if foreground.is_none() && background.is_none() {
                    // Transparent: the conversation underneath stays visible.
                    continue;
                }
                let Ok(x) = u16::try_from(column) else {
                    continue;
                };
                let Ok(y) = u16::try_from(row) else {
                    continue;
                };
                let cell = buffer.cell_mut((left + x, top + y));
                if let Some(cell) = cell {
                    cell.set_char(glyph).set_style(
                        Style::default()
                            .fg(foreground.unwrap_or(Color::Reset))
                            .bg(background.unwrap_or(Color::Reset)),
                    );
                }
            }
        }
    }
}

/// Parse a colour palette; an empty string means "leave this channel alone".
fn colours(value: &Value) -> Vec<Option<Color>> {
    value
        .as_array()
        .map(|entries| entries.iter().map(|entry| hex(entry.as_str())).collect())
        .unwrap_or_default()
}

/// `#rrggbb` to a truecolor; anything else is transparent.
fn hex(value: Option<&str>) -> Option<Color> {
    let text = value?.strip_prefix('#')?;
    if text.len() != 6 {
        return None;
    }
    let channel = |start: usize| u8::from_str_radix(text.get(start..start + 2)?, 16).ok();
    Some(Color::Rgb(channel(0)?, channel(2)?, channel(4)?))
}

/// Draw the avatar when it is on and the terminal has room for it.
pub fn draw(
    frame: &mut TerminalFrame,
    area: Rect,
    avatar: &Avatar,
    busy: bool,
    talking: bool,
    working: bool,
) {
    // The terminal decides, not the conversation pane: the terminal UI's rule is
    // about the window it is drawn in.
    let screen = frame.area();
    if screen.width < MIN_WIDTH || screen.height < MIN_HEIGHT {
        return;
    }
    let state = avatar.state_at(busy, talking, working);
    let Some(sprite) = avatar.frame_for(&state).cloned() else {
        return;
    };
    avatar.render(area, &sprite, frame.buffer_mut());
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Two cells is enough to exercise every rule the real sheet uses.
    fn tiny() -> Avatar {
        Avatar::decode(&serde_json::json!({
            "width": 2,
            "height": 1,
            "fps": {"idle": 2.0, "talking": 4.0},
            "foreground": ["", "#3b82f6"],
            "background": ["", ""],
            "frames": [
                {"state": "idle", "rows": [" █"], "cells": [[0, 1]]},
                {"state": "idle", "rows": ["█ "], "cells": [[1, 0]]},
                {"state": "talking", "rows": ["██"], "cells": [[1, 1]]}
            ]
        }))
        .expect("a complete payload decodes")
    }

    #[test]
    fn a_partial_payload_is_refused_rather_than_half_drawn() {
        assert!(Avatar::decode(&serde_json::json!({"width": 2})).is_none());
        let mut value = serde_json::json!({
            "width": 2, "height": 1, "fps": {}, "foreground": [], "background": [],
            "frames": []
        });
        assert!(Avatar::decode(&value).is_none());
        value["foreground"] = serde_json::json!([""]);
        value["frames"] = serde_json::json!([{"state": "idle", "rows": ["  "], "cells": [[0, 0]]}]);
        assert!(Avatar::decode(&value).is_some());
    }

    #[test]
    fn turning_it_on_starts_with_a_reaction() {
        let avatar = tiny();
        let mood = avatar.mood.expect("a fresh sprite greets with a mood");
        assert_eq!(mood.0, "happy");
        assert!((mood.1 - REACTION_SECONDS).abs() < f64::EPSILON);
    }

    #[test]
    fn activity_outranks_the_idle_cycle_once_the_reaction_ends() {
        let mut avatar = tiny();
        avatar.advance(REACTION_SECONDS + 1.0, false);
        assert_eq!(avatar.state_at(true, false, false), "thinking");
        assert_eq!(avatar.state_at(true, true, false), "talking");
        assert_eq!(avatar.state_at(true, false, true), "running");
        assert_eq!(avatar.state_at(false, false, false), "idle");
    }

    #[test]
    fn the_idle_cycle_walks_and_then_sleeps() {
        let mut avatar = tiny();
        avatar.advance(REACTION_SECONDS + 1.0, false);

        let mut seen = Vec::new();
        for _ in 0..4 {
            seen.push(avatar.state_at(false, false, false));
            avatar.advance(8.0, false);
        }
        assert_eq!(seen, ["idle", "sitting", "playful", "walking"]);
        avatar.advance(SLEEP_AFTER, false);
        assert_eq!(avatar.state_at(false, false, false), "sleeping");
    }

    #[test]
    fn the_frame_follows_the_states_own_rate() {
        let mut avatar = tiny();
        avatar.mood = None;
        assert_eq!(avatar.frame_for("idle").expect("idle exists").state, "idle");
        avatar.advance(0.5, false);
        assert_eq!(avatar.frame_for("idle").expect("idle exists").rows, ["█ "]);
        assert_eq!(
            avatar.frame_for("talking").expect("talking exists").rows,
            ["██"]
        );
        // A state the sheet lacks falls back to its first frame.
        assert_eq!(avatar.frame_for("sleeping").expect("a frame").state, "idle");
    }

    #[test]
    fn a_transparent_cell_never_paints_over_the_conversation() {
        let avatar = tiny();
        let area = Rect::new(0, 0, MIN_WIDTH, MIN_HEIGHT);
        let mut buffer = Buffer::empty(area);
        buffer.set_string(0, MIN_HEIGHT - 2, "underneath", Style::default());
        let frame = avatar.frames[0].clone();
        avatar.render(area, &frame, &mut buffer);
        let row = MIN_HEIGHT - 2;
        let line: String = (0..MIN_WIDTH)
            .map(|x| buffer[(x, row)].symbol().to_string())
            .collect();
        assert!(line.starts_with("underneath"));
        assert_eq!(buffer[(MIN_WIDTH - 2, row)].symbol(), "█");
    }
}
