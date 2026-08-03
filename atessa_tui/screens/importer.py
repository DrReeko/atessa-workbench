"""Paste-in importer for Atessa quota weights.

Layout-tolerant by design: a deterministic parser runs first, and only if it
comes up short does the vision/text model get a turn. Whatever the source, the
result is validated against the live catalog before anything is stored, because
a silently wrong credit cost is worse than no credit cost.
"""
from __future__ import annotations

import json
import re

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TextArea

from ..weights import (
    check_alignment,
    load_weights,
    parse_rows,
    refresh,
    save_weights,
    validate_weights,
)

STEPS = (
    "1. Open https://atessa.top/models while signed in\n"
    "2. Select the whole page and copy it\n"
    "3. Paste below, then press Import"
)

EXTRACT_PROMPT = (
    "You are reading a copy of the Atessa models page. For every model row, "
    "extract the model id, its credits-per-request cost, and its context window.\n"
    "Rules:\n"
    "- The cost is the Cost column, rendered like '×7×' or 'Free'. 'Free' means 0.\n"
    "- The context is the window shown on the same row, like '1M' or '400K'. "
    "Report it as a plain integer token count (1M -> 1000000).\n"
    "- Never use a timestamp, tokens/sec figure, or vendor count as either value.\n"
    "- Copy ids exactly as written.\n"
    'Reply with ONLY a JSON object: {"model-id": {"credits": 7, "context": 1000000}}. '
    "No prose, no markdown.\n\n"
)


class WeightsImportScreen(ModalScreen[int]):
    """Turns a copy of the models page into stored credit costs."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+s", "import_weights", "Import", show=False),
    ]

    def __init__(
        self,
        api=None,
        catalog: list[str] | None = None,
        api_context: dict[str, int | None] | None = None,
    ) -> None:
        super().__init__()
        self._api = api
        self._catalog = catalog or []
        self._api_context = api_context or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="import-dialog"):
            with Horizontal(id="import-head"):
                yield Label("Import credit costs", id="import-title")
                yield Button("Close  Esc", id="import-close")
            yield Static(STEPS, id="import-steps")
            yield TextArea(id="import-paste")
            yield Static("", id="import-result")
            with Horizontal(id="import-actions"):
                yield Button("Import", variant="primary", id="import-go")

    def on_mount(self) -> None:
        stored = len(self._safe_load())
        if stored:
            self.query_one("#import-result", Static).update(
                f"{stored} models already imported · pasting again replaces them"
            )
        self.query_one("#import-paste", TextArea).focus()

    def _safe_load(self) -> dict[str, float]:
        try:
            return load_weights()
        except ValueError:
            return {}

    def action_close(self) -> None:
        self.dismiss(0)

    def action_import_weights(self) -> None:
        text = self.query_one("#import-paste", TextArea).text
        result = self.query_one("#import-result", Static)
        if not text.strip():
            result.update("[b]Nothing pasted yet.[/b]  Copy the models page first.")
            return

        rows = parse_rows(text)
        clean, problems = validate_weights(
            {model: weight for model, (weight, _) in rows.items()}, self._catalog
        )
        agreed, checked, mismatches = check_alignment(rows, self._api_context)
        expected = len(self._catalog) if self._catalog else 0

        # Trust the parser only when the page's own context column agrees with
        # the live catalog; that is what proves the rows were read in order.
        trustworthy = checked >= 3 and agreed >= checked * 0.9

        if clean and trustworthy:
            # A short but verified parse means the copy was cut off, not
            # misread — keep it and say what is missing rather than guessing.
            missing = expected - len(clean) if expected else 0
            note = f"page text · {agreed}/{checked} rows verified"
            if missing > 0:
                note += f" · {missing} model(s) not in the paste"
            self._store(clean, problems, note)
            return

        if self._api is not None:
            reason = (
                "Layout not recognised" if not clean
                else f"Only {agreed}/{checked} rows matched the live catalog"
            )
            result.update(f"{reason} — asking the model to read it…")
            self._ai_extract(text, mismatches)
            return

        if not clean:
            result.update(
                "[b]No models found in that paste.[/b]  Copy the whole page, "
                "including the Cost column."
            )
            return
        self._store(clean, problems, "page text (unverified)")

    @work(exclusive=True, group="weights-ai")
    async def _ai_extract(self, text: str, mismatches: list[str]) -> None:
        result = self.query_one("#import-result", Static)
        try:
            answer = await self._api.chat(
                [{"role": "user", "content": EXTRACT_PROMPT + text}],
                model=self._api.cfg.model_for("power"),
                max_tokens=4000,
            )
        except Exception as error:
            result.update(f"[b]Could not read that paste:[/b] {error}")
            return

        match = re.search(r"\{.*\}", answer, re.S)
        if not match:
            result.update("[b]Could not read that paste.[/b]  Check it copied fully.")
            return
        try:
            raw = json.loads(match.group(0))
        except (ValueError, TypeError):
            result.update("[b]Could not read that paste.[/b]  Check it copied fully.")
            return

        # Same evidence standard as the parser: the model must also report the
        # context it saw, and that has to match the live catalog.
        rows: dict[str, tuple[float, int | None]] = {}
        for model, value in raw.items():
            if isinstance(value, dict):
                cost = value.get("credits", value.get("cost"))
                context = value.get("context")
            else:
                cost, context = value, None
            try:
                rows[str(model)] = (float(cost), int(context) if context else None)
            except (TypeError, ValueError):
                continue

        clean, problems = validate_weights(
            {model: weight for model, (weight, _) in rows.items()}, self._catalog
        )
        if not clean:
            result.update(
                "[b]Nothing usable found.[/b]  Copy the whole models page, "
                "including the Cost column."
            )
            return

        agreed, checked, _ = check_alignment(rows, self._api_context)
        if checked < 3 or agreed < checked * 0.9:
            result.update(
                f"[b]Read looks unreliable[/b] — only {agreed}/{checked} rows matched "
                "the live catalog (minimum 3 checkable rows required), so nothing was saved. Re-copy the page and retry."
            )
            return
        verified = f" · {agreed}/{checked} rows verified"
        self._store(clean, problems, f"model-assisted read{verified}")

    def _store(self, weights: dict[str, float], problems: list[str], source: str) -> None:
        # Merge, so a partial copy tops up earlier imports instead of losing them.
        merged = {**self._safe_load(), **weights}
        save_weights(merged)
        refresh()
        result = self.query_one("#import-result", Static)
        free = sum(1 for weight in merged.values() if weight == 0)
        costly = sorted(merged.items(), key=lambda item: -item[1])[:3]
        summary = ", ".join(f"{model} {weight:g}×" for model, weight in costly)
        lines = [
            f"[b]{len(weights)} models read[/b] via {source}.",
            f"{len(merged)} stored in total ({free} free) · most expensive: {summary}",
        ]
        if problems:
            lines.append("[dim]" + " · ".join(problems[:2]) + "[/dim]")
        result.update("\n".join(lines))
        self.notify(f"Credit costs stored for {len(merged)} models")
        self.dismiss(len(merged))

    @on(Button.Pressed, "#import-go")
    def _import_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_import_weights()

    @on(Button.Pressed, "#import-close")
    def _close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(0)
