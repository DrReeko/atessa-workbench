"""Exact production pane registry for the persistent workbench shell."""
from __future__ import annotations

from .compare import ArenaPane, BenchPane, CouncilPane
from .core import ChatPane, ModelsPane, ReadPane, SearchPane
from .dev import ActivityPane, CommandPane, ExplainPane, GitPane
from .media import ImagePane, ShotPane, VisionPane


PANES: tuple[type, ...] = (
    ChatPane,
    SearchPane,
    ReadPane,
    ImagePane,
    VisionPane,
    ShotPane,
    CouncilPane,
    BenchPane,
    ArenaPane,
    ExplainPane,
    GitPane,
    CommandPane,
    ModelsPane,
    ActivityPane,
)

PANE_BY_KEY = {pane.META.key: pane for pane in PANES}

NAV_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Create", ("chat", "search", "read", "image", "view", "shot")),
    ("Compare", ("council", "bench", "arena")),
    ("Develop", ("explain", "git", "shell")),
    ("System", ("models", "activity")),
)

# Single-cell glyphs (no Nerd Font needed - these live in fonts Windows, macOS and
# Linux terminals ship by default). Verified width 1 so they never break alignment.
TOOL_GLYPHS: dict[str, str] = {
    "chat": "◆",
    "search": "⌕",
    "read": "▤",
    "image": "▩",
    "view": "◉",
    "shot": "▣",
    "council": "❖",
    "bench": "⏱",
    "arena": "★",
    "explain": "‼",
    "git": "⑂",
    "shell": "❯",
    "models": "⬢",
    "activity": "▰",
}

# Compatibility name for callers that only need the visible group order.
GROUP_ORDER = tuple(group for group, _keys in NAV_GROUPS)

__all__ = [
    "ActivityPane",
    "ArenaPane",
    "BenchPane",
    "ChatPane",
    "CommandPane",
    "CouncilPane",
    "ExplainPane",
    "GitPane",
    "ImagePane",
    "ModelsPane",
    "NAV_GROUPS",
    "PANES",
    "PANE_BY_KEY",
    "TOOL_GLYPHS",
    "ReadPane",
    "SearchPane",
    "ShotPane",
    "VisionPane",
]
