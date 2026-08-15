"""Themable color palettes for the Atessa workbench.

The app.css palette is defined through CSS variables (``$canvas``, ``$ink``, …)
so a live theme switch only has to swap the active ``Theme`` and Textual
re-resolves every reference. Each palette here supplies every variable the
stylesheet references; anything it leaves out falls back to
``AtessaApp.get_theme_variable_defaults``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Every CSS variable referenced by app.tcss. Kept in one place so a new theme
# author knows the full surface they must colour.
VARIABLE_NAMES: tuple[str, ...] = (
    "canvas",
    "workspace",
    "surface",
    "surface-raised",
    "surface-hover",
    "sidebar",
    "sidebar-raised",
    "inspector",
    "line",
    "line-soft",
    "ink",
    "ink-strong",
    "muted",
    "quiet",
    "accent",
    "accent-ink",
    "blue",
    "warning",
    "danger",
    "success-surface",
    "band",
    "control-band",
    "result-surface",
)

# The current dark blueprint, used both as the startup defaults and as the
# reference every other palette is measured against.
DEFAULT_VARIABLES: dict[str, str] = {
    "canvas": "#070b11",
    "workspace": "#090e15",
    "surface": "#0e1620",
    "surface-raised": "#14202d",
    "surface-hover": "#1a2938",
    "sidebar": "#182431",
    "sidebar-raised": "#1d2b3a",
    "inspector": "#111b26",
    "line": "#2b3b4d",
    "line-soft": "#1c2a39",
    "ink": "#dbe4ed",
    "ink-strong": "#f0f5f9",
    "muted": "#8795a6",
    "quiet": "#627183",
    "accent": "#63d7c7",
    "accent-ink": "#071411",
    "blue": "#9bb7ff",
    "warning": "#e6bd73",
    "danger": "#ff8d8d",
    "success-surface": "#102c28",
    "band": "#0b121a",
    "control-band": "#101a26",
    "result-surface": "#16212e",
}


@dataclass(frozen=True)
class Palette:
    """A named colour scheme for the whole workbench."""

    name: str
    # Theme-level colours that seed Textual's $primary/$surface/$panel/…
    primary: str
    background: str
    foreground: str
    surface: str
    panel: str
    accent: str
    # The CSS variables app.tcss actually reads.
    variables: Mapping[str, str]


# Each palette restates only what differs from the dark blueprint; the rest is
# inherited through get_theme_variable_defaults. All are dark-friendly so the
# dense layout never loses contrast.
PALETTES: tuple[Palette, ...] = (
    Palette(
        name="Midnight",
        primary="#9bb7ff",
        background="#0a0e14",
        foreground="#c0caf5",
        surface="#0e141b",
        panel="#0c1118",
        accent="#63d7c7",
        variables={
            "canvas": "#0a0e14",
            "workspace": "#0c1118",
            "surface": "#0e141b",
            "surface-raised": "#131c26",
            "surface-hover": "#182331",
            "sidebar": "#0f1520",
            "sidebar-raised": "#141c29",
            "inspector": "#0d141e",
            "line": "#2a3a4d",
            "line-soft": "#1b2836",
            "ink": "#c0caf5",
            "ink-strong": "#e6edf7",
            "muted": "#93a3b8",
            "quiet": "#6c7c91",
            "accent": "#63d7c7",
            "accent-ink": "#04121b",
            "blue": "#9bb7ff",
            "warning": "#e6bd73",
            "danger": "#ff8d8d",
            "success-surface": "#0f2a26",
            "band": "#0c1219",
            "control-band": "#0f1824",
            "result-surface": "#141e2c",
        },
    ),
    Palette(
        name="Nord",
        primary="#88c0d0",
        background="#2e3440",
        foreground="#d8dee9",
        surface="#3b4252",
        panel="#343a46",
        accent="#8fbcbb",
        variables={
            "canvas": "#2e3440",
            "workspace": "#313744",
            "surface": "#3b4252",
            "surface-raised": "#434c5e",
            "surface-hover": "#4c566a",
            "sidebar": "#343a46",
            "sidebar-raised": "#3b4252",
            "inspector": "#333943",
            "line": "#4c566a",
            "line-soft": "#3f4757",
            "ink": "#d8dee9",
            "ink-strong": "#eceff4",
            "muted": "#a6b1c0",
            "quiet": "#8793a5",
            "accent": "#8fbcbb",
            "accent-ink": "#1f2c2c",
            "blue": "#81a1c1",
            "warning": "#ebcb8b",
            "danger": "#bf616a",
            "success-surface": "#2c3a37",
            "band": "#2f3542",
            "control-band": "#363d4c",
            "result-surface": "#414a5b",
        },
    ),
    Palette(
        name="Dracula",
        primary="#bd93f9",
        background="#282a36",
        foreground="#f8f8f2",
        surface="#343746",
        panel="#2f313e",
        accent="#50fa7b",
        variables={
            "canvas": "#282a36",
            "workspace": "#2b2d3a",
            "surface": "#343746",
            "surface-raised": "#3c3f50",
            "surface-hover": "#454a5d",
            "sidebar": "#2e303c",
            "sidebar-raised": "#353746",
            "inspector": "#2c2e3a",
            "line": "#454a5d",
            "line-soft": "#363a49",
            "ink": "#f8f8f2",
            "ink-strong": "#ffffff",
            "muted": "#bd93f9",
            "quiet": "#8b90a8",
            "accent": "#50fa7b",
            "accent-ink": "#08240f",
            "blue": "#8be9fd",
            "warning": "#f1fa8c",
            "danger": "#ff5555",
            "success-surface": "#1f3a26",
            "band": "#262833",
            "control-band": "#313443",
            "result-surface": "#383b4b",
        },
    ),
    Palette(
        name="Tokyo Night",
        primary="#7aa2f7",
        background="#1a1b26",
        foreground="#c0caf5",
        surface="#24283b",
        panel="#1f2233",
        accent="#7dcfff",
        variables={
            "canvas": "#1a1b26",
            "workspace": "#1d1f2b",
            "surface": "#24283b",
            "surface-raised": "#2b3044",
            "surface-hover": "#334057",
            "sidebar": "#1f2233",
            "sidebar-raised": "#262b40",
            "inspector": "#1e2131",
            "line": "#3b4261",
            "line-soft": "#2a3049",
            "ink": "#c0caf5",
            "ink-strong": "#e0e8ff",
            "muted": "#9aa5ce",
            "quiet": "#7a83a8",
            "accent": "#7dcfff",
            "accent-ink": "#04222e",
            "blue": "#7aa2f7",
            "warning": "#e0af68",
            "danger": "#f7768e",
            "success-surface": "#123f35",
            "band": "#181922",
            "control-band": "#21242f",
            "result-surface": "#272b40",
        },
    ),
    Palette(
        name="Aurora",
        primary="#63d7c7",
        background="#0b1020",
        foreground="#e4e8f2",
        surface="#111831",
        panel="#0e1428",
        accent="#63d7c7",
        variables={
            "canvas": "#0b1020",
            "workspace": "#0d1326",
            "surface": "#111831",
            "surface-raised": "#182142",
            "surface-hover": "#1f2a50",
            "sidebar": "#0f162c",
            "sidebar-raised": "#151d3a",
            "inspector": "#0e1428",
            "line": "#263a63",
            "line-soft": "#1a2647",
            "ink": "#e4e8f2",
            "ink-strong": "#f4f7fc",
            "muted": "#9fb0cf",
            "quiet": "#7484a8",
            "accent": "#63d7c7",
            "accent-ink": "#061a17",
            "blue": "#a5c8ff",
            "warning": "#ffd38a",
            "danger": "#ff8d9e",
            "success-surface": "#123a33",
            "band": "#0a0f20",
            "control-band": "#0f1730",
            "result-surface": "#15203c",
        },
    ),
)

# Display names -> internal theme identifiers. Kept in the same order as the
# cycle so the down-arrow key walks them left to right.
PALETTE_ORDER: tuple[str, ...] = tuple(palette.name for palette in PALETTES)

CYCLE_THEME_NAMES: tuple[str, ...] = PALETTE_ORDER
