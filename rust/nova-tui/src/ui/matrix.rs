//! The code rain behind the home banner.
//!
//! A port of `novacode_cli/tui/matrix3.py`: ninety-percent density, weighted
//! speeds, an 80 ms step, per-step character mutation and a quadratic fade from
//! the head. The animation is decoration, so it lives entirely on the client and
//! needs no server frame — and no `rand` crate: a small xorshift generator keeps
//! the dependency list as short as the rest of the client.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};

/// Seconds between rain steps, from the source animation.
const STEP_SECONDS: f64 = 0.08;
/// Share of the even columns that carry a chain.
const DENSITY: f64 = 0.9;
/// Chance per character per step that it changes glyph.
const MUTATION_RATE: f64 = 0.002;
/// "Love of my life": each character occupies two terminal cells.
const CHARACTERS: [char; 6] = ['生', '涯', '最', '愛', 'の', '人'];
/// Brightest level; the fade uses levels 0 to [`FADE_LEVELS`].
const FADE_LEVELS: f64 = 15.0;

/// One falling chain of characters.
#[derive(Debug, Clone)]
struct Column {
    x: u16,
    /// Row of the chain's head, which may start above the screen.
    head: i32,
    speed: i32,
    chain: Vec<char>,
}

/// A xorshift64* generator: enough randomness for decoration, and testable.
#[derive(Debug, Clone)]
struct Rng(u64);

impl Rng {
    /// The next value in the sequence.
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// A value in `0..bound`; `0` when the bound is empty.
    fn below(&mut self, bound: u64) -> u64 {
        if bound == 0 {
            return 0;
        }
        self.next() % bound
    }

    /// A value in `low..=high`.
    fn between(&mut self, low: i32, high: i32) -> i32 {
        if high <= low {
            return low;
        }
        low + i32::try_from(self.below(u64::try_from(high - low + 1).unwrap_or(1))).unwrap_or(0)
    }

    /// A value in `0.0..1.0`.
    fn unit(&mut self) -> f64 {
        // 53 significant bits, the same precision `rand` gives `f64`.
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// One of *choices*, uniformly.
    fn choice(&mut self, choices: &[char]) -> char {
        let index = usize::try_from(self.below(choices.len() as u64)).unwrap_or(0);
        choices[index]
    }

    /// A weighted speed: 1 most often, then 2, then 3 — as the source draws it.
    fn speed(&mut self) -> i32 {
        let roll = self.unit();
        if roll < 1.0 / 1.42 {
            1
        } else if roll < 1.4 / 1.42 {
            2
        } else {
            3
        }
    }
}

/// The live rain: chains plus the glyph and fade grids they paint.
#[derive(Debug, Clone)]
pub struct Rain {
    width: u16,
    height: u16,
    /// `width` cells by `height` rows; a wide glyph leaves its second cell blank.
    glyphs: Vec<Vec<char>>,
    /// Fade level per cell; `-1` is empty.
    levels: Vec<Vec<i32>>,
    /// The left columns available to chains: every other cell.
    slots: Vec<u16>,
    columns: Vec<Column>,
    elapsed: f64,
    rng: Rng,
}

impl Rain {
    /// Seed a full screen of chains, so the banner never opens on an empty grid.
    pub fn new(width: u16, height: u16, seed: u64) -> Self {
        let mut rain = Self {
            width,
            height,
            glyphs: Vec::new(),
            levels: Vec::new(),
            slots: Vec::new(),
            columns: Vec::new(),
            elapsed: 0.0,
            rng: Rng(seed | 1),
        };
        rain.resize(width, height);
        rain
    }

    /// Rebuild the grid for a new terminal size, keeping the generator moving.
    pub fn resize(&mut self, width: u16, height: u16) {
        self.width = width;
        self.height = height;
        self.glyphs = vec![vec![' '; usize::from(width)]; usize::from(height)];
        self.levels = vec![vec![-1; usize::from(width)]; usize::from(height)];
        self.slots = (0..width.saturating_sub(1)).step_by(2).collect();
        let slots = self.slots.len();
        let wanted = (slots as f64 * DENSITY) as usize;
        let mut picked: Vec<u16> = Vec::with_capacity(wanted);
        while picked.len() < wanted && picked.len() < slots {
            let index = usize::try_from(self.rng.below(slots as u64)).unwrap_or(0);
            let slot = self.slots[index];
            if !picked.contains(&slot) {
                picked.push(slot);
            }
        }
        self.columns = picked.into_iter().map(|x| self.column(x, true)).collect();
        self.paint();
    }

    /// A fresh chain at *x*, optionally already on screen.
    fn column(&mut self, x: u16, visible: bool) -> Column {
        let height = i32::from(self.height);
        let low = i32::from(self.height.clamp(1, 4));
        let length = usize::try_from(self.rng.between(low, height)).unwrap_or(1);
        let chain = (0..length).map(|_| self.rng.choice(&CHARACTERS)).collect();
        Column {
            x,
            head: self.rng.between(-height, if visible { height } else { 0 }),
            speed: self.rng.speed(),
            chain,
        }
    }

    /// Repaint the grids from the live chains.
    fn paint(&mut self) {
        for row in &mut self.glyphs {
            row.fill(' ');
        }
        for row in &mut self.levels {
            row.fill(-1);
        }
        for column in &self.columns {
            let length = column.chain.len() as f64;
            for (distance, glyph) in column.chain.iter().enumerate() {
                let row = column.head - distance as i32;
                if !(0..i32::from(self.height)).contains(&row) {
                    continue;
                }
                let Ok(y) = usize::try_from(row) else {
                    continue;
                };
                let x = usize::from(column.x);
                let level = if distance == 0 {
                    0
                } else {
                    1 + (FADE_LEVELS * (distance as f64 / length).powi(2)).round() as i32
                };
                if let Some(cell) = self.glyphs[y].get_mut(x) {
                    *cell = *glyph;
                }
                if let Some(cell) = self.levels[y].get_mut(x) {
                    *cell = level;
                }
                // The glyph is two cells wide, so the next cell stays blank.
                if let Some(cell) = self.glyphs[y].get_mut(x + 1) {
                    *cell = ' ';
                }
                if let Some(cell) = self.levels[y].get_mut(x + 1) {
                    *cell = level;
                }
            }
        }
    }

    /// Move every chain one step, respawning any that have left the screen.
    fn step(&mut self) {
        let height = i32::from(self.height);
        let slots = self.slots.clone();
        for index in 0..self.columns.len() {
            let mut column = self.columns[index].clone();
            for _ in 0..column.speed.max(1) {
                column.chain.insert(0, self.rng.choice(&CHARACTERS));
                column.chain.pop();
            }
            for position in 0..column.chain.len() {
                if self.rng.unit() < MUTATION_RATE {
                    column.chain[position] = self.rng.choice(&CHARACTERS);
                }
            }
            column.head += column.speed;
            if column.head - i32::try_from(column.chain.len()).unwrap_or(0) >= height {
                let occupied: Vec<u16> = self.columns.iter().map(|other| other.x).collect();
                let free: Vec<u16> = slots
                    .iter()
                    .copied()
                    .filter(|slot| !occupied.contains(slot))
                    .collect();
                let x = if free.is_empty() {
                    column.x
                } else {
                    let pick = usize::try_from(self.rng.below(free.len() as u64)).unwrap_or(0);
                    free[pick]
                };
                column = self.column(x, false);
            }
            self.columns[index] = column;
        }
        self.paint();
    }

    /// Consume *seconds* of the animation clock, stepping at the source rate.
    pub fn advance(&mut self, seconds: f64) {
        self.elapsed += seconds;
        while self.elapsed + 1e-9 >= STEP_SECONDS {
            self.elapsed -= STEP_SECONDS;
            self.step();
        }
    }

    /// Paint the rain into *area*, fading each level from *accent* to black.
    pub fn draw(&self, area: Rect, buffer: &mut Buffer, accent: Color) {
        if self.width != area.width || self.height != area.height {
            // A layout change repaints on the next step; a stale grid must never
            // be drawn at the wrong size.
            return;
        }
        for (row, (glyphs, levels)) in self.glyphs.iter().zip(&self.levels).enumerate() {
            for (column, glyph) in glyphs.iter().enumerate() {
                let level = levels.get(column).copied().unwrap_or(-1);
                if level < 0 || *glyph == ' ' {
                    continue;
                }
                let Ok(x) = u16::try_from(column) else {
                    continue;
                };
                let Ok(y) = u16::try_from(row) else {
                    continue;
                };
                let x = area.x + x;
                let y = area.y + y;
                if x >= area.right() || y >= area.bottom() {
                    continue;
                }
                let colour = fade(accent, level);
                if let Some(cell) = buffer.cell_mut((x, y)) {
                    cell.set_char(*glyph).set_style(Style::default().fg(colour));
                }
                // Cover the wide glyph's second cell so a shorter resident is gone.
                if let Some(cell) = buffer.cell_mut((x + 1, y)) {
                    cell.set_char(' ').set_style(Style::default());
                }
            }
        }
    }
}

/// Blend *accent* toward black for fade *level*: 0 is the bright head.
fn fade(accent: Color, level: i32) -> Color {
    if level <= 0 {
        return accent;
    }
    // Level 16 is the dimmest tail, so the fade reaches black exactly there.
    let factor = (1.0 - f64::from(level) / (FADE_LEVELS + 1.0)).max(0.0);
    match accent {
        Color::Rgb(red, green, blue) => Color::Rgb(
            (f64::from(red) * factor) as u8,
            (f64::from(green) * factor) as u8,
            (f64::from(blue) * factor) as u8,
        ),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_grid_is_already_raining() {
        let rain = Rain::new(60, 20, 7);
        assert_eq!(rain.columns.len(), 27, "90% of 30 even columns");
        let painted = rain
            .levels
            .iter()
            .flatten()
            .filter(|level| **level >= 0)
            .count();
        assert!(
            painted > 0,
            "chains are visible at once, not after an intro"
        );
        assert!(rain.columns.iter().all(|column| column.x < 59));
    }

    #[test]
    fn the_same_seed_draws_the_same_rain() {
        let mut one = Rain::new(40, 12, 99);
        let mut other = Rain::new(40, 12, 99);
        one.advance(1.0);
        other.advance(1.0);
        assert_eq!(one.glyphs, other.glyphs);
        assert_eq!(one.levels, other.levels);
    }

    #[test]
    fn chains_move_down_and_respawn() {
        let mut rain = Rain::new(40, 12, 5);
        let before: Vec<i32> = rain.columns.iter().map(|column| column.head).collect();
        rain.advance(0.5);
        let after: Vec<i32> = rain.columns.iter().map(|column| column.head).collect();
        assert_ne!(before, after, "half a second is several steps");
        assert!(after.iter().all(|head| *head > -13), "no chain drifts away");
        rain.advance(60.0);
        assert!(rain.columns.iter().all(|column| column.x < 39));
    }

    #[test]
    fn fewer_than_a_step_paints_nothing_new() {
        let mut rain = Rain::new(30, 8, 3);
        let before = rain.glyphs.clone();
        rain.advance(0.01);
        assert_eq!(before, rain.glyphs);
    }

    #[test]
    fn the_head_is_brighter_than_the_tail() {
        let accent = Color::Rgb(0, 255, 65);
        assert_eq!(fade(accent, 0), accent);
        assert_eq!(fade(accent, 16), Color::Rgb(0, 0, 0));
        match fade(accent, 8) {
            Color::Rgb(_, green, _) => assert!(green < 255, "the tail is dimmer"),
            other => panic!("expected rgb, got {other:?}"),
        }
    }

    #[test]
    fn a_resized_grid_keeps_chains_inside_the_new_bounds() {
        let mut rain = Rain::new(60, 20, 11);
        rain.resize(20, 6);
        assert_eq!(rain.glyphs.len(), 6);
        assert!(rain.columns.iter().all(|column| column.x < 19));
        rain.advance(0.5);
        assert!(rain.columns.iter().all(|column| column.x < 19));
    }
}
