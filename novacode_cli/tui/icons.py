"""Text-style icons at the boundary between agent events and terminal UI."""

from __future__ import annotations

# Fixed, single-cell forms that accept the active theme's Rich/CSS colors.
# Color emoji cannot be recolored reliably by terminal foreground styles.
_TEXT_ICONS = {
    "⚠": "!",
    "⚡": "◇",
    "⚙": "≡",
    "⚒": "⌘",
    "⏳": "◷",
    "⌛": "◷",
    "⏱": "◷",
    "⏸": "‖",
    "⏹": "■",
    "▶": "▸",
    "☑": "✓",
    "✅": "✓",
    "❌": "✕",
    "❗": "!",
    "❓": "?",
    "✨": "◇",
    "⭐": "◇",
    "🎯": "◇",
    "🎤": "♪",
    "🔊": "♫",
    "🔇": "♪",
    "💭": "∴",
    "🔁": "↻",
    "🔄": "↻",
    "🔔": "◆",
    "📎": "↳",
    "🔬": "⌕",
    "🔍": "⌕",
    "🔎": "⌕",
    "🧠": "◈",
    "📥": "↳",
    "📚": "≡",
    "📝": "✎",
    "✏": "✎",
    "🖊": "✎",
    "🔧": "⌘",
    "📋": "≡",
    "📡": "↗",
    "🔗": "↗",
    "📊": "≡",
    "🔑": "◇",
    "📭": "◇",
    "💾": "▣",
    "🟢": "●",
    "🔴": "✕",
    "🟡": "◷",
    "🤖": "◇",
    "📦": "▣",
    "📁": "▸",
    "📂": "▸",
    "📄": "▤",
    "📃": "▤",
    "🗂": "▤",
    "🛠": "⌘",
    "🛡": "▣",
    "🖼": "▣",
    "🌐": "◎",
    "🚀": "↗",
}
_PICTOGRAPHS = range(0x1F000, 0x1FB00)


def text_icon(icon: str) -> str:
    """Render event icon metadata without changing message or tool content.

    Keep existing text symbols and plugin labels. Unknown pictographic emoji
    receive a neutral marker so they cannot introduce a colored fallback font.
    """
    plain = icon.replace("\ufe0f", "").replace("\ufe0e", "")
    if plain in _TEXT_ICONS:
        return _TEXT_ICONS[plain]
    if any(ord(character) in _PICTOGRAPHS for character in plain):
        return "◇"
    return plain
