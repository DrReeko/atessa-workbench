"""Token counting and price lookup.

Both providers import slowly, so every entry point is lazy and cached. Missing
packages or unknown models degrade to None rather than raising.
"""
from __future__ import annotations

from functools import lru_cache

from .weights import weight_for

_ENCODING = None
_ENCODING_TRIED = False


@lru_cache(maxsize=1)
def _cost_table() -> dict:
    try:
        import litellm
    except Exception:
        return {}
    return dict(litellm.model_cost)


def _encoding():
    global _ENCODING, _ENCODING_TRIED
    if _ENCODING_TRIED:
        return _ENCODING
    _ENCODING_TRIED = True
    try:
        import tiktoken

        _ENCODING = tiktoken.get_encoding("o200k_base")
    except Exception:
        _ENCODING = None
    return _ENCODING


def count_tokens(text: str) -> int | None:
    """Exact token count when tiktoken is installed, else None."""
    encoding = _encoding()
    if encoding is None:
        return None
    return len(encoding.encode(text, disallowed_special=()))


def estimate_tokens(text: str) -> int:
    """Best-effort count: exact when possible, character heuristic otherwise."""
    exact = count_tokens(text)
    return exact if exact is not None else max(1, len(text) // 4)


@lru_cache(maxsize=256)
def model_meta(model: str) -> dict | None:
    """Context and capability metadata for a model, or None when unknown."""
    entry = _cost_table().get(model)
    if not entry:
        return None
    return {
        "context": entry.get("max_input_tokens") or entry.get("max_tokens"),
        "vision": bool(entry.get("supports_vision")),
    }


def request_credits(model: str, requests: int = 1) -> float | None:
    """Credits consumed by N requests, or None when the weight is unknown.

    Atessa bills per request times the model's quota weight, so token counts
    do not enter into it.
    """
    weight = weight_for(model)
    if weight is None:
        return None
    return weight * requests


def format_context(context: int | None) -> str:
    """Render a context window as a compact token count."""
    if not context:
        return "—"
    if context >= 1_000_000:
        return f"{context / 1_000_000:.1f}M".replace(".0M", "M")
    if context >= 1_000:
        return f"{context // 1000}k"
    return str(context)
