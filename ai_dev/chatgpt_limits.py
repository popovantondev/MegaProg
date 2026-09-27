"""Offline, fail-closed snapshot/ledger for ordinary ChatGPT only.

This never contacts ChatGPT and never claims to know an official limit.
Codex/Work usage is deliberately not represented here.
"""
from datetime import datetime, timezone
from pathlib import Path
from .storage import read, write

LEDGER_FILE = ".ai-dev/chatgpt-ledger.json"
LEDGER_VERSION = 2
LEGACY_VERSION = 1
STATUSES = ("available", "exhausted", "unknown")
SOURCES = ("manual_ui", "official_export", "unknown")
CONFIDENCES = ("high", "medium", "low", "unknown")
DEFAULT_BUCKET = "chat_pro"


def _default_bucket():
    return {"status": "unknown", "observed_at": None, "reset_at": None,
            "limit": None, "used": None, "remaining": None,
            "source": "unknown", "confidence": "unknown", "notes": ""}


def _default():
    return {"version": LEDGER_VERSION, "default_bucket": DEFAULT_BUCKET,
            "buckets": {DEFAULT_BUCKET: _default_bucket()},
            # v1 compatibility fields remain available to old callers.
            "limit_known": False, "limit_turns": None, "reserved_turns": 0,
            "spent_turns": 0, "manual_status": "unknown", "reset_at": None,
            "source": "unknown", "confidence": "unknown", "notes": ""}


def _valid_time(value):
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    # v1 accepted free-form reset labels (for example ``tomorrow``); retain
    # those strings on migration.  ISO values receive real expiry handling.
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return True


def _iso_time(value):
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _expired(value):
    if not value or not _valid_time(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _clean_bucket(value):
    result = _default_bucket()
    if not isinstance(value, dict):
        return result
    status = value.get("status", "unknown")
    result["status"] = status if status in STATUSES else "unknown"
    result["observed_at"] = value.get("observed_at") if _iso_time(value.get("observed_at")) else None
    result["reset_at"] = value.get("reset_at") if _valid_time(value.get("reset_at")) else None
    for key in ("limit", "used", "remaining"):
        result[key] = _number(value.get(key))
    source = value.get("source", "unknown")
    result["source"] = source if source in SOURCES else "unknown"
    confidence = value.get("confidence", "unknown")
    result["confidence"] = confidence if confidence in CONFIDENCES else "unknown"
    result["notes"] = str(value.get("notes", ""))[:2000]
    return result


def _sync_legacy(result):
    default = result.get("default_bucket", DEFAULT_BUCKET)
    if not isinstance(result.get("buckets"), dict):
        result["buckets"] = {}
    result["buckets"][default] = _clean_bucket(result["buckets"].get(default))
    bucket = result["buckets"][default]
    result["manual_status"] = bucket["status"]
    result["reset_at"] = bucket["reset_at"]
    result["limit_turns"] = bucket["limit"]
    result["limit_known"] = bucket["limit"] is not None
    result["spent_turns"] = _number(result.get("spent_turns")) or 0
    result["reserved_turns"] = _number(result.get("reserved_turns")) or 0
    result["source"] = bucket["source"]
    result["confidence"] = bucket["confidence"]
    result["notes"] = bucket["notes"]
    return result


def load(root):
    path = Path(root) / LEDGER_FILE
    if not path.is_file() or path.is_symlink():
        return _default()
    try:
        data = read(path)
    except (OSError, ValueError, TypeError):
        return _default()
    if not isinstance(data, dict):
        return _default()
    result = _default()
    if data.get("version") == LEGACY_VERSION:
        legacy = _default_bucket()
        legacy.update({"status": data.get("manual_status", "unknown"),
                       "observed_at": "1970-01-01T00:00:00+00:00",
                       "reset_at": data.get("reset_at"),
                       "limit": data.get("limit_turns") if data.get("limit_known") else None,
                       "used": (data.get("reserved_turns", 0) or 0) + (data.get("spent_turns", 0) or 0),
                       "source": "manual_ui", "confidence": "low",
                       "notes": "Migrated from legacy chatgpt-ledger.json"})
        result["buckets"] = {DEFAULT_BUCKET: _clean_bucket(legacy)}
        result["reserved_turns"] = data.get("reserved_turns", 0)
        result["spent_turns"] = data.get("spent_turns", 0)
        return _sync_legacy(result)
    if data.get("version") != LEDGER_VERSION:
        return _default()
    result.update({key: data[key] for key in result if key in data})
    if not isinstance(result.get("default_bucket"), str) or not result["default_bucket"]:
        result["default_bucket"] = DEFAULT_BUCKET
    buckets = result.get("buckets")
    if not isinstance(buckets, dict) or not buckets:
        return _default()
    result["buckets"] = {name: _clean_bucket(value) for name, value in buckets.items()
                          if isinstance(name, str) and name and len(name) <= 80}
    return _sync_legacy(result)


def bucket_names(ledger):
    return sorted((ledger or {}).get("buckets", {}).keys()) or [DEFAULT_BUCKET]


def availability(ledger, bucket=None):
    ledger = ledger if isinstance(ledger, dict) else _default()
    if "buckets" not in ledger:
        legacy = _default_bucket()
        legacy["status"] = ledger.get("manual_status", "unknown")
        legacy["limit"] = ledger.get("limit_turns") if ledger.get("limit_known") else None
        reserved = _number(ledger.get("reserved_turns")) or 0
        spent = _number(ledger.get("spent_turns")) or 0
        legacy["used"] = reserved + spent
        legacy["observed_at"] = "1970-01-01T00:00:00+00:00"
        ledger = dict(ledger, default_bucket=DEFAULT_BUCKET, buckets={DEFAULT_BUCKET: legacy})
    bucket = bucket or ledger.get("default_bucket", DEFAULT_BUCKET)
    item = _clean_bucket((ledger.get("buckets") or {}).get(bucket))
    status, reason = item["status"], ""
    if status != "unknown" and not item["observed_at"]:
        status, reason = "unknown", "snapshot has no observed_at"
    elif status != "unknown" and _expired(item["reset_at"]):
        status, reason = "unknown", "reset_at has passed; fresh observation required"
    if item["limit"] is not None and item["used"] is not None and item["used"] >= item["limit"]:
        status, reason = "exhausted", "local used reached local limit"
    remaining = item["remaining"]
    if remaining is None and item["limit"] is not None and item["used"] is not None:
        remaining = max(0, item["limit"] - item["used"])
    return {"bucket": bucket, "status": status.upper(), "reason": reason,
            "remaining": remaining, "remaining_turns": remaining, "official": False,
            "action": ("offer ordinary ChatGPT only for an eligible task in this bucket"
                        if status == "available" else
                        "fallback to Codex; task, resume, and verification remain unblocked")}


def report(root):
    ledger = load(root)
    result = dict(ledger)
    result["buckets"] = {name: dict(value, **availability(ledger, name))
                          for name, value in ledger["buckets"].items()}
    result.update(availability(ledger, ledger["default_bucket"]))
    result["ledger_path"] = str(Path(root) / LEDGER_FILE)
    result["boundary"] = "ChatGPT only; Codex/Work limits are separate and not represented"
    result["official_limit_source"] = False
    return result


def update(root, *, bucket=DEFAULT_BUCKET, status=None, limit_turns=None,
           reserved_turns=None, spent_turns=None, reset_at=None, observed_at=None,
           source=None, confidence=None, notes=None, remaining=None, used=None,
           default_bucket=None):
    ledger = load(root)
    if not isinstance(bucket, str) or not bucket or len(bucket) > 80:
        raise ValueError("bucket must be a non-empty name up to 80 characters")
    item = _clean_bucket(ledger["buckets"].get(bucket))
    if status is not None and observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    if status is not None and status not in STATUSES:
        raise ValueError("status must be available, exhausted, or unknown")
    if source is not None and source not in SOURCES:
        raise ValueError("source must be manual_ui, official_export, or unknown")
    if confidence is not None and confidence not in CONFIDENCES:
        raise ValueError("confidence must be high, medium, low, or unknown")
    for key, value in {"status": status, "reset_at": reset_at, "observed_at": observed_at,
                       "source": source, "confidence": confidence, "notes": notes,
                       "remaining": remaining, "used": used}.items():
        if value is not None:
            if key == "observed_at" and not _iso_time(value):
                raise ValueError("%s must be an ISO timestamp" % key)
            if key in ("remaining", "used") and _number(value) is None:
                raise ValueError("%s must be a non-negative integer" % key)
            item[key] = value
    if limit_turns is not None:
        if _number(limit_turns) is None:
            raise ValueError("limit_turns must be a non-negative integer")
        item["limit"] = limit_turns
    for key, value in (("reserved_turns", reserved_turns), ("spent_turns", spent_turns)):
        if value is not None:
            if _number(value) is None:
                raise ValueError("%s must be a non-negative integer" % key)
            ledger[key] = value
    if reserved_turns is not None or spent_turns is not None:
        item["used"] = ledger["reserved_turns"] + ledger["spent_turns"]
    ledger["buckets"][bucket] = item
    if default_bucket is not None:
        if not isinstance(default_bucket, str) or not default_bucket:
            raise ValueError("default_bucket must be a non-empty name")
        ledger["default_bucket"] = default_bucket
        ledger["buckets"].setdefault(default_bucket, _default_bucket())
    ledger["version"] = LEDGER_VERSION
    _sync_legacy(ledger)
    write(Path(root) / LEDGER_FILE, ledger)
    return report(root)


def record_import(root, bucket=DEFAULT_BUCKET):
    """Charge one accepted fresh import in exactly the selected bucket."""
    ledger = load(root)
    if bucket not in ledger["buckets"]:
        raise ValueError("unknown ChatGPT bucket: %s" % bucket)
    item = _clean_bucket(ledger["buckets"][bucket])
    item["used"] = (item["used"] or 0) + 1
    if item["limit"] is not None:
        item["remaining"] = max(0, item["limit"] - item["used"])
    ledger["buckets"][bucket] = item
    ledger["spent_turns"] = (ledger.get("spent_turns") or 0) + 1
    _sync_legacy(ledger)
    write(Path(root) / LEDGER_FILE, ledger)
    return report(root)
