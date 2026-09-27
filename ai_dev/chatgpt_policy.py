"""Deterministic, non-blocking policy for ordinary ChatGPT MCP advisory.

This module selects *whether* a compact advisory may be requested.  It never
calls ChatGPT or MCP, and its result cannot replace a Codex turn or verification.
"""

import hashlib
import json

POLICY_VERSION = "2.0.14-chatgpt-advisory-v2"
DEFAULT_BATCH_ITEMS = 4
PREFERRED_BATCH_ITEMS = 6
MAX_BATCH_ITEMS = 8
NORMAL = "normal"
DEEP = "deep"

ALLOWED_REASONS = ("diagnosis", "architecture", "repeated_stall",
                   "disagreement", "context_assumption_review")
NEGATIVE_REASONS = ("boilerplate", "obvious_lint_type_fix", "tests",
                    "ordinary_implementation")


def _text(task):
    """Use only caller-provided compact metadata; never repository/source text."""
    if not isinstance(task, dict):
        return ""
    values = []
    for key in ("advisory_reason", "review_reason", "kind", "reason", "prompt", "label"):
        value = task.get(key)
        if isinstance(value, str):
            values.append(value.lower())
    return " ".join(values)


def _has(text, *words):
    return any(word in text for word in words)


def decide(task):
    """Return a JSON-safe advisory decision for compact task metadata.

    Negative categories win, so a routine implementation remains Codex-only
    even when a caller labels it as a review.  ``deep`` is reserved solely for
    an architecture deadlock; every other eligible case is ``normal``.
    """
    task = task if isinstance(task, dict) else {}
    text = _text(task)
    attempts = task.get("attempt_count", 0)
    attempts = attempts if type(attempts) is int and attempts >= 0 else 0
    status = str(task.get("status", "")).upper()
    negative = (
        ("boilerplate", _has(text, "boilerplate", "шаблон")),
        ("obvious_lint_type_fix", _has(text, "lint", "type fix", "type-fix", "типизац")),
        ("tests", _has(text, "test only", "tests", "тест")),
        ("ordinary_implementation", _has(text, "ordinary implementation", "обычн", "implement", "реализац")),
    )
    for reason, matched in negative:
        if matched:
            return _decision(False, reason)
    if _has(text, "architecture deadlock", "архитектурный тупик"):
        return _decision(True, "architecture", DEEP)
    if _has(text, "architecture", "архитектур"):
        return _decision(True, "architecture", NORMAL)
    if _has(text, "diagnosis", "diagnose", "диагност"):
        return _decision(True, "diagnosis", NORMAL)
    if _has(text, "disagreement", "несоглас", "разноглас") or task.get("disagreement") is True:
        return _decision(True, "disagreement", NORMAL)
    if _has(text, "assumption", "context review", "контекст", "допущен") or task.get("assumption_review") is True:
        return _decision(True, "context_assumption_review", NORMAL)
    if status in ("STALLED", "BLOCKED") and attempts >= 2:
        return _decision(True, "repeated_stall", NORMAL)
    return _decision(False, "not_eligible")


def _decision(enabled, reason, level=None):
    return {
        "policy_version": POLICY_VERSION,
        "enabled": enabled,
        "reason": reason,
        "level": level if enabled else None,
        "non_blocking": True,
        "compact_evidence_only": True,
        "on_demand_details": True,
        "forbidden_context": ["repository", "source", "reasoning_transcript"],
        "resume_models": ["Luna", "Spark"] if enabled else [],
        "codex_only_models": ["Terra", "Sol", "Astra"],
    }


def batch_limit(requested=None):
    """Choose a 4--6 item batch by default and reject requests above eight."""
    if requested is None:
        return DEFAULT_BATCH_ITEMS
    if type(requested) is not int or requested < 1 or requested > MAX_BATCH_ITEMS:
        raise ValueError("advisory batch must contain 1..%d tasks" % MAX_BATCH_ITEMS)
    return requested


def cache_key(task_hash, repo_state_hash):
    """Exact cache identity; either hash changing makes an entry stale."""
    payload = {"task_hash": str(task_hash), "repo_state_hash": str(repo_state_hash),
               "policy_version": POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def cache_status(cache, task_hash, repo_state_hash):
    """Report exact hit, negative hit, miss, or stale without mutating cache."""
    cache = cache if isinstance(cache, dict) else {}
    key = cache_key(task_hash, repo_state_hash)
    item = cache.get(key)
    if isinstance(item, dict):
        return "negative_hit" if item.get("negative") else "exact_hit"
    for value in cache.values():
        if isinstance(value, dict) and value.get("task_hash") == task_hash:
            return "stale"
    return "miss"
