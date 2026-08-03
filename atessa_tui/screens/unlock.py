"""Guided unlock screen: what is live, what is not, and the exact fix."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ..capabilities import (
    TOOL_CAPABILITIES,
    locked_capabilities,
    refresh,
    unlocked_count,
)


class UnlockScreen(ModalScreen[None]):
    """Shows live capability state with copyable install commands."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("u", "close", "Close", show=False),
        Binding("r", "recheck", "Re-check"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="unlock-dialog"):
            with Horizontal(id="unlock-head"):
                yield Label("Capabilities", id="unlock-title")
                yield Button("Close  Esc", id="unlock-close")
            yield Static("checking…", id="unlock-summary")
            with VerticalScroll(id="unlock-body"):
                yield Static("checking…", id="unlock-content")
            yield Static(
                "r re-checks after installing · Esc closes", id="unlock-hint"
            )

    def on_mount(self) -> None:
        self._render_capabilities()

    def _render_capabilities(self) -> None:
        available, total = unlocked_count()
        self.query_one("#unlock-summary", Static).update(
            f"{available} of {total} capabilities unlocked"
        )

        lines: list[str] = []
        for key, tool in TOOL_CAPABILITIES.items():
            if not tool.capabilities:
                continue
            unlocked = tool.unlocked()
            locked = tool.locked()
            state = f"{len(unlocked)}/{len(tool.capabilities)}"
            lines.append(f"[b]{key.upper()}[/b]  [dim]{state}[/dim]")
            for cap in unlocked:
                lines.append(f"  [green]●[/green] {cap.label}")
            for cap in locked:
                reason = cap.unsupported_note if not cap.supported() else ""
                suffix = f"  [dim]{reason}[/dim]" if reason else ""
                lines.append(f"  [red]○[/red] {cap.label}{suffix}")
            lines.append("")

        pending = [cap for cap in locked_capabilities() if cap.supported()]
        if pending:
            lines.append("[b]TO UNLOCK[/b]")
            seen: set[str] = set()
            for cap in pending:
                if cap.install in seen:
                    continue
                seen.add(cap.install)
                lines.append(f"  {cap.install}")
                if cap.note:
                    lines.append(f"    [dim]{cap.note}[/dim]")
            lines.append("")
            lines.append("[dim]Or install everything: pip install atessa-tui[full][/dim]")
        else:
            lines.append("[green]Everything available on this platform is unlocked.[/green]")

        self.query_one("#unlock-content", Static).update("\n".join(lines))

    def action_close(self) -> None:
        self.dismiss(None)

    def action_recheck(self) -> None:
        refresh()
        self._render_capabilities()
        self.notify("Re-checked capabilities")

    @on(Button.Pressed, "#unlock-close")
    def _close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
