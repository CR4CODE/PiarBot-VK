"""DST-safe scheduled posts with one durable claim before Telegram I/O."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_to_utc(value: str, zone_name: str) -> str:
    """Interpret a wall clock in an IANA zone, rejecting DST gaps/folds.

    An ambiguous wall clock is accepted only with an explicit matching offset.
    """
    try:
        if not isinstance(value, str) or not isinstance(zone_name, str):
            raise ValueError("Date and timezone required")
        zone = ZoneInfo(zone_name)
        local = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except (ValueError, ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("Invalid local date or IANA timezone") from exc
    if local.tzinfo is not None:
        instant = local.astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("Offset disagrees with timezone")
        if instant.astimezone(zone).utcoffset() != local.utcoffset():
            raise ValueError("Offset disagrees with timezone")
        return instant.isoformat()
    valid = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        converted = aware.astimezone(timezone.utc)
        if converted.astimezone(zone).replace(tzinfo=None) == local:
            valid.append(converted)
    unique = {candidate.isoformat(): candidate for candidate in valid}
    if not unique:
        raise ValueError("Nonexistent local time during daylight-saving transition")
    if len(unique) > 1:
        raise ValueError("Ambiguous local time: specify a matching UTC offset")
    return next(iter(unique.values())).isoformat()


def run_due(service, store, now_utc: datetime | str) -> list[tuple[int, str]]:
    """Claim due jobs first; no stale inflight job is ever replayed."""
    if isinstance(now_utc, datetime):
        if now_utc.tzinfo is None:
            raise ValueError("Scheduler clock must be timezone-aware")
        now_utc = now_utc.astimezone(timezone.utc).isoformat()
    results = []
    for job in store.claim_due(now_utc):
        try:
            state = service.execute_job(job)
            if state not in ("published", "failed", "uncertain"):
                state = "uncertain"
        except Exception:
            # No attempt to infer whether Telegram received an action.
            state = "uncertain"
        try:
            store.finish_job(job["id"], state, {"publication_state": state})
        except Exception:
            # A crashed receipt writer must leave the durable claim in place.
            state = "uncertain"
        results.append((job["id"], state))
    return results
