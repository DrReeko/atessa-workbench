"""Capability probing: what each tool can do right now, and how to unlock more.

Targets Windows and Linux first, with macOS supported where the underlying
package supports it. Every capability is optional: a missing package never
breaks a pane, it only downgrades the pane's advertised power and yields a
copyable install command.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from functools import lru_cache

WINDOWS = sys.platform == "win32" or platform.system() == "Windows"
MACOS = platform.system() == "Darwin"
LINUX = not WINDOWS and not MACOS


def wayland_session() -> bool:
    """Report whether this Linux session is Wayland rather than X11."""
    if not LINUX:
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


@dataclass(frozen=True)
class Capability:
    """One unlockable power belonging to a tool."""

    key: str
    label: str
    module: str = ""
    binary: str = ""
    install: str = ""
    note: str = ""
    platforms: tuple[str, ...] = ("windows", "linux", "macos")
    unsupported_note: str = ""

    def supported(self) -> bool:
        current = "windows" if WINDOWS else "macos" if MACOS else "linux"
        return current in self.platforms

    def available(self) -> bool:
        if not self.supported():
            return False
        if self.key == "mss" and wayland_session():
            return False
        if self.module and importlib.util.find_spec(self.module) is None:
            return False
        if self.binary and shutil.which(self.binary) is None:
            return False
        return True


@dataclass(frozen=True)
class ToolCapabilities:
    """The capability story for a single tool pane."""

    headline: str
    best_for: str = ""
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)

    def unlocked(self) -> list[Capability]:
        return [cap for cap in self.capabilities if cap.available()]

    def locked(self) -> list[Capability]:
        return [cap for cap in self.capabilities if not cap.available()]


_DDGS = Capability(
    key="ddgs",
    label="hard-to-reach coding sources",
    module="ddgs",
    install="pip install ddgs",
    note="Finds Reddit, support forums, DEV, and YouTube videos.",
)
_TRAFILATURA = Capability(
    key="trafilatura",
    label="offline article extraction",
    module="trafilatura",
    install="pip install trafilatura",
    note="Extracts the real article locally when reader services fail.",
)
_YOUTUBE = Capability(
    key="youtube",
    label="YouTube transcripts",
    module="youtube_transcript_api",
    install="pip install youtube-transcript-api",
    note="Reads a video's transcript instead of its page chrome.",
)
_YOUTUBE_COMMENTS = Capability(
    key="youtube_comments",
    label="YouTube comments",
    module="yt_dlp",
    install="pip install yt-dlp",
    note="Loads a small, bounded comment sample; slower and noisier than transcripts.",
)
_FEEDPARSER = Capability(
    key="feedparser",
    label="RSS / Atom feeds",
    module="feedparser",
    install="pip install feedparser",
    note="Turns a feed URL into a readable list of entries.",
)
_MSS = Capability(
    key="mss",
    label="region + monitor capture",
    module="mss",
    install="pip install mss",
    note="Fast in-process capture with region and monitor selection.",
    unsupported_note="Wayland session — grim handles capture here.",
)
_TIKTOKEN = Capability(
    key="tiktoken",
    label="exact token counts",
    module="tiktoken",
    install="pip install tiktoken",
    note="Counts real tokens instead of estimating from characters.",
)
_LITELLM = Capability(
    key="litellm",
    label="model capability metadata",
    module="litellm",
    install="pip install litellm",
    note="Flags which models accept images. Credit costs come from Atessa itself.",
)
_OPENSKILL = Capability(
    key="openskill",
    label="Bayesian ratings",
    module="openskill",
    install="pip install openskill",
    note="Rates models with uncertainty so one lucky win cannot top the board.",
)
_GITPYTHON = Capability(
    key="gitpython",
    label="branch + ahead/behind state",
    module="git",
    install="pip install GitPython",
    note="Shows branch, divergence, and conflict state beside the diff.",
)
_COMMITIZEN = Capability(
    key="commitizen",
    label="commit message validation",
    module="commitizen",
    install="pip install commitizen",
    note="Checks the drafted message against conventional-commit rules.",
)
_GIT_BINARY = Capability(
    key="git",
    label="local repository",
    binary="git",
    install="install Git and open a repository",
    note="Git itself must be on PATH to read diffs.",
)

TOOL_CAPABILITIES: dict[str, ToolCapabilities] = {
    "chat": ToolCapabilities(
        headline="One model has one set of blind spots; this swaps the brain answering you without losing the thread.",
        best_for="Ask a fast cheap model first, then re-ask the hard part of the same conversation to a stronger one.",
        capabilities=(_TIKTOKEN,),
    ),
    "search": ToolCapabilities(
        headline="Reddit, Stack Overflow, GitHub issues, forums and YouTube transcripts block agent fetches; this reaches them server-side.",
        best_for="A version-specific breakage where the only real fix is buried in an issue thread. Paste the exact error string.",
        capabilities=(_DDGS, _YOUTUBE, _YOUTUBE_COMMENTS),
    ),
    "read": ToolCapabilities(
        headline="JavaScript-heavy pages return an empty shell to a plain fetch; Readability strips the scaffolding and leaves the article.",
        best_for="Quoting documentation accurately, or reading a page that rendered blank everywhere else.",
        capabilities=(_TRAFILATURA, _YOUTUBE, _FEEDPARSER),
    ),
    "image": ToolCapabilities(
        headline="A text-only model cannot ship a site, deck or README that needs artwork.",
        best_for="Generating a matching asset set - repeat one style suffix per prompt, then verify each with View.",
    ),
    "view": ToolCapabilities(
        headline="Your model may have no vision, or the harness may never pass the image through; this gets eyes on a file you only have the path to.",
        best_for="A mockup, diagram or screenshot the user referenced - or verifying an image you just generated before you rely on it.",
    ),
    "shot": ToolCapabilities(
        headline="Lifts two limits at once: you cannot capture a screen, and you may not have vision. The user does nothing.",
        best_for="Reading an error dialog or checking what actually rendered, when asking the user to screenshot and paste would cost a round trip.",
        capabilities=(_MSS,),
    ),
    "council": ToolCapabilities(
        headline="Several models answer the same prompt and a judge rules, so you get one defensible answer instead of a coin flip.",
        best_for="An architecture or security call where being wrong is expensive and one opinion is not enough.",
    ),
    "bench": ToolCapabilities(
        headline="Published benchmarks are not your workload; this runs your real prompt across every model with live latency and cost.",
        best_for="Deciding which model to route a repeated task to, when speed and price matter as much as quality.",
        capabilities=(_TIKTOKEN, _LITELLM),
    ),
    "arena": ToolCapabilities(
        headline="Names bias judgement; blind A/B removes the brand and leaves only the answer.",
        best_for="Finding which model you genuinely prefer for your own writing or code, rather than the one you assume is best.",
        capabilities=(_OPENSKILL,),
    ),
    "explain": ToolCapabilities(
        headline="Reads the source file your traceback names off this machine, so the fix cites your real lines instead of a guess.",
        best_for="A traceback or compiler error you can paste whole - it grounds the diagnosis in the actual surrounding code.",
    ),
    "git": ToolCapabilities(
        headline="Reviews the diff you actually staged, so the message and the review describe real changes.",
        best_for="Turning a messy staged diff into a conventional commit, and catching a regression before you push.",
        capabilities=(_GIT_BINARY, _GITPYTHON, _COMMITIZEN),
    ),
    "shell": ToolCapabilities(
        headline="Translates an outcome into exactly one command for this OS, shown for approval before anything runs.",
        best_for="A command you would otherwise search for - rsync flags, ffmpeg arguments, or finding what holds a port.",
    ),
    "models": ToolCapabilities(
        headline="One place to route default, vision, power and image roles; a change here applies to every tool.",
        best_for="Pointing the expensive roles at a strong model and the chatty ones at a cheap model, once, persistently.",
        capabilities=(_LITELLM,),
    ),
    "activity": ToolCapabilities(
        headline="Every call this workbench made, replayable, with what it cost.",
        best_for="Finding which tool burned your credits, or recovering the output of a call you already closed.",
    ),
}


@lru_cache(maxsize=None)
def _cached_state(key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tool = TOOL_CAPABILITIES[key]
    return (
        tuple(cap.label for cap in tool.unlocked()),
        tuple(cap.label for cap in tool.locked()),
    )


def capability_rows(key: str) -> list[tuple[str, str]]:
    """Labelled (label, body) rows for the bottom band: payoff, best use, gaps."""
    tool = TOOL_CAPABILITIES.get(key)
    if tool is None:
        return []
    rows = [("Why it helps", tool.headline)]
    if tool.best_for:
        rows.append(("Best for", tool.best_for))
    _unlocked, locked = _cached_state(key)
    if locked:
        rows.append(("Missing", " · ".join(locked) + "  ·  press u to install"))
    return rows


def capability_strip(key: str) -> str:
    """Plain-text form of the bottom band."""
    return "\n".join(f"{label}:  {body}" for label, body in capability_rows(key))


def has(key: str) -> bool:
    """Report whether a single named capability is usable right now."""
    for tool in TOOL_CAPABILITIES.values():
        for cap in tool.capabilities:
            if cap.key == key:
                return cap.available()
    return False


def locked_capabilities() -> list[Capability]:
    """Every distinct locked capability, in stable tool order."""
    seen: set[str] = set()
    out: list[Capability] = []
    for tool in TOOL_CAPABILITIES.values():
        for cap in tool.locked():
            if cap.key not in seen:
                seen.add(cap.key)
                out.append(cap)
    return out


def unlocked_count() -> tuple[int, int]:
    """Return (available, total) across every distinct capability."""
    seen: dict[str, Capability] = {}
    for tool in TOOL_CAPABILITIES.values():
        for cap in tool.capabilities:
            seen.setdefault(cap.key, cap)
    total = len(seen)
    available = sum(1 for cap in seen.values() if cap.available())
    return available, total


def refresh() -> None:
    """Drop cached probe results after an unlock attempt."""
    _cached_state.cache_clear()
