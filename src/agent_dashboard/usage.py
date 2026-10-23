"""Shared tolerant reader for per-attempt model usage and cost.

The agent writes per-model usage in camelCase (``costUSD``,
``inputTokens``, ``cacheReadInputTokens``, ``cacheCreationInputTokens``,
``outputTokens``) and the attempt total under
``token_budget.total_cost_usd``. Older or divergent payloads use snake_case
(``cost_usd``, ``input_tokens``, ...). Both the attempt summaries
(``history``) and the Attempt log header (``attempt_log``) read usage
through this module so the two cannot drift apart again.

Cost precedence (as the attempt-log view spec says): ``token_budget``
total when present, else the sum of per-model costs. A record without
usage renders ``n/a``, never zero: only a real number counts as usage.
"""

from __future__ import annotations

from typing import Any

#: Budget total keys accepted at the top level of one attempt record, in
#: preference order. The agent reports the total as ``total_cost_usd``; the
#: camelCase and bare variants stay accepted for older/divergent payloads.
BUDGET_COST_KEYS = (
    "total_cost_usd",
    "totalCostUsd",
    "totalCostUSD",
    "cost_usd",
    "costUSD",
    "cost",
    "total_cost",
)

#: Per-model cost keys accepted inside one model's usage counters, in
#: preference order. The agent writes camelCase ``costUSD``; snake_case
#: stays accepted for older or divergent payloads.
MODEL_COST_KEYS = (
    "costUSD",
    "cost_usd",
    "total_cost_usd",
    "totalCostUsd",
    "cost",
    "total_cost",
)

#: Token counters summed across the per-model usage map. Each canonical
#: snake_case key maps to the accepted variants in preference order: the
#: agent's camelCase first, snake_case for older or divergent payloads.
TOKEN_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("inputTokens", "input_tokens", "input"),
    "cache_read_input_tokens": (
        "cacheReadInputTokens",
        "cache_read_input_tokens",
        "cache_read",
    ),
    "cache_creation_input_tokens": (
        "cacheCreationInputTokens",
        "cache_creation_input_tokens",
        "cache_creation",
    ),
    "output_tokens": ("outputTokens", "output_tokens", "output"),
}

#: Canonical token keys in stable display order.
TOKEN_CANONICAL_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

#: Stop reason keys accepted at the top level of one attempt record, in
#: preference order. The agent writes snake_case ``model_stop_reason``;
#: camelCase stays accepted for divergent payloads.
STOP_REASON_KEYS = (
    "model_stop_reason",
    "modelStopReason",
    "stop_reason",
    "stopReason",
)

#: Display for missing usage.
NOT_APPLICABLE = "n/a"


def _num(raw: Any) -> int | float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw


def _str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _model_usage(attempt: dict[str, Any]) -> dict[str, Any]:
    usage = attempt.get("model_usage")
    return usage if isinstance(usage, dict) else {}


def _first_num(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | float | None:
    for key in keys:
        value = _num(mapping.get(key))
        if value is not None:
            return value
    return None


def read_cost_value(attempt: dict[str, Any]) -> float | None:
    """Numeric USD cost: ``token_budget`` total first, else summed model costs.

    Only a real number counts: a missing, non-numeric or absent budget falls
    back to the per-model sum; a missing sum means no cost data (``None``),
    never zero.
    """
    if isinstance(attempt, dict):
        budget = attempt.get("token_budget")
        if isinstance(budget, dict):
            value = _first_num(budget, BUDGET_COST_KEYS)
            if value is not None:
                return float(value)
        usage = _model_usage(attempt)
        total: int | float | None = None
        for model in usage.values():
            if not isinstance(model, dict):
                continue
            value = _first_num(model, MODEL_COST_KEYS)
            if value is not None:
                total = (total or 0) + value
        if total is not None:
            return float(total)
    return None


def read_token_totals(attempt: dict[str, Any]) -> dict[str, int | float]:
    """Token counters summed across the per-model usage map.

    Each model contributes its first matching variant per canonical key, so
    a record carrying both spellings never double-counts. Only real numbers
    count; missing categories stay absent.
    """
    totals: dict[str, int | float] = {}
    if not isinstance(attempt, dict):
        return totals
    usage = _model_usage(attempt)
    for model in usage.values():
        if not isinstance(model, dict):
            continue
        for canonical in TOKEN_CANONICAL_KEYS:
            value = _first_num(model, TOKEN_FIELD_KEYS[canonical])
            if value is not None:
                totals[canonical] = totals.get(canonical, 0) + value
    return totals


def has_usage(attempt: dict[str, Any]) -> bool:
    """Whether an attempt record carries any usable cost or token data."""
    if not isinstance(attempt, dict):
        return False
    if read_cost_value(attempt) is not None:
        return True
    return bool(read_token_totals(attempt))


def read_stop_reason(attempt: dict[str, Any], usage_present: bool) -> str:
    """Model stop reason text, or ``n/a`` when the attempt has no usage.

    Without usage data the stop reason renders as ``n/a`` (attempts that
    failed before the model ran); otherwise the first present reason key
    wins, with per-model dicts joined in stable order.
    """
    if not usage_present:
        return NOT_APPLICABLE
    if not isinstance(attempt, dict):
        return NOT_APPLICABLE
    for key in STOP_REASON_KEYS:
        raw = attempt.get(key)
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, dict):
            reasons = sorted({v for v in raw.values() if isinstance(v, str) and v})
            if reasons:
                return ", ".join(reasons)
    return NOT_APPLICABLE
