"""Comparison panes: judged model council, live benchmark, and persistent blind arena."""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from pathlib import Path


from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
    Markdown,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from .. import capabilities
from ..api import ApiError
from ..config import ROLE_KEYS, atomic_write_json, get_atessa_dir
from ..metering import estimate_tokens, request_credits
from ..spend import record_models
from ..weights import format_credits, selection_cost, weight_for
from .base import ToolMeta, ToolPane

_ARENA_LOCK = threading.Lock()


def get_arena_path() -> Path:
    return get_atessa_dir() / "arena.json"
K = 32
DEFAULT_BENCH_PROMPT = "Explain what a mutex is in one paragraph."
# A run at or above this many credits gets called out before it is started.
EXPENSIVE_RUN = 25
ARENA_MAX_WEIGHT = 7.0
ARENA_RANDOM = ("__random__",)  # ModelPickerScreen sentinel: pick a random arena pair


def arena_eligible_models(models: list[str]) -> list[str]:
    """Return models with an imported per-request cost at or below the arena cap."""
    return [
        model
        for model in models
        if (weight := weight_for(model)) is not None and weight <= ARENA_MAX_WEIGHT
    ]


def describe_run_cost(models: list[str], unit: str) -> str:
    """One line telling the user what pressing the button will cost."""
    if not models:
        return "[dim]No models selected.[/dim]"
    total, unknown, priced = selection_cost(models)
    if not priced and unknown:
        return (
            f"[dim]{len(models)} models · credit costs not imported "
            "(Models → Credit costs)[/dim]"
        )
    unit_word = "credit" if total == 1 else "credits"
    parts = [f"{format_credits(total)} {unit_word} {unit}"]
    parts.append(f"{len(models)} model" + ("" if len(models) == 1 else "s"))
    if unknown:
        parts.append(f"{unknown} with unknown cost")
    line = " · ".join(parts)
    if priced and priced[0][1] >= EXPENSIVE_RUN:
        model, weight = priced[0]
        return f"[b]⚠ {line}[/b] — {model} alone is {weight:g}×"
    if total >= EXPENSIVE_RUN:
        return f"[b]⚠ {line}[/b]"
    return f"[b]{line}[/b]"


def _picker_label(model: str) -> str:
    """Keep dense model choices scanable; hover exposes the exact model ID."""
    return model if len(model) <= 27 else f"{model[:26]}…"


def _picker_row(model: str, selected: bool) -> str:
    """A checkable row: visible glyph, escaped name, dim cost badge."""
    mark = "☑" if selected else "☐"
    cost = weight_for(model)
    badge = f"  [dim]{cost:g}×[/]" if cost else ""
    return f"{mark}  {escape(model)}{badge}"


class ModelPickerScreen(ModalScreen[tuple[str, ...] | None]):
    """Pop-up model chooser: filter-as-you-type, Space toggles, Ctrl+Enter confirms.

    Dismisses with the chosen model IDs in catalog order, or None when
    cancelled. When show_random is set, a "Random pair" button dismisses
    with ARENA_RANDOM instead.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("space", "toggle", "Toggle"),
        Binding("ctrl+enter", "confirm", "Done"),
    ]

    def __init__(
        self,
        *,
        title: str,
        models: list[str],
        selected: list[str] | tuple[str, ...] = (),
        min_count: int = 0,
        max_count: int = 0,
        show_random: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._models = list(models)
        self._selected = set(selected).intersection(models)
        self._min = min_count
        self._max = max_count
        self._show_random = show_random

    DEFAULT_CSS = """
    ModelPickerScreen {
        align: center middle;
        background: $background 60%;
    }
    ModelPickerScreen #picker-card {
        width: 80;
        max-width: 96%;
        height: auto;
        max-height: 88%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    ModelPickerScreen #picker-title {
        text-style: bold;
        width: 100%;
    }
    ModelPickerScreen #picker-subtitle {
        color: $text-muted;
        width: 100%;
        margin-bottom: 1;
    }
    ModelPickerScreen #picker-filter { margin-bottom: 1; }
    ModelPickerScreen #picker-list {
        width: 100%;
        height: auto;
        max-height: 22;
        border: blank;
        background: $surface;
    }
    ModelPickerScreen #picker-footer {
        height: 3;
        margin-top: 1;
        border-top: solid $panel;
        padding-top: 1;
    }
    ModelPickerScreen #picker-count {
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
    }
    ModelPickerScreen #picker-footer Button {
        min-width: 10;
        margin-left: 1;
    }
    """

    def _subtitle(self) -> str:
        hints = (
            "↑↓ move · [b]Space[/b] toggles · [b]Ctrl+Enter[/b] confirms · "
            "[b]Esc[/b] cancels"
        )
        if self._min == 2 and self._max == 2:
            hints += " · choose exactly 2"
        elif self._max == 1:
            hints += " · choose one"
        return hints

    def compose(self) -> ComposeResult:
        with Container(id="picker-card"):
            yield Static(self._title, id="picker-title")
            yield Static(self._subtitle(), id="picker-subtitle")
            yield Input(
                placeholder=f"Type to filter {len(self._models)} models…",
                id="picker-filter",
            )
            yield OptionList(id="picker-list")
            with Horizontal(id="picker-footer"):
                yield Static("", id="picker-count")
                if self._max != 1:
                    yield Button("Select all", id="picker-all")
                yield Button("Clear", id="picker-clear")
                if self._show_random:
                    yield Button("Random pair", id="picker-random")
                yield Button("Cancel", id="picker-cancel")
                yield Button("Done", variant="primary", id="picker-done")

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#picker-filter", Input).focus()

    def _filtered(self) -> list[str]:
        needle = self.query_one("#picker-filter", Input).value.strip().casefold()
        if not needle:
            return self._models
        return [model for model in self._models if needle in model.casefold()]

    def _refresh(self) -> None:
        """Rebuild rows; keep the highlight parked on the same model."""
        listing = self.query_one("#picker-list", OptionList)
        highlighted_model: str | None = None
        if listing.highlighted is not None and listing.highlighted < listing.option_count:
            highlighted_model = listing.get_option_at_index(listing.highlighted).id
        options = [
            Option(_picker_row(model, model in self._selected), id=model)
            for model in self._filtered()
        ]
        listing.clear_options()
        listing.add_options(options)
        if highlighted_model is not None:
            for index, option in enumerate(options):
                if option.id == highlighted_model:
                    listing.highlighted = index
                    listing.scroll_to_highlight()
                    break
        if options and listing.highlighted is None:
            listing.highlighted = 0
            listing.scroll_to_highlight()
        self._refresh_footer()
    def _refresh_footer(self) -> None:
        count = len(self._selected)
        valid = (not self._min or count >= self._min) and (
            not self._max or count <= self._max
        )
        self.query_one("#picker-done", Button).disabled = not valid
        if self._min == 2 and self._max == 2:
            label = f"{count} of 2 chosen" + (" ✓" if count == 2 else "")
        else:
            label = f"{count} selected"
        self.query_one("#picker-count", Static).update(label)
    def _toggle(self, model: str) -> None:
        if self._max == 1:
            self._selected = {model}
        elif model in self._selected:
            self._selected.discard(model)
        else:
            self._selected.add(model)
        self._refresh()

    @on(OptionList.OptionSelected, "#picker-list")
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            self._toggle(event.option_id)

    @on(Input.Changed, "#picker-filter")
    def _filter_changed(self) -> None:
        self._refresh()

    @on(Button.Pressed, "#picker-all")
    def _select_all(self, event: Button.Pressed) -> None:
        event.stop()
        self._selected = set(self._filtered())
        self._refresh()

    @on(Button.Pressed, "#picker-clear")
    def _clear(self, event: Button.Pressed) -> None:
        event.stop()
        self._selected.clear()
        self._refresh()

    @on(Button.Pressed, "#picker-random")
    def _random_pair(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(ARENA_RANDOM)

    @on(Button.Pressed, "#picker-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#picker-done")
    def _done(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_confirm()

    def action_toggle(self) -> None:
        listing = self.query_one("#picker-list", OptionList)
        if listing.highlighted is not None and listing.highlighted < listing.option_count:
            model = listing.get_option_at_index(listing.highlighted).id
            if model is not None:
                self._toggle(model)

    def action_confirm(self) -> None:
        count = len(self._selected)
        if self._min and count < self._min:
            return
        if self._max and count > self._max:
            return
        self.dismiss(tuple(model for model in self._models if model in self._selected))

    def action_cancel(self) -> None:
        self.dismiss(None)

def _default_record() -> dict:
    return {"elo": 1000.0, "games": 0, "mu": 25.0, "sigma": 25.0 / 3}


def load_elo() -> dict[str, dict]:
    """Load and normalize local ratings; malformed existing state is an error."""
    path = get_arena_path()
    if not path.exists():
        return {}
    with _ARENA_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("arena state root must be an object")
            state: dict[str, dict] = {}
            for model, record in data.items():
                if not isinstance(record, dict):
                    raise ValueError(f"invalid arena record for {model}")
                elo = float(record.get("elo", 1000.0))
                games = int(record.get("games", 0))
                if games < 0:
                    raise ValueError(f"negative game count for {model}")
                entry = {"elo": elo, "games": games}
                # Ratings saved before openskill carry only ELO; seed mu from it.
                entry["mu"] = float(record.get("mu", 25.0 + (elo - 1000.0) / 40.0))
                entry["sigma"] = float(record.get("sigma", 25.0 / 3))
                if entry["sigma"] <= 0:
                    raise ValueError(f"invalid sigma for {model}")
                state[str(model)] = entry
            return state
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"cannot load {path}: {error}") from error


def save_elo(state: dict[str, dict]) -> None:
    """Persist arena ratings below the user's Atessa directory."""
    path = get_arena_path()
    with _ARENA_LOCK:
        atomic_write_json(path, state, indent=2)


def _update_openskill(record_a: dict, record_b: dict, score_a: float) -> bool:
    """Apply a Bayesian update in place. Returns False when unavailable."""
    try:
        from openskill.models import PlackettLuce
    except Exception:
        return False
    model = PlackettLuce()
    rating_a = model.rating(mu=record_a["mu"], sigma=record_a["sigma"])
    rating_b = model.rating(mu=record_b["mu"], sigma=record_b["sigma"])
    ranks = [1, 1] if score_a == 0.5 else ([1, 2] if score_a > 0.5 else [2, 1])
    [[new_a], [new_b]] = model.rate([[rating_a], [rating_b]], ranks=ranks)
    record_a["mu"], record_a["sigma"] = new_a.mu, new_a.sigma
    record_b["mu"], record_b["sigma"] = new_b.mu, new_b.sigma
    return True


def update_elo(
    state: dict[str, dict], model_a: str, model_b: str, score_a: float
) -> None:
    """Record one result, preferring Bayesian ratings over plain K=32 ELO."""
    record_a = state.setdefault(model_a, _default_record())
    record_b = state.setdefault(model_b, _default_record())
    record_a.setdefault("mu", 25.0)
    record_a.setdefault("sigma", 25.0 / 3)
    record_b.setdefault("mu", 25.0)
    record_b.setdefault("sigma", 25.0 / 3)

    rating_a = float(record_a["elo"])
    rating_b = float(record_b["elo"])
    expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400))
    record_a["elo"] = rating_a + K * (score_a - expected_a)
    record_b["elo"] = rating_b + K * ((1.0 - score_a) - (1.0 - expected_a))

    _update_openskill(record_a, record_b, score_a)
    record_a["games"] = int(record_a["games"]) + 1
    record_b["games"] = int(record_b["games"]) + 1


def rating_of(record: dict) -> float:
    """Conservative skill estimate when available, else raw ELO."""
    if capabilities.has("openskill") and "mu" in record and "sigma" in record:
        return float(record["mu"]) - 3.0 * float(record["sigma"])
    return float(record.get("elo", 1000.0))


class CouncilPane(ToolPane):
    """Ask multiple models concurrently, then synthesize with the power route."""

    META = ToolMeta(
        key="council",
        title="Council",
        purpose="Several models answer, then a judge rules. One defensible answer.",
        request_estimate="members + 1",
        group="compare",
        role="power",
        action="Ask council",
        input_label="Decision question",
        output_label="Verdict plus the model opinions",
        flow="Ask a question → models answer → judge picks a verdict",
        examples=(
            ("Architecture", "SQLite or Postgres for a local-first developer tool?"),
            ("Risk review", "What are the failure modes in this rollout plan?"),
            ("Trade-off", "Choose a Python TUI packaging strategy for Windows and Linux."),
        ),
        avoid="routine factual questions where several paid model calls add no value",
    )
    EXAMPLE_SELECTOR = "#council-prompt"
    RESULT_SELECTOR = "#council-results"

    DEFAULT_CSS = """
    CouncilPane Horizontal.input-row { height: 3; }
    CouncilPane Horizontal.input-row Input { width: 1fr; }
    CouncilPane Horizontal.input-row Button { width: auto; min-width: 16; }
    CouncilPane #council-picker-row { height: 3; margin-bottom: 1; }
    CouncilPane #council-selection {
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
        border: round $primary;
    }
    CouncilPane #council-choose { min-width: 16; margin-left: 1; }
    CouncilPane #council-judge-row { height: 3; margin-bottom: 1; }
    CouncilPane #council-judge {
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
        border: round $accent;
    }
    CouncilPane #council-choose-judge { min-width: 16; margin-left: 1; }
    CouncilPane VerticalScroll#council-results {
        width: 1fr;
        height: 1fr;
        min-height: 5;
        border: round $primary;
        padding: 0 1;
    }
    CouncilPane #council-verdict {
        background: $boost;
        border-bottom: solid $accent;
        padding: 0 1;
        margin-bottom: 1;
    }
    """
    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0
        self._models: list[str] = []
        self._selected: list[str] = []
        self._judge: str = ""


    def compose_body(self) -> ComposeResult:
        with Horizontal(classes="input-row"):
            yield Input(
                placeholder="Decision or question for the council",
                id="council-prompt",
            )
            yield Button("Ask council", id="council-go", variant="primary")
        with Horizontal(id="council-picker-row"):
            yield Static("Loading models…", id="council-selection")
            yield Button("Choose models…", id="council-choose")
        with Horizontal(id="council-judge-row"):
            yield Static(f"Judge: {escape(self.model_for('power'))}", id="council-judge")
            yield Button("Choose judge…", id="council-choose-judge")
        yield Static("", id="council-cost", classes="cost-preview")
        yield VerticalScroll(id="council-results")

    def on_mount(self) -> None:
        self._load_models()

    @work(exclusive=True, group="council-models")
    async def _load_models(self) -> None:
        try:
            models = await self.api.models()
        except ApiError as error:
            self.notify(f"models: {error}", severity="error")
            self.app.record_activity("council", "error", f"models: {error}")
            return
        if not self.is_mounted:
            return
        self._models = list(models)
        self._judge = self.model_for("power") if self.model_for("power") in models else ""
        defaults = [
            model
            for model in self._models
            if any(self.model_for(role) == model for role in ROLE_KEYS)
        ]
        self._set_selected(defaults)
        self._update_judge_label()
        self._update_cost()

    @on(Button.Pressed, "#council-choose")
    def _choose_models(self, event: Button.Pressed) -> None:
        event.stop()
        if not self._models:
            self.notify(
                "Model catalog not loaded yet — try again in a moment",
                severity="warning",
            )
            return
        self.app.push_screen(
            ModelPickerScreen(
                title="Choose council models",
                models=self._models,
                selected=self._selected,
            ),
            self._models_picked,
        )

    def _models_picked(self, result: tuple[str, ...] | None) -> None:
        if result is not None:
            self._set_selected(list(result))
            self._update_cost()

    @on(Button.Pressed, "#council-choose-judge")
    def _choose_judge(self, event: Button.Pressed) -> None:
        event.stop()
        if not self._models:
            self.notify(
                "Model catalog not loaded yet — try again in a moment",
                severity="warning",
            )
            return
        self.app.push_screen(
            ModelPickerScreen(
                title="Choose council judge",
                models=self._models,
                selected=(self._judge,) if self._judge else (),
                max_count=1,
            ),
            self._judge_picked,
        )

    def _judge_picked(self, result: tuple[str, ...] | None) -> None:
        if result is not None:
            self._judge = result[0] if result else ""
            self._update_judge_label()
            self._update_cost()

    def _update_judge_label(self) -> None:
        try:
            label = self.query_one("#council-judge", Static)
        except NoMatches:
            return
        judge = self._judge or self.model_for("power")
        suffix = "" if self._judge else " (power route fallback)"
        label.update(f"Judge: {escape(judge)}{suffix}")

    def _set_selected(self, models: list[str]) -> None:
        self._selected = [model for model in self._models if model in models]
        try:
            label = self.query_one("#council-selection", Static)
        except NoMatches:
            return
        if self._selected:
            names = escape(", ".join(self._selected))
            if len(names) > 72:
                names = names[:69] + "…"
            label.update(f"[b]{len(self._selected)}[/b] · {names}")
        else:
            label.update("[dim]No models chosen — use Choose models…[/dim]")

    def _update_cost(self) -> None:
        if not self.query("#council-cost"):
            return
        judge = self._judge or self.model_for("power")
        # The judge is one extra call on top of every member.
        self.query_one("#council-cost", Static).update(
            describe_run_cost(self._selected + [judge], "per council run")
        )
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "council-go":
            event.stop()
            self.run_primary()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "council-prompt":
            event.stop()
    def run_primary(self) -> None:
        prompt = self.query_one("#council-prompt", Input).value.strip()
        if not prompt:
            self.notify("Enter a prompt first", severity="warning")
            return
        models = list(self._selected)
        if not models:
            self.notify("Select at least one council model", severity="warning")
            return
        self._run_id += 1
        self._run_council(prompt, models, self._run_id)

    @work(exclusive=True, group="council-run")
    async def _run_council(self, prompt: str, models: list[str], run_id: int) -> None:
        if self._run_id != run_id:
            return
        results = self.query_one("#council-results", VerticalScroll)
        await results.remove_children()
        await results.mount(
            Markdown("*Council deliberating…*", id="council-verdict")
        )
        messages = [{"role": "user", "content": prompt}]

        async def ask(model: str) -> tuple[str, bool, str]:
            try:
                return model, True, await self.api.chat(messages, model=model)
            except Exception as error:
                return model, False, str(error)

        answers = await asyncio.gather(*(ask(model) for model in models))
        for model, succeeded, answer in answers:
            body = answer if succeeded else f"**error:** {answer}"
            await results.mount(
                Collapsible(Markdown(body), title=model, collapsed=True)
            )

        successful = [
            (model, answer)
            for model, succeeded, answer in answers
            if succeeded
        ]
        verdict = self.query_one("#council-verdict", Markdown)
        judge_model = self._judge or self.model_for("power")
        if not successful:
            credits, unknown = record_models("council", models)
            verdict.update("**Judge:** every council member failed; nothing to judge.")
            detail = f"all {len(models)} council members failed"
            self.app.record_activity(
                "council", "error", detail, judge_model,
                credits=credits, count_spend=False,
            )
            return

        sections = "\n\n".join(
            f"### Answer from `{model}`\n{answer}"
            for model, answer in successful
        )
        judge_prompt = (
            "You are judging a council of AI models. The original question was:\n\n"
            f"{prompt}\n\nHere are the successful answers:\n\n{sections}\n\n"
            "Write a short synthesis of the best combined answer, then rank the "
            "models from best to worst with a one-line justification for each."
        )
        try:
            judgment = await self.api.chat(
                [{"role": "user", "content": judge_prompt}], model=judge_model
            )
        except Exception as error:
            credits, unknown = record_models("council", models)
            verdict.update(f"**Judge ({judge_model}) failed:** {error}")
            self.app.record_activity(
                "council", "error", f"judge failed: {error}", judge_model,
                credits=credits, count_spend=False,
            )
            return
        spent_models = models + [judge_model]
        credits, unknown = record_models("council", spent_models)
        verdict.update(f"## Judge verdict ({judge_model})\n\n{judgment}")
        failed = len(models) - len(successful)
        self.app.record_activity(
            "council",
            "ok",
            f"{len(successful)}/{len(models)} members answered; {failed} failed"
            + (f"; {unknown} unpriced" if unknown else ""),
            judge_model,
            credits=credits,
            count_spend=False,
        )


class BenchPane(ToolPane):
    """Stream one prompt through selected models and report live timing."""

    META = ToolMeta(
        key="bench",
        title="Benchmark",
        purpose="Your real prompt across every model, ranked by speed and credit cost.",
        request_estimate="1/model",
        group="compare",
        role="",
        action="Race",
        input_label="Benchmark prompt",
        output_label="Speed and credit table",
        flow="Enter one prompt → models race → compare speed and cost",
        examples=(
            ("Short answer", "Explain optimistic concurrency in one paragraph."),
            ("Code task", "Write a Python LRU cache with type hints."),
            ("Structured", "Return three deployment risks as JSON."),
        ),
        avoid="treating a single transient speed run as a permanent quality ranking",
    )
    EXAMPLE_SELECTOR = "#bench-prompt"
    RESULT_SELECTOR = "#bench-table"

    DEFAULT_CSS = """
    BenchPane Horizontal.input-row { height: 3; }
    BenchPane Horizontal.input-row Input { width: 1fr; }
    BenchPane Horizontal.input-row Button { width: auto; min-width: 10; }
    BenchPane #bench-picker-row { height: 3; margin-bottom: 1; }
    BenchPane #bench-selection {
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
        border: round $primary;
    }
    BenchPane #bench-choose { min-width: 16; margin-left: 1; }
    BenchPane DataTable#bench-table {
        height: auto;
        min-height: 6;
        max-height: 16;
        border: round $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0
        self._models: list[str] = []
        self._selected: list[str] = []

    def compose_body(self) -> ComposeResult:
        with Horizontal(classes="input-row"):
            yield Input(
                value=DEFAULT_BENCH_PROMPT,
                placeholder="Prompt every selected model should answer…",
                id="bench-prompt",
            )
            yield Button("Race", id="bench-go", variant="primary")
        with Horizontal(id="bench-picker-row"):
            yield Static("Loading models…", id="bench-selection")
            yield Button("Choose models…", id="bench-choose")
        yield Static("", id="bench-cost", classes="cost-preview")
        yield DataTable(id="bench-table")

    def on_mount(self) -> None:
        table = self.query_one("#bench-table", DataTable)
        table.add_columns(
            "model", "TTFT s", "total s", "tok/s", "tokens", "credits", "output", "status"
        )
        self._load_models()

    @work(exclusive=True, group="bench-models")
    async def _load_models(self) -> None:
        try:
            models = await self.api.models()
        except ApiError as error:
            self.notify(f"models: {error}", severity="error")
            self.app.record_activity("bench", "error", f"models: {error}")
            return
        if not self.is_mounted:
            return
        self._models = list(models)
        self._set_selected([])
        self._update_cost()

    @on(Button.Pressed, "#bench-choose")
    def _choose_models(self, event: Button.Pressed) -> None:
        event.stop()
        if not self._models:
            self.notify(
                "Model catalog not loaded yet — try again in a moment",
                severity="warning",
            )
            return
        self.app.push_screen(
            ModelPickerScreen(
                title="Choose benchmark models",
                models=self._models,
                selected=self._selected,
            ),
            self._models_picked,
        )

    def _models_picked(self, result: tuple[str, ...] | None) -> None:
        if result is not None:
            self._set_selected(list(result))
            self._update_cost()

    def _set_selected(self, models: list[str]) -> None:
        self._selected = [model for model in self._models if model in models]
        try:
            label = self.query_one("#bench-selection", Static)
        except NoMatches:
            return
        if self._selected:
            names = escape(", ".join(self._selected))
            if len(names) > 72:
                names = names[:69] + "…"
            label.update(f"[b]{len(self._selected)}[/b] · {names}")
        else:
            label.update("[dim]No models chosen — use Choose models…[/dim]")

    def _update_cost(self) -> None:
        if not self.query("#bench-cost"):
            return
        self.query_one("#bench-cost", Static).update(
            describe_run_cost(self._selected, "per race")
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bench-go":
            event.stop()
            self.run_primary()
    def run_primary(self) -> None:
        prompt = self.query_one("#bench-prompt", Input).value.strip()
        if not prompt:
            self.notify("Enter a prompt first", severity="warning")
            return
        models = list(self._selected)
        if not models:
            self.notify("Select at least one model to benchmark", severity="warning")
            return
        self._run_id += 1
        self._run_bench(prompt, models, self._run_id)
    @work(exclusive=True, group="bench-run")
    async def _run_bench(self, prompt: str, models: list[str], run_id: int) -> None:
        if self._run_id != run_id:
            return
        table = self.query_one("#bench-table", DataTable)
        table.clear()
        rows = {
            model: table.add_row(
                model, "…", "…", "…", "…", "…", "", "queued", key=model
            )
            for model in models
        }
        messages = [{"role": "user", "content": prompt}]
        stats: dict[str, tuple[float, float, int, str, str]] = {}

        async def race(model: str) -> None:
            started = time.monotonic()
            ttft: float | None = None
            chars = 0
            text = ""
            self._set_bench_row(rows[model], "…", "…", "…", "…", "…", "", "streaming")
            try:
                async for chunk in self.api.chat_stream(messages, model=model):
                    now = time.monotonic()
                    if ttft is None:
                        ttft = now - started
                    chars += len(chunk)
                    text += chunk
                    total = now - started
                    tokens = estimate_tokens(text)
                    throughput = tokens / total if total > 0 else 0.0
                    self._set_bench_row(
                        rows[model],
                        f"{ttft:.2f}",
                        f"{total:.2f}",
                        f"{throughput:.1f}",
                        str(tokens),
                        format_credits(request_credits(model)),
                        text[:40].replace("\n", " "),
                        "streaming",
                    )
                total = time.monotonic() - started
                first_token = ttft if ttft is not None else float("inf")
                status = "ok" if ttft is not None else "empty"
                tokens = estimate_tokens(text) if text else 0
                stats[model] = (first_token, total, chars, text, status)
                throughput = tokens / total if total > 0 else 0.0
                self._set_bench_row(
                    rows[model],
                    f"{ttft:.2f}" if ttft is not None else "-",
                    f"{total:.2f}",
                    f"{throughput:.1f}",
                    str(tokens),
                    format_credits(request_credits(model)),
                    text[:40].replace("\n", " "),
                    status,
                )
            except Exception as error:
                stats[model] = (float("inf"), 0.0, -1, str(error), "error")
                self._set_bench_row(
                    rows[model], "error", "-", "-", "-", "-", str(error)[:40], "error"
                )

        await asyncio.gather(*(race(model) for model in models))

        table.clear()
        for model in sorted(models, key=lambda item: stats[item][0]):
            ttft, total, chars, text, status = stats[model]
            if chars < 0:
                table.add_row(model, "error", "-", "-", "-", "-", text[:40].replace("\n", " "), status)
                continue
            tokens = estimate_tokens(text) if text else 0
            throughput = tokens / total if total > 0 else 0.0
            table.add_row(
                model,
                f"{ttft:.2f}" if ttft != float("inf") else "-",
                f"{total:.2f}",
                f"{throughput:.1f}",
                str(tokens),
                format_credits(request_credits(model)),
                text[:40].replace("\n", " "),
                status,
            )
        failures = sum(1 for stat in stats.values() if stat[4] == "error")
        overall = "error" if failures == len(models) else "ok"
        # Atessa charges per request, even when a model returns an error.
        credits, unknown = record_models("bench", models)
        self.app.record_activity(
            "bench",
            overall,
            f"raced {len(models)} models; {failures} failed"
            + (f"; {unknown} unpriced" if unknown else ""),
            credits=credits,
            count_spend=False,
        )

    def _set_bench_row(
        self,
        row_key,
        ttft: str,
        total: str,
        throughput: str,
        tokens: str,
        cost: str,
        output: str,
        status: str,
    ) -> None:
        table = self.query_one("#bench-table", DataTable)
        columns = list(table.columns)
        try:
            for column, value in zip(
                columns[1:8], (ttft, total, throughput, tokens, cost, output, status)
            ):
                table.update_cell(row_key, column, value)
        except Exception:
            pass


class ArenaPane(ToolPane):
    """Run blind A/B rounds and persist local ELO ratings after each vote."""

    META = ToolMeta(
        key="arena",
        title="Arena",
        purpose="Blind A/B. Pick the better answer without seeing the brand.",
        request_estimate="2 per round",
        group="compare",
        role="",
        action="New round",
        input_label="Evaluation prompt",
        output_label="Blind A/B result and ratings",
        flow="Ask a question → compare two hidden answers → vote",
        examples=(
            ("Reasoning", "Design idempotency for a webhook consumer."),
            ("Writing", "Explain database indexes to a junior developer."),
            ("Code review", "Review this retry loop for subtle bugs."),
        ),
        avoid="speed comparisons; use Benchmark when latency is the criterion",
    )
    EXAMPLE_SELECTOR = "#arena-prompt"
    RESULT_SELECTOR = "#arena-board"
    BINDINGS = [
        ("1", "vote('a')", "Vote A"),
        ("2", "vote('b')", "Vote B"),
        ("t", "vote('tie')", "Tie"),
        ("n", "new_round", "New round"),
    ]

    DEFAULT_CSS = """
    ArenaPane Input#arena-prompt { height: 3; }
    ArenaPane #arena-pair-row { height: 3; }
    ArenaPane #arena-pair {
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
        border: round $primary;
    }
    ArenaPane #arena-pair-row Button { min-width: 13; margin-left: 1; }
    ArenaPane Horizontal#arena-buttons { height: 3; }
    ArenaPane Horizontal#arena-buttons Button {
        width: 1fr;
        min-width: 12;
    }
    ArenaPane Static#arena-status { height: auto; min-height: 1; color: $text-muted; }
    ArenaPane Horizontal#arena-panels { height: 1fr; min-height: 8; }
    ArenaPane VerticalScroll.arena-panel {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    ArenaPane DataTable#arena-board {
        height: auto;
        min-height: 4;
        max-height: 9;
        margin-top: 1;
        border: round $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.pair: tuple[str, str] | None = None
        self.answers: dict[str, str] = {}
        self.round_ready = False
        self.voted = True
        self.round_credits = 0.0
        self._run_id = 0
        self._pair_override: tuple[str, str] | None = None
        self._pair_catalog: list[str] = []
    def compose_body(self) -> ComposeResult:
        yield Input(placeholder="Prompt for a blind comparison", id="arena-prompt")
        with Horizontal(id="arena-pair-row"):
            yield Static("Pair: random each round", id="arena-pair")
            yield Button("Choose pair…", id="arena-choose")
            yield Button("Random pair", id="arena-random")
        with Horizontal(id="arena-buttons"):
            yield Button("New round [n]", id="arena-new", variant="primary")
            yield Button("Vote A [1]", id="arena-vote-a")
            yield Button("Vote B [2]", id="arena-vote-b")
            yield Button("Tie [t]", id="arena-vote-tie")
        yield Static("Enter a prompt to begin.", id="arena-status")
        with Horizontal(id="arena-panels"):
            with VerticalScroll(classes="arena-panel"):
                yield Markdown("## Model A\n\n*waiting*", id="arena-panel-a")
            with VerticalScroll(classes="arena-panel"):
                yield Markdown("## Model B\n\n*waiting*", id="arena-panel-b")
        yield DataTable(id="arena-board")

    def on_mount(self) -> None:
        board = self.query_one("#arena-board", DataTable)
        if capabilities.has("openskill"):
            board.add_columns("rank", "model", "rating", "±", "games")
        else:
            board.add_columns("rank", "model", "ELO", "games")
        self._load_catalog()
        self._refresh_board()
    def _refresh_board(self) -> None:
        board = self.query_one("#arena-board", DataTable)
        board.clear()
        try:
            ranked = sorted(load_elo().items(), key=lambda item: -rating_of(item[1]))
        except ValueError as error:
            self.query_one("#arena-status", Static).update(f"Ratings unavailable · {error}")
            return
        bayesian = capabilities.has("openskill")
        for rank, (model, record) in enumerate(ranked, 1):
            if bayesian:
                board.add_row(
                    str(rank),
                    model,
                    f"{rating_of(record):.1f}",
                    f"{float(record.get('sigma', 25.0 / 3)):.1f}",
                    str(int(record["games"])),
                )
            else:
                board.add_row(
                    str(rank), model, f"{float(record['elo']):.0f}", str(int(record["games"]))
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "arena-new": self.action_new_round,
            "arena-vote-a": lambda: self.action_vote("a"),
            "arena-vote-b": lambda: self.action_vote("b"),
            "arena-vote-tie": lambda: self.action_vote("tie"),
            "arena-choose": self.action_choose_pair,
            "arena-random": self.action_random_pair,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            event.stop()
            action()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "arena-prompt":
            event.stop()
            self.action_new_round()

    def run_primary(self) -> None:
        self.action_new_round()

    def action_new_round(self) -> None:
        prompt = self.query_one("#arena-prompt", Input).value.strip()
        if not prompt:
            self.notify("Enter an evaluation prompt first", severity="warning")
            return
        self.pair = None
        self.answers = {}
        self.round_ready = False
        self.voted = False
        self.round_credits = 0.0
        self._run_id += 1
        self._run_round(prompt, self._run_id)

    def action_choose_pair(self) -> None:
        if not self._pair_catalog:
            self.notify(
                "Model catalog not loaded yet — try again in a moment",
                severity="warning",
            )
            return
        self.app.push_screen(
            ModelPickerScreen(
                title="Choose arena pair",
                models=self._pair_catalog,
                selected=self._pair_override or (),
                min_count=2,
                max_count=2,
                show_random=True,
            ),
            self._pair_picked,
        )

    def action_random_pair(self) -> None:
        self._pair_override = None
        self.query_one("#arena-pair", Static).update("Pair: random each round")

    def _pair_picked(self, result: tuple[str, ...] | None) -> None:
        if result is None:
            return
        if result == ARENA_RANDOM:
            self.action_random_pair()
            return
        self._pair_override = (result[0], result[1])
        self.query_one("#arena-pair", Static).update(
            f"Pair: {escape(result[0])}  vs  {escape(result[1])}"
        )

    @work(exclusive=True, group="arena-catalog")
    async def _load_catalog(self) -> None:
        try:
            self._pair_catalog = await self.api.models()
        except ApiError:
            self._pair_catalog = []

    @work(exclusive=True, group="arena-round")
    async def _run_round(self, prompt: str, run_id: int) -> None:
        if self._run_id != run_id:
            return
        status = self.query_one("#arena-status", Static)
        status.update(f"Finding eligible models · max {ARENA_MAX_WEIGHT:g}×…")
        try:
            models = await self.api.models()
        except ApiError as error:
            if self._run_id == run_id:
                self.notify(f"models: {error}", severity="error")
                self.app.record_activity("arena", "error", f"models: {error}")
            return
        if self._run_id != run_id:
            return
        catalog_size = len(models)
        self._pair_catalog = models
        models = arena_eligible_models(models)
        if len(models) < 2:
            detail = (
                f"at least two models with imported cost ≤{ARENA_MAX_WEIGHT:g}× are required "
                "(Models → Credit costs)"
            )
            self.notify(detail, severity="error")
            self.app.record_activity("arena", "error", detail)
            return

        model_a, model_b = random.sample(models, 2)
        fell_back = False
        if self._pair_override:
            first, second = self._pair_override
            if first != second and first in models and second in models:
                model_a, model_b = first, second
            else:
                fell_back = True
        self.pair = (model_a, model_b)
        panel_a = self.query_one("#arena-panel-a", Markdown)
        panel_b = self.query_one("#arena-panel-b", Markdown)
        panel_a.update("## Model A\n\n*thinking…*")
        panel_b.update("## Model B\n\n*thinking…*")
        excluded = catalog_size - len(models)
        status.update(
            f"Round in progress — identities hidden · max {ARENA_MAX_WEIGHT:g}× "
            f"· {excluded} expensive/unpriced excluded."
            + (" Requested pair unavailable — random pair used." if fell_back else "")
            + " Vote 1 / 2 / t."
        )
        messages = [{"role": "user", "content": prompt}]

        async def ask(model: str) -> str:
            return await self.api.chat(messages, model=model)

        results = await asyncio.gather(
            ask(model_a), ask(model_b), return_exceptions=True
        )
        credits, unpriced = record_models("arena", [model_a, model_b])
        if self._run_id != run_id:
            return
        self.round_credits = credits
        if any(isinstance(result, Exception) for result in results):
            errors = "; ".join(
                str(result) for result in results if isinstance(result, Exception)
            )
            self.pair = None
            panel_a.update("## Model A\n\n*round failed*")
            panel_b.update("## Model B\n\n*round failed*")
            status.update("Round failed · both models must answer successfully")
            self.app.record_activity(
                "arena", "error", errors[:300], credits=credits, count_spend=False
            )
            return

        answer_a, answer_b = str(results[0]), str(results[1])
        if not answer_a.strip() or not answer_b.strip():
            self.pair = None
            status.update("Round failed · both answers must be non-empty")
            self.app.record_activity(
                "arena", "error", "empty arena answer", credits=credits, count_spend=False
            )
            return
        self.answers = {"a": answer_a, "b": answer_b}
        self.round_ready = True
        panel_a.update(f"## Model A\n\n{answer_a}")
        panel_b.update(f"## Model B\n\n{answer_b}")
        status.update(
            f"Vote: [1] A wins  [2] B wins  [t] tie · max {ARENA_MAX_WEIGHT:g}× "
            f"· {excluded} expensive/unpriced excluded"
        )

    def action_vote(self, choice: str) -> None:
        if choice not in {"a", "b", "tie"}:
            return
        if self.pair is None or not self.round_ready or self.voted:
            self.notify("No completed round awaiting a vote", severity="warning")
            return

        model_a, model_b = self.pair
        score_a = {"a": 1.0, "b": 0.0, "tie": 0.5}[choice]
        try:
            state = load_elo()
        except ValueError as error:
            self.notify(str(error), severity="error", timeout=10)
            self.record("error", str(error)[:300])
            return
        update_elo(state, model_a, model_b, score_a)
        try:
            save_elo(state)
        except OSError as error:
            self.notify(f"Could not save ELO: {error}", severity="error")
            self.app.record_activity(
                "arena", "error", f"saving ELO: {error}"
            )
            return

        self.voted = True
        marker_a = {"a": " — winner", "b": "", "tie": " — tie"}[choice]
        marker_b = {"a": "", "b": " — winner", "tie": " — tie"}[choice]
        self.query_one("#arena-panel-a", Markdown).update(
            f"## Model A: `{model_a}`{marker_a}\n\n{self.answers['a']}"
        )
        self.query_one("#arena-panel-b", Markdown).update(
            f"## Model B: `{model_b}`{marker_b}\n\n{self.answers['b']}"
        )
        total, unknown, _ = selection_cost([model_a, model_b])
        spent = (
            f" Cost {format_credits(total)} credits."
            if not unknown
            else ""
        )
        self.query_one("#arena-status", Static).update(
            f"Revealed: A={model_a}  B={model_b}.{spent} Press n for a new round."
        )
        self._refresh_board()
        self.app.record_activity(
            "arena", "ok", f"A={model_a} B={model_b} vote={choice}",
            credits=self.round_credits, count_spend=False,
        )
