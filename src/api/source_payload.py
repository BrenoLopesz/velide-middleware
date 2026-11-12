"""Immutable business payload comparison uses instants, not clock spellings."""
from datetime import datetime, timezone
import pytz


def canonical_payload(payload, source_timezone=None):
    result = dict(payload)
    if result.get("createdAt") is not None:
        value = result["createdAt"]
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            if not source_timezone:
                raise ValueError("A bound source timezone is required for a naive original receipt")
            instant = pytz.timezone(source_timezone).localize(instant, is_dst=None)
        result["createdAt"] = instant.astimezone(timezone.utc).isoformat()
    return result


def comparable_payload(payload, source_timezone=None):
    return {key: value for key, value in canonical_payload(payload, source_timezone).items() if value is not None}
