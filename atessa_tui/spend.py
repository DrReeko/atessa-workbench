"""Running record of credits consumed, per day and per tool.

Atessa bills one request times the model's quota weight, so spend is counted in
requests — not tokens. Anything the workbench cannot price (no imported weight)
is counted separately rather than silently treated as free.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

from .config import atomic_write_json, get_atessa_dir
from .weights import weight_for

_SPEND_LOCK = threading.Lock()


def get_spend_path() -> Path:
    return get_atessa_dir() / "spend.json"
# Days of history to keep; enough for a monthly view without unbounded growth.
KEEP_DAYS = 45


def _load() -> dict:
    path = get_spend_path()
    if not path.exists():
        return {"days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except (OSError, ValueError):
        pass
    # Explicit recovery policy: isolate corrupt file so data is not silently lost
    try:
        corrupt_path = path.with_suffix(".json.corrupt")
        if path.exists():
            os.replace(path, corrupt_path)
    except OSError:
        pass
    return {"days": {}}


def _save(data: dict) -> None:
    days = data.get("days", {})
    for old in sorted(days)[:-KEEP_DAYS]:
        days.pop(old, None)
    path = get_spend_path()
    atomic_write_json(path, data, indent=2)

def record_models(tool: str, models: list[str]) -> tuple[float, int]:
    """Bank one request for every model in a fan-out operation."""
    total = 0.0
    unknown = 0
    with _SPEND_LOCK:
        for model in models:
            weight = _record_call_unlocked(tool, model)
            if weight is None:
                unknown += 1
            else:
                total += weight
    return total, unknown


def record_call(tool: str, model: str) -> float | None:
    """Bank one request against today's ledger. Returns its credit cost."""
    if not model:
        return None
    with _SPEND_LOCK:
        return _record_call_unlocked(tool, model)


def _record_call_unlocked(tool: str, model: str) -> float | None:
    if not model:
        return None
    weight = weight_for(model)
    data = _load()
    today = data["days"].setdefault(
        date.today().isoformat(), {"credits": 0.0, "requests": 0, "unpriced": 0, "tools": {}}
    )
    today["requests"] = today.get("requests", 0) + 1
    if weight is None:
        today["unpriced"] = today.get("unpriced", 0) + 1
    else:
        today["credits"] = round(today.get("credits", 0.0) + weight, 4)
        tools = today.setdefault("tools", {})
        tools[tool] = round(tools.get(tool, 0.0) + weight, 4)
    _save(data)
    return weight


def today_totals() -> dict:
    """Credits, requests, and per-tool split for the current day."""
    with _SPEND_LOCK:
        data = _load()
        return data["days"].get(
            date.today().isoformat(),
            {"credits": 0.0, "requests": 0, "unpriced": 0, "tools": {}},
        )


def recent_days(limit: int = 7) -> list[tuple[str, dict]]:
    """Most recent days, newest first."""
    with _SPEND_LOCK:
        data = _load()
        return [(day, data["days"][day]) for day in sorted(data["days"], reverse=True)[:limit]]

def top_tools(limit: int = 4) -> list[tuple[str, float]]:
    """Today's biggest credit consumers."""
    tools = today_totals().get("tools", {})
    return sorted(tools.items(), key=lambda item: -item[1])[:limit]
