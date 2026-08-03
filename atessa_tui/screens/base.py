"""Shared production contract for Atessa workbench panes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.markup import escape
from textual.widgets import Button, Input, Label, Markdown, Static, TextArea

from rich.table import Table
from rich.text import Text

from ..capabilities import capability_rows


@dataclass(frozen=True)
class ToolMeta:
    key: str
    title: str
    purpose: str
    group: str
    role: str = ""
    action: str = "Run"
    input_label: str = ""
    output_label: str = ""
    examples: tuple[tuple[str, str], ...] = ()
    avoid: str = ""
    request_estimate: str = "0"

    def __post_init__(self) -> None:
        if len(self.examples) != 3:
            raise ValueError(f"{self.key}: ToolMeta requires exactly three guide examples")


class PageHead(Vertical):
    """Two labelled lines: the tool's purpose, then its input/output and cost."""

    def __init__(self, meta: ToolMeta) -> None:
        super().__init__(classes="pane-head")
        self.meta = meta

    def compose(self) -> ComposeResult:
        yield Label(
            f"[b #9bb7ff]Purpose:[/]   {escape(self.meta.purpose)}", classes="purpose"
        )
        yield Static(
            f"[b #9bb7ff]Flow:[/]      {escape(self.meta.input_label)}"
            f"  →  {escape(self.meta.output_label)}"
            f"    ·    Req.Estim = {escape(self.meta.request_estimate)}",
            classes="flow pane-flow",
        )


class CapabilityStrip(Static):
    """Bottom band: why this tool helps, its best use, and any missing prereqs."""

    LABEL_WIDTH = 14

    def __init__(self, key: str) -> None:
        super().__init__("", id=f"cap-{key}", classes="capability-strip", markup=False)
        self.key = key

    def on_mount(self) -> None:
        self.refresh_capabilities()

    def refresh_capabilities(self) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=self.LABEL_WIDTH, no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        for label, body in capability_rows(self.key):
            table.add_row(
                Text(f"{label}:", style="bold #9bb7ff"),
                Text(body, style="#7d8fa1"),
            )
        self.update(table)


class GuideModal(ModalScreen[str | None]):
    """Consistent per-tool explainer with exactly three loadable examples."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("?", "close", "Close", show=False),
    ]

    def __init__(self, meta: ToolMeta) -> None:
        super().__init__()
        self.meta = meta

    def compose(self) -> ComposeResult:
        with Vertical(id="guide-dialog"):
            with Horizontal(id="guide-head"):
                yield Label(f"{self.meta.title.upper()} · GUIDE", id="guide-title")
                yield Button("×", id="guide-close")
            yield Markdown(
                f"**Problem solved**  {self.meta.purpose}\n\n"
                f"**Provide**  {self.meta.input_label}\n\n"
                f"**Receive**  {self.meta.output_label}\n\n"
                f"**Do not use when**  {self.meta.avoid}",
                id="guide-copy",
            )
            yield Label("LOAD AN EXAMPLE", classes="field-label")
            for index, (title, value) in enumerate(self.meta.examples, 1):
                yield Button(
                    f"{index}  {title}  —  {value}",
                    id=f"guide-example-{index}",
                    classes="example-button",
                )
            yield Static("Click an example to load it · Esc closes", classes="guide-hint")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#guide-close")
    def close_guide(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, ".example-button")
    def choose_example(self, event: Button.Pressed) -> None:
        index = int(event.button.id.rsplit("-", 1)[1]) - 1
        self.dismiss(self.meta.examples[index][1])


def _widget_text(widget) -> str:
    """Best-effort plain text for any result widget, for copying."""
    from textual.widgets import DataTable, Log, RichLog

    if isinstance(widget, TextArea):
        return widget.text
    if isinstance(widget, Markdown):
        return getattr(widget, "_markdown", "") or ""
    if isinstance(widget, (RichLog, Log)):
        return "\n".join(
            line.text if hasattr(line, "text") else str(line) for line in widget.lines
        )
    if isinstance(widget, DataTable):
        rows: list[str] = []
        header = [str(col.label) for col in widget.columns.values()]
        if header:
            rows.append("\t".join(header))
        for row_key in widget.rows:
            cells = widget.get_row(row_key)
            rows.append("\t".join(str(cell) for cell in cells))
        return "\n".join(rows)
    renderable = getattr(widget, "renderable", None)
    if renderable is not None:
        from rich.text import Text

        if isinstance(renderable, Text):
            return renderable.plain
        return str(renderable)
    return ""


class ToolPane(Container):
    """Base for an API-backed tool mounted in the persistent ContentSwitcher."""

    META: ClassVar[ToolMeta]
    EXAMPLE_SELECTOR: ClassVar[str] = ""
    RESULT_SELECTOR: ClassVar[str] = ""

    def __init__(self) -> None:
        super().__init__(id=f"pane-{self.META.key}")

    @property
    def api(self):
        return self.app.api

    @property
    def cfg(self):
        return self.app.cfg

    def model_for(self, role: str) -> str:
        return self.cfg.model_for(role)

    def compose(self) -> ComposeResult:
        yield PageHead(self.META)
        yield from self.compose_body()
        yield CapabilityStrip(self.META.key)

    def compose_body(self) -> ComposeResult:
        raise NotImplementedError

    def result_text(self) -> str:
        """Return this pane's current result as plain text, for the clipboard."""
        if not self.RESULT_SELECTOR:
            return ""
        for selector in self.RESULT_SELECTOR.split(","):
            try:
                target = self.query_one(selector.strip())
            except Exception:
                continue
            text = _widget_text(target)
            if text.strip():
                return text
        return ""

    def run_primary(self) -> None:
        """Dispatch the pane's primary action; concrete panes override this."""

    def load_example(self, value: str) -> None:
        if not self.EXAMPLE_SELECTOR:
            return
        try:
            target = self.query_one(self.EXAMPLE_SELECTOR)
        except Exception:
            return
        if isinstance(target, TextArea):
            target.text = value
        elif isinstance(target, Input):
            target.value = value
        else:
            return
        target.focus()

    def record(self, status: str, detail: str, model: str = "") -> None:
        self.app.record_activity(self.META.key, status, detail, model)

