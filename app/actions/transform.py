import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil.parser import parse as parse_date

logger = logging.getLogger(__name__)


def _ensure_utc(val: datetime) -> datetime:
    return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val.astimezone(timezone.utc)


def human_friendly_timedelta(td: timedelta) -> str:
    """Format a timedelta as e.g. '2d, 3h, 4m' (sub-minute becomes '0m')."""
    if td < timedelta(0):
        return "-" + human_friendly_timedelta(-td)
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return ", ".join(parts) if parts else "0m"


def chunks(items: list, n: int):
    if n <= 0:
        raise ValueError(f"chunk size must be a positive integer, got {n}")
    for i in range(0, len(items), n):
        yield items[i:i + n]


def build_observation(*, event: dict, device_name: str) -> Optional[dict]:
    """Transform one Movebank event record into a Gundi v2 observation dict.

    Returns None for events that can't be used (bad timestamp, no individual_id,
    or future-dated).
    """
    try:
        recorded_at = _ensure_utc(parse_date(event.get("timestamp")))
    except Exception:
        logger.warning(f"unable to parse timestamp: {event.get('timestamp')}")
        return None

    # Everything except coordinates and timestamp goes to additional, so it's
    # available for analysis in the destination.
    additional = {k: v for k, v in event.items() if k not in ("location_long", "location_lat", "timestamp")}

    if update_ts := event.get("update_ts"):
        try:
            updated_at = _ensure_utc(parse_date(update_ts))
            additional["update_latency"] = human_friendly_timedelta(updated_at - recorded_at)
        except Exception:
            pass

    individual_id = event.get("individual_id")
    lon, lat = event.get("location_long"), event.get("location_lat")

    # Records without coordinates (e.g. accessory-measurements) are stored at
    # (0, 0) with a +1ms shift, so they don't collide with a GPS observation at
    # the same timestamp. Revisit when observations can be updated in EarthRanger.
    # Only genuinely missing values count as "no coordinates" — Movebank CSV
    # rows carry coordinates as strings, and a real "0.0" must not be fudged.
    if lon in (None, "") or lat in (None, ""):
        x, y = 0.0, 0.0
        recorded_at += timedelta(milliseconds=1)
    else:
        try:
            x, y = float(lon), float(lat)
        except (TypeError, ValueError):
            logger.warning(f"unable to parse coordinates: lon={lon!r} lat={lat!r}")
            return None

    if not individual_id or recorded_at > datetime.now(tz=timezone.utc):
        return None

    additional["subject_name"] = device_name
    additional["loaded_at"] = datetime.now(tz=timezone.utc).isoformat()  # fudge to avoid duplicate drop

    return {
        "source": individual_id,
        "source_name": device_name,
        "type": "tracking-device",
        "recorded_at": recorded_at.isoformat(),
        "location": {"lat": y, "lon": x},
        "additional": additional,
    }
