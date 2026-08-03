"""Atessa quota weights: what a request actually costs.

Atessa meters usage in credits, not dollars: each model has a quota weight and
one request consumes that many credits. The weights are only shown on the
account-gated models page, so they are pasted in by the user and cached here.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .config import atomic_write_json, get_atessa_dir

_WEIGHTS_LOCK = threading.Lock()


def get_weights_path() -> Path:
    return get_atessa_dir() / "weights.json"
# A model id: lowercase start, no spaces, and at least one digit or dash.
_MODEL_ID = re.compile(r"^[a-z][a-z0-9._-]*[a-z0-9]$")
# "×7×", "x7x", "×6.5×", or a bare "7×" as the site renders the cost cell.
_WEIGHT = re.compile(r"^[×x]?\s*(\d+(?:\.\d+)?)\s*[×x]$|^[×x]\s*(\d+(?:\.\d+)?)\s*$")
_FREE = re.compile(r"^free$", re.IGNORECASE)


def _looks_like_model(token: str) -> bool:
    if not _MODEL_ID.match(token) or len(token) < 3:
        return False
    return bool(re.search(r"[\d-]", token))


def _weight_of(token: str) -> float | None:
    if _FREE.match(token):
        return 0.0
    match = _WEIGHT.match(token)
    if not match:
        return None
    return float(match.group(1) or match.group(2))


def _context_of(token: str) -> int | None:
    """'1M' / '400K' / '976K' as a token count."""
    match = re.match(r"^(\d+(?:\.\d+)?)([MK])$", token)
    if not match:
        return None
    scale = 1_000_000 if match.group(2) == "M" else 1_000
    return int(float(match.group(1)) * scale)


def parse_models_page(text: str) -> dict[str, float]:
    """Pull model -> credits-per-request out of a pasted models page."""
    return {model: row[0] for model, row in parse_rows(text).items()}


def parse_rows(text: str) -> dict[str, tuple[float, int | None]]:
    """Pull model -> (credits, context) so the result can be self-checked.

    The context column is the corroborating signal: it comes from the same row
    as the cost, so if it matches the live catalog the row was read correctly.
    """
    tokens: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # A row may arrive whole ("claude-opus-5 ... ×7× 1M OK") or split.
        tokens.extend(part for part in stripped.split() if part)

    rows: dict[str, tuple[float, int | None]] = {}
    pending: str | None = None
    awaiting_context: str | None = None
    for token in tokens:
        if awaiting_context is not None:
            context = _context_of(token)
            if context is not None:
                weight = rows[awaiting_context][0]
                rows[awaiting_context] = (weight, context)
                awaiting_context = None
                continue

        weight = _weight_of(token)
        if weight is not None:
            if pending is not None:
                rows[pending] = (weight, None)
                awaiting_context = pending
                pending = None
            continue
        if _looks_like_model(token):
            # A second id before any cost means the first row had no weight.
            pending = token
            awaiting_context = None
    return rows


def validate_weights(
    weights: dict[str, float], known_models: list[str] | None = None
) -> tuple[dict[str, float], list[str]]:
    """Drop implausible entries. Returns (clean weights, human-readable problems).

    Guards against an extractor inventing models or misreading a cost column:
    ids must exist in the live catalog and weights must be sane.
    """
    clean: dict[str, float] = {}
    problems: list[str] = []
    catalog = set(known_models or [])
    for model, weight in weights.items():
        if catalog and model not in catalog:
            problems.append(f"{model}: not in the live catalog")
            continue
        if weight < 0 or weight > 1000:
            problems.append(f"{model}: implausible weight {weight:g}")
            continue
        clean[model] = float(weight)
    if catalog:
        missing = sorted(catalog - set(clean))
        if missing:
            problems.append(
                f"{len(missing)} catalog model(s) had no cost: {', '.join(missing[:4])}"
                + ("…" if len(missing) > 4 else "")
            )
    return clean, problems


def check_alignment(
    rows: dict[str, tuple[float, int | None]], api_context: dict[str, int | None]
) -> tuple[int, int, list[str]]:
    """Judge an extraction by its context column. Returns (agreed, checked, mismatches).

    Each pasted row carries a context window next to the cost. Those values are
    specific (976K, 262K) and independently known from /v1/models, so agreement
    is strong evidence the row was read in the right order — and disagreement
    means the cost beside it cannot be trusted either.
    """
    agreed = 0
    checked = 0
    mismatches: list[str] = []
    for model, (_weight, context) in rows.items():
        expected = api_context.get(model)
        if context is None or not expected:
            continue
        checked += 1
        # The page rounds hard (1M shown for 1,048,576); allow binary-vs-decimal drift.
        if abs(context - expected) <= max(2000, expected * 0.05):
            agreed += 1
        else:
            mismatches.append(f"{model}: page {context:,} vs API {expected:,}")
    return agreed, checked, mismatches


def load_weights() -> dict[str, float]:
    """Read cached weights; supports the original flat JSON format."""
    path = get_weights_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("weights file must be a JSON object")
        raw = data.get("weights", data)
        if not isinstance(raw, dict):
            raise ValueError("weights field must be a JSON object")
        return {str(model): float(weight) for model, weight in raw.items()}
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"cannot load {path}: {error}") from error

def imported_at() -> str:
    """ISO timestamp of the latest import, or empty for legacy/absent data."""
    path = get_weights_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("imported_at", "")) if isinstance(data, dict) else ""


def import_age_days() -> int | None:
    """Whole days since the last import, or None when no dated import exists."""
    stamp = imported_at()
    if not stamp:
        return None
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - parsed).days)
    except ValueError:
        return None

def save_weights(weights: dict[str, float]) -> None:
    """Persist weights with the time they were last confirmed from Atessa."""
    from datetime import datetime, timezone

    path = get_weights_path()
    payload = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "weights": dict(sorted(weights.items())),
    }
    with _WEIGHTS_LOCK:
        atomic_write_json(path, payload, indent=2)
        refresh()

def weight_for(model: str) -> float | None:
    """Credits per request for a model, or None when it has not been imported."""
    return _cache().get(model)


_CACHED: dict[str, float] | None = None


def _cache() -> dict[str, float]:
    global _CACHED
    with _WEIGHTS_LOCK:
        if _CACHED is None:
            try:
                _CACHED = load_weights()
            except ValueError:
                _CACHED = {}
        return _CACHED


def refresh() -> None:
    """Drop the in-memory cache after an import."""
    global _CACHED
    with _WEIGHTS_LOCK:
        _CACHED = None

def format_weight(weight: float | None) -> str:
    """Render a quota weight the way the models page does."""
    if weight is None:
        return "?"
    if weight == 0:
        return "Free"
    return f"{weight:g}×"


def format_credits(credits: float | None) -> str:
    """Render a credit total, keeping fractional weights readable."""
    if credits is None:
        return "?"
    if credits == 0:
        return "0"
    return f"{credits:g}"


def selection_cost(models: list[str]) -> tuple[float, int, list[tuple[str, float]]]:
    """Total credits for one run across models.

    Returns (known total, count of models with no imported cost, the priciest
    few) so a pane can show what a run costs before it starts.
    """
    total = 0.0
    unknown = 0
    priced: list[tuple[str, float]] = []
    for model in models:
        weight = weight_for(model)
        if weight is None:
            unknown += 1
            continue
        total += weight
        priced.append((model, weight))
    priced.sort(key=lambda item: -item[1])
    return total, unknown, priced
