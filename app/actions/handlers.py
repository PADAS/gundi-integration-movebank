import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import random
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pydantic
import redis.asyncio as redis_asyncio
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient
from movebank_client.errors import MBRateLimitError

import app.actions.client as client
from app.actions.core import ReferenceDataResponse, action_title
from app import settings
from dataclasses import dataclass, field

from app.actions.backfill_queue import STEP_ACQUIRED, STEP_BUSY, BackfillJob
from app.actions.client import IndividualState, generate_individuals
from app.actions.configurations import (
    AuthenticateConfig,
    BackfillConfig,
    BackfillEventsForIndividualConfig,
    ListIndividualsQuery,
    ListStudiesQuery,
    PullObservationsConfig,
    PullEventsForIndividualConfig,
)
from app.actions import reference_data
from app.actions.transform import _ensure_utc, build_observation, chunks
from app.services import reference_cache
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.errors import IntegrationRateLimitError
from app.services.gundi import send_observations_to_gundi
from app.services.movebank_connections import movebank_slot, NoConnectionSlot
from app.services.state import IntegrationStateManager


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

# Ported from the v1 integration — production-proven values.
HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD = 5000  # events; above this, shrink the fetch window
# Per-run cap, checked between windows: once total processed reaches this, no
# further windows are fetched. A single window may overshoot the cap — everything
# fetched in a window is sent so its cursors can advance consistently.
MAXIMUM_RECORDS_PER_INDIVIDUAL = 2000
DEFAULT_BATCH_WINDOW = timedelta(days=5)
QUIET_PERIOD_SECONDS = 3600  # skip an individual for this long after an empty window

CURSOR_STATE_ACTION_ID = "pull_events_for_individual"
QUIET_STATE_ACTION_ID = "pull_events_for_individual_quiet"

BACKFILL_ACTION_ID = "backfill_events_for_individual"
BACKFILL_WATERMARK_ACTION_ID = "backfill_watermark"
BACKFILL_PROGRESS_THROTTLE_SECONDS = 300
BACKFILL_MAX_ATTEMPTS = 5
# Per-step backstop for the backfill cascade: once a single invocation's
# observations reach this, it stops taking more windows, persists its scan
# position, and re-triggers — bounding how long one PubSub-ack'd invocation
# can run even if window sizing (below) underestimates event density.
MAX_RECORDS_PER_BACKFILL_STEP = 10000


def _query_start_for_sensor(sensor_type_id: int, sensor_start: datetime, minimum_event_id: int) -> datetime:
    """Accessory-measurements can arrive hours late, so its query re-reads back
    ACCESSORY_SETTLING_HOURS — but only once there's a real prior cursor for
    this sensor (minimum_event_id > 1, i.e. at least one event already
    recorded). Widening a freshly-seeded cursor (minimum_event_id == 1, no
    prior events — e.g. right after _seed_pull_cursor_at_end) would re-read
    [T-settling, T) with minimum_event_id=1 and re-emit duplicates of
    everything already sent in that span. GPS is prompt and always uses its
    exact cursor."""
    if (sensor_type_id == MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID["accessory-measurements"]
            and minimum_event_id > 1):
        return sensor_start - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS)
    return sensor_start


async def _cancel_fetch(task: "asyncio.Task") -> None:
    """Cancel an in-flight sensor fetch and wait for it to unwind (releasing
    its Movebank connection slot). The task's own CancelledError is expected;
    anything else it raises while unwinding is logged and dropped — the
    window is being discarded anyway. Cancellation of the calling step is
    NOT swallowed."""
    task.cancel()
    # Collect the fetch's own outcome, but let cancellation of THIS step
    # propagate: catching CancelledError around a direct await would also
    # swallow the runner's hard-deadline cancellation of the handler, which
    # could then persist state and publish a continuation despite being
    # cancelled (same split as action_runner._cancel_handler).
    (outcome,) = await asyncio.gather(task, return_exceptions=True)
    if isinstance(outcome, Exception):
        logger.warning(f"Backfill fetch raised while being cancelled (ignored): {outcome!r}")


def _window_fetch_outran_budget(window_started_at: float, budget_seconds: float, now: float = None) -> bool:
    """True when fetching the current window has consumed at least
    SLOW_WINDOW_BUDGET_FRACTION of the step budget: the window itself is too
    big (or Movebank too slow) for the budget and should be retried smaller.

    Shared by the pull and backfill loops. Running out of budget on the last
    window of a step is the normal case and must NOT shrink the window, so
    the decision is based on how long THIS window's fetching took, not on
    whether a fetch happened to be in progress at the deadline."""
    elapsed = (time.monotonic() if now is None else now) - window_started_at
    return elapsed >= settings.SLOW_WINDOW_BUDGET_FRACTION * budget_seconds


def _compute_batch_window(number_of_events, span_seconds) -> timedelta:
    """Shared window-sizing logic for the steady-state pull and backfill:
    high-frequency individuals get a window sized to hold roughly
    HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events over the given span, assuming
    an even distribution; falls back to DEFAULT_BATCH_WINDOW otherwise, or
    when the computed window would be non-positive (which would never
    advance the scan position and spin forever)."""
    if (number_of_events or 0) > HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD:
        window = timedelta(seconds=(HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD / number_of_events) * span_seconds)
    else:
        window = DEFAULT_BATCH_WINDOW
    if window <= timedelta(0):
        window = DEFAULT_BATCH_WINDOW
    return window


def _pin_unset_sensor_cursors(state, sensor_type_ids, sensor_type_timestamps, minimum_event_ids):
    """Store the start position of every sensor that has no saved cursor yet, so a
    state saved before any events arrive does not let the next run start from a
    later `now - maximum_lookback_hours` (skipping data the backfill also leaves
    out, since coverage_start is already saved). Event-id cursors are unchanged."""
    for stid in sensor_type_ids:
        if state.get_sensor_state(stid).latest_timestamp is None:
            state.update_sensor_state(stid, sensor_type_timestamps[stid], minimum_event_ids[stid] - 1)


def _display_name(ind) -> str:
    """Human-facing name for an individual, used as the observation's
    subject_name/source_name. Prefers the tag's local_identifier, falling
    back to the nick_name and then the ring_id."""
    return ind.local_identifier or ind.nick_name or ind.ring_id


def _supported_sensor_type_ids(ind) -> list:
    """Movebank sensor-type labels this individual reports, mapped to the
    numeric sensor_type_ids this integration knows how to handle."""
    labels = [label.strip().lower() for label in ind.sensor_type_ids.split(",")]
    return [
        MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID[label]
        for label in labels
        if label in MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID
    ]


def _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids):
    """Per-sensor cursor advancement shared by the steady-state pull and the
    backfill sub-action: regroups a flat event list by each event's own
    sensor_type_id field, then advances the timestamp/event-id cursor for each
    sensor type from what actually came back (event-id cursor still advances
    on unparseable timestamps so junk records aren't refetched forever)."""
    events_by_sensor = {}
    for e in events:
        try:
            events_by_sensor.setdefault(int(e.get("sensor_type_id")), []).append(e)
        except (TypeError, ValueError):
            continue
    for stid in sensor_type_ids:
        se = events_by_sensor.get(stid, [])
        stamps, ids = [], []
        for e in se:
            try:
                stamps.append(_ensure_utc(parse_date(e.get("timestamp"))))
            except Exception:
                pass
            try:
                ids.append(int(e.get("event_id")))
            except (TypeError, ValueError):
                pass
        if stamps or ids:
            existing = state.get_sensor_state(stid)
            window_latest = max(stamps) if stamps else sensor_type_timestamps[stid]
            window_max_id = max(ids) if ids else minimum_event_ids[stid] - 1
            # Never move a sensor cursor BACKWARD: max against what's already
            # stored. The steady-state pull is monotonic-forward, so this is a
            # no-op there; reverse backfill processes RECENT windows first, so
            # this preserves the most-recent window's timestamp/event-id for the
            # finalize forward-merge. Carrying the oldest window's lower id would
            # let the pull's accessory settling re-read re-emit seam events as
            # duplicates.
            candidate_stamps = [t for t in (existing.latest_timestamp, window_latest) if t]
            new_latest = max(candidate_stamps) if candidate_stamps else window_latest
            new_max = max(existing.highest_event_id or 0, window_max_id)
            state.update_sensor_state(stid, new_latest, new_max)
            sensor_type_timestamps[stid] = new_latest
            minimum_event_ids[stid] = new_max + 1


@action_title("(1) Movebank Credentials")
async def action_auth(integration, action_config: AuthenticateConfig):
    # Log identifiers only: the integration carries its configurations (the
    # plaintext Movebank password among them) and the action config the same
    # secret. Neither may reach the logs, on a saved integration or on the
    # ephemeral path where the credentials were never persisted anywhere.
    logger.info(f"Executing auth action for integration '{integration.id}'...")
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=action_config.username,
        password=action_config.password.get_secret_value(),
    )

    try:
        token = await mb_client.get_token()
    except client.MBForbiddenError:
        logger.exception(f"Auth unsuccessful for integration {str(integration.id)}. MB returned 403 (wrong credentials)")
        return {"valid_credentials": False, "message": "Invalid credentials"}
    except client.MBClientError as e:
        logger.exception(f"Auth action failed for integration {str(integration.id)}. Exception: {e}")
        return {"error": "An internal error occurred while trying to test credentials. Please try again later."}
    else:
        if token:
            logger.info(f"Auth successful for integration '{integration.id}'.")
            return {"valid_credentials": True}
        else:
            logger.error(f"Auth unsuccessful for integration '{integration.id}': Movebank returned no token.")
            return {"valid_credentials": False}


# --- Reference actions: the portal's study and individual pickers -----------
#
# The portal calls these while the user types (`q`) and to label saved values
# (`ids`). Movebank has no name search, so each list is fetched whole, cached
# trimmed in Redis per account, and searched here. Deliberately not wrapped in
# @activity_logger: an activity-log entry per keystroke would bury the log.

BUSY_MESSAGE = "Movebank is busy right now. Try again shortly."


@asynccontextmanager
async def _reference_movebank(integration, auth_config):
    """A Movebank client holding one of the account's connection slots.

    A lookup never waits for a slot: when pulls or a backfill hold them all,
    the picker reports Movebank as busy and the user retries, rather than a
    form field queueing behind a backfill. If Redis (which counts the slots)
    is unreachable, the lookup goes ahead uncounted: the cache read has
    already failed open for the same outage, and a form picker should not be
    the thing that turns it into an error.
    """
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(movebank_slot(auth_config.username))
        except NoConnectionSlot:
            # Retryable provider capacity, not a configuration fault. from
            # None: the message is all the portal needs.
            raise IntegrationRateLimitError(BUSY_MESSAGE) from None
        except redis_asyncio.RedisError as exc:
            logger.warning(f"Movebank connection slot unavailable (Redis); reference lookup proceeds uncounted: {exc}")
        mb = await stack.enter_async_context(mb_client)
        yield mb


async def _cached_reference_list(integration, auth_config, key: str, fetch, trim, fields) -> list:
    """The trimmed list under `key`: from Redis, else fetched from Movebank and
    cached. `fetch(mb)` returns Movebank's rows; only a real answer is cached.
    A cached entry that is not the shape this revision writes reads as a miss."""
    cached = await reference_cache.get_cached_list(key)
    if cached is not None and reference_data.has_shape(cached, fields):
        return cached
    async with _reference_movebank(integration, auth_config) as mb:
        rows = await fetch(mb)
    # Rows without an id cannot be picked; drop them rather than cache them.
    trimmed = [row for row in (trim(row) for row in rows or []) if row["id"]]
    await reference_cache.set_cached_list(key, trimmed)
    return trimmed


async def _lookup_uncached_studies(integration, auth_config, study_ids: list) -> list:
    """Ask Movebank for studies the cached list lacks, one by one. A lookup
    that fails is skipped: one bad saved value must not cost the others their
    labels. Free text is allowed in the form, so a value that is not a Movebank
    ID (they are numeric) is never sent."""
    study_ids = [study_id for study_id in study_ids if study_id.isdigit()]
    if not study_ids:
        return []
    logger.info(f"Resolving {len(study_ids)} uncached study id(s) for integration {integration.id}")
    found = []
    async with _reference_movebank(integration, auth_config) as mb:
        for study_id in study_ids:
            try:
                row = await mb.get_study(study_id=study_id)
            except Exception as exc:
                logger.warning(f"Could not look up study {study_id} for integration {integration.id}: {type(exc).__name__}")
                continue
            if row:
                found.append(reference_data.trim_study(row))
    return found


@action_title("Search Movebank Studies")
async def action_list_studies(integration, action_config: ListStudiesQuery):
    """Reference action (typeahead): studies this account can see.

    With `ids` it returns those studies in the order asked, so the portal can
    label saved values; IDs Movebank doesn't know are left out and the portal
    keeps showing them raw. Otherwise it searches by name or ID. An empty
    query returns nothing with truncated=True: the list runs to thousands.
    """
    auth_config = client.get_auth_config(integration)
    key = reference_cache.studies_key(
        integration.base_url, auth_config.username, auth_config.password.get_secret_value(),
    )
    studies = await _cached_reference_list(
        integration, auth_config, key,
        lambda mb: mb.get_studies(), reference_data.trim_study, reference_data.STUDY_FIELDS,
    )
    if not action_config.ids:
        return reference_data.search_studies(studies, action_config.q)

    # A study shared with the account after the list was cached is missing
    # from it. Ask Movebank for those directly so a saved value still gets its
    # name; they are not written back, the list refreshes on its own TTL.
    known = {study["id"] for study in studies}
    missing = [study_id for study_id in action_config.ids if study_id not in known]
    if missing:
        studies = studies + await _lookup_uncached_studies(integration, auth_config, missing)
    return reference_data.resolve(studies, action_config.ids, reference_data.study_option)


@action_title("Search Individuals in a Study")
async def action_list_individuals(integration, action_config: ListIndividualsQuery):
    """Reference action (typeahead): the individuals of one study.

    The study comes from the form field above the picker. Without one there is
    nothing to list. An empty query lists the study's first individuals, which
    is short enough to browse.
    """
    study_id = action_config.study_id
    if not study_id:
        return ReferenceDataResponse(options=[]).dict()
    auth_config = client.get_auth_config(integration)
    key = reference_cache.individuals_key(
        integration.base_url, auth_config.username, auth_config.password.get_secret_value(), study_id,
    )
    individuals = await _cached_reference_list(
        integration, auth_config, key,
        lambda mb: mb.get_individuals_by_study(study_id=study_id),
        reference_data.trim_individual, reference_data.INDIVIDUAL_FIELDS,
    )
    if action_config.ids:
        return reference_data.resolve(individuals, action_config.ids, reference_data.individual_option)
    return reference_data.search_individuals(individuals, action_config.q)


@action_title("(2) Read Movebank Data")
@activity_logger()
@crontab_schedule("*/10 * * * *")  # same cadence as the v1 cronjob
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    """List the study's individuals and trigger one sub-action per individual."""
    integration_id = str(integration.id)
    logger.info(f"Pulling observations for study {action_config.study_id}, integration {integration_id}...")

    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    try:
        async with mb_client as mb:
            async with movebank_slot(auth_config.username):
                individual_rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    except NoConnectionSlot:
        # Movebank connection budget exhausted (e.g. a backfill is saturating it).
        # This is a scheduled tick — skip cleanly and let the next one retry,
        # rather than surfacing an action failure and triggering no sub-actions.
        logger.info(
            f"Skipping pull for study {action_config.study_id}: Movebank connection budget "
            "exhausted; the next scheduled tick will retry."
        )
        return {"skipped": "no_connection_slot"}

    individuals = list(generate_individuals(individual_rows))
    logger.info(f"{len(individuals)} individuals found for study {action_config.study_id}")

    triggered = 0
    for ind in individuals:
        await trigger_action(
            integration_id=integration_id,
            action_id="pull_events_for_individual",
            config=PullEventsForIndividualConfig(
                study_id=action_config.study_id,
                individual=ind,
                maximum_lookback_hours=action_config.maximum_lookback_hours,
            ),
        )
        triggered += 1

    return {"individuals_found": len(individuals), "sub_actions_triggered": triggered}


@activity_logger()
async def action_pull_events_for_individual(integration, action_config: PullEventsForIndividualConfig):
    """Fetch events for one individual with per-sensor cursors, transform, and send to Gundi.

    Internal sub-action, triggered by pull_observations once per individual.
    """
    ind = action_config.individual
    integration_id = str(integration.id)
    log_reference = f"study:{action_config.study_id},individual:{ind.id},local_identifier:{ind.local_identifier}"

    if await state_manager.get_state(integration_id, QUIET_STATE_ACTION_ID, source_id=ind.id):
        logger.info(f"Skipping individual {log_reference} for quiet period.")
        return {"skipped": "quiet_period"}

    if ind.timestamp_start is None:
        logger.info(f"Skip Movebank {log_reference} for no timestamp_start.")
        return {"skipped": "no_timestamp_start"}

    now = datetime.now(tz=timezone.utc)
    # Fetch up to NOW, NOT the individual's timestamp_end. Movebank's per-individual
    # timestamp_end is derived study metadata that often lags the live event
    # stream, so capping here froze collection once a cursor caught up to a stale
    # value ("no new data" forever). The per-sensor cursor + event-id filter dedup,
    # and the empty-window quiet flag idles genuinely dormant individuals.
    highest_date = now

    sensor_type_ids = _supported_sensor_type_ids(ind)
    if not sensor_type_ids:
        logger.info(f"Skip Movebank {log_reference} — no supported sensor types in '{ind.sensor_type_ids}'.")
        return {"skipped": "no_supported_sensors"}

    saved_state = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    try:
        individual_state = IndividualState.parse_obj(saved_state) if saved_state else None
    except pydantic.ValidationError:
        logger.exception(f"Failed parsing saved state for {log_reference}; starting fresh.")
        individual_state = None
    was_fresh = individual_state is None
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback
    # window measured from NOW (not timestamp_end, which can be stale) so a fresh
    # individual's first run stays bounded to the recent window — history is the
    # backfill's job.
    default_start = now - timedelta(hours=action_config.maximum_lookback_hours)
    if was_fresh:
        # First-ever cursor for this individual: record the oldest point the pull
        # will cover, so a later backfill knows where to stop (fills [start, this)).
        individual_state.coverage_start = default_start
    sensor_type_timestamps = {}
    minimum_event_ids = {}
    for sensor_type_id in sensor_type_ids:
        sensor_state = individual_state.get_sensor_state(sensor_type_id)
        sensor_type_timestamps[sensor_type_id] = sensor_state.latest_timestamp or default_start
        minimum_event_ids[sensor_type_id] = (sensor_state.highest_event_id or 0) + 1

    earliest_start = min(sensor_type_timestamps.values())
    if earliest_start >= now:
        # Cursor has reached the present — nothing to fetch. (Idle individuals
        # whose cursor is behind `now` are handled by the empty-window quiet
        # flag below, not by the stale timestamp_end metadata.)
        logger.info(f"Skip Movebank {log_reference} for no new data.")
        return {"skipped": "no_new_data"}

    # Size the fetch window by event density (shared with backfill via
    # _compute_batch_window): high-frequency individuals get a window that
    # should hold roughly HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events, assuming
    # an even distribution over the individual's active range. Size against `now`,
    # not timestamp_end: a stale timestamp_end would shrink the span and produce
    # needlessly small windows (extra Movebank calls) for high-frequency tags.
    total_seconds = (now - ind.timestamp_start).total_seconds()
    batch_window_size = _compute_batch_window(ind.number_of_events, total_seconds)
    if individual_state.window_seconds:
        # A previous run found the density-based window too dense to send; keep
        # the smaller size it settled on.
        batch_window_size = min(batch_window_size, timedelta(seconds=individual_state.window_seconds))
    min_window = timedelta(seconds=settings.MIN_BACKFILL_WINDOW_SECONDS)

    logger.info(
        f"For individual {ind.id} ({ind.nick_name}), using a window size of {batch_window_size} "
        f"({ind.number_of_events} events on record)."
    )

    device_name = _display_name(ind)
    auth_config = client.get_auth_config(integration)
    total_observations_sent = 0
    current_window_start = earliest_start
    if individual_state.scan_from and earliest_start < individual_state.scan_from <= highest_date:
        # A previous run ran out of time in an empty stretch: resume after it.
        current_window_start = individual_state.scan_from
    else:
        individual_state.scan_from = None
    saved_scan_from = individual_state.scan_from
    # Only a stretch scanned by an EARLIER run is skipped; within this run each
    # window still queries from the sensor cursors (keeping the accessory settling
    # re-read), as before.
    resume_from = individual_state.scan_from

    async def save_cursor_state():
        nonlocal saved_scan_from
        _pin_unset_sensor_cursors(individual_state, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
        await state_manager.set_state(
            integration_id,
            CURSOR_STATE_ACTION_ID,
            json.loads(individual_state.json()),
            source_id=ind.id,
        )
        saved_scan_from = individual_state.scan_from

    # Stop taking new windows well before the runner's hard limit: a window is
    # fetched and sent as a whole, so it must have time to finish once started.
    deadline = time.monotonic() + 0.5 * settings.MAX_ACTION_EXECUTION_TIME

    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        while (current_window_start <= highest_date
               and total_observations_sent < MAXIMUM_RECORDS_PER_INDIVIDUAL
               and time.monotonic() < deadline):
            end_at = min(highest_date, current_window_start + batch_window_size)

            # One request per sensor type, each from its own cursor.
            # _advance_watermarks regroups the combined events by each event's
            # own sensor_type_id field; since every request here already filters
            # to a single sensor_type_id, Movebank reliably echoes that same id
            # back on every event it returns.
            events = []
            sensor_event_counts = {}  # per-sensor counts for logging + the fetched-any check
            window_interrupted = False
            window_started_at = now = None
            for sensor_type_id in sensor_type_ids:
                now = time.monotonic()
                if window_started_at is None:
                    window_started_at = now
                if now >= deadline:
                    window_interrupted = True
                    break
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue  # this sensor is already past the window
                query_start = _query_start_for_sensor(sensor_type_id, sensor_start, minimum_event_ids[sensor_type_id])
                if resume_from and query_start == sensor_start:
                    # A sensor that re-reads a settling margin keeps its full query,
                    # so late records below the resume point are still fetched
                    # before its event-id cursor moves past them.
                    query_start = max(query_start, resume_from)
                count = 0
                try:
                    async with movebank_slot(auth_config.username):
                        async for event in mb.get_individual_events_by_time(
                            study_id=action_config.study_id,
                            individual_id=ind.id,
                            timestamp_start=query_start,
                            timestamp_end=end_at,
                            sensor_type_ids=[sensor_type_id],
                            minimum_event_id=minimum_event_ids[sensor_type_id],
                        ):
                            events.append(event)  # single combined list; no per-sensor copy
                            count += 1
                except NoConnectionSlot:
                    # Budget exhausted (e.g. a backfill is saturating it). Prior
                    # windows already persisted their cursors, so skip cleanly and
                    # resume from the saved cursor on the next scheduled tick —
                    # rather than raising a noisy action failure. This window's
                    # partial fetch is discarded (not yet sent) and re-fetched next
                    # tick; the event-id filter dedups.
                    logger.info(
                        f"Skipping {log_reference}: Movebank connection budget exhausted; "
                        "resuming from the saved cursor on the next tick."
                    )
                    if individual_state.scan_from != saved_scan_from:
                        await save_cursor_state()  # keep this run's empty-stretch progress
                    return {"skipped": "no_connection_slot", "observations_sent": total_observations_sent}
                sensor_event_counts[sensor_type_id] = count

            if window_interrupted:
                # Out of time mid-window: nothing from it was sent, so drop the
                # partial fetch and resume from the saved cursor next tick.
                if (sensor_event_counts and batch_window_size > min_window
                        and _window_fetch_outran_budget(window_started_at, 0.5 * settings.MAX_ACTION_EXECUTION_TIME, now=now)):
                    # The fetch itself was too slow for the budget: retry a
                    # smaller window next time so a repeat can still make progress.
                    batch_window_size = max(min_window, batch_window_size / 2)
                    individual_state.window_seconds = batch_window_size.total_seconds()
                    await save_cursor_state()
                logger.info(f"Stopping {log_reference}: time budget used; resuming from the saved cursor.")
                break

            if not sensor_event_counts:
                # Every sensor was already past this window: advance without
                # touching the quiet flag or the saved cursor (v1 behavior).
                current_window_start = current_window_start + batch_window_size
                continue

            observations = [
                obs for event in events
                if (obs := build_observation(event=event, device_name=device_name)) is not None
            ]

            # Overflow guard (as in the backfill): a window too dense to send in
            # one run is discarded, shrunk, and re-fetched; the cursor stays put.
            if len(observations) > settings.MAX_RECORDS_PER_PULL_WINDOW and batch_window_size > min_window:
                shrunk = (batch_window_size.total_seconds()
                          * settings.MAX_RECORDS_PER_PULL_WINDOW / len(observations)
                          * settings.BACKFILL_WINDOW_SHRINK_SAFETY)
                batch_window_size = max(min_window, timedelta(seconds=shrunk))
                individual_state.window_seconds = batch_window_size.total_seconds()
                await save_cursor_state()
                logger.info(
                    f"Window for {log_reference} over cap ({len(observations)} > "
                    f"{settings.MAX_RECORDS_PER_PULL_WINDOW}); shrunk to {batch_window_size.total_seconds():.0f}s, retrying"
                )
                continue
            if len(observations) > settings.MAX_RECORDS_PER_PULL_WINDOW:
                logger.warning(
                    f"Window for {log_reference} at floor ({settings.MIN_BACKFILL_WINDOW_SECONDS}s) still over cap "
                    f"({len(observations)} observations); sending anyway"
                )

            for batch in chunks(observations, settings.OBSERVATIONS_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)

            # Advance per-sensor cursors from what actually came back.
            _advance_watermarks(individual_state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)

            logger.info(
                f"Processed Movebank data for {log_reference}: {len(observations)} observations, "
                f"events by sensor: {sensor_event_counts}"
            )

            if events:
                # Persist cursors whenever events were processed — sends happened
                # above, and even if the transform dropped every record, advancing
                # keeps unusable events from being refetched on the next run.
                individual_state.scan_from = None
                resume_from = None
                await state_manager.set_state(
                    integration_id,
                    CURSOR_STATE_ACTION_ID,
                    json.loads(individual_state.json()),
                    source_id=ind.id,
                )
                saved_scan_from = None
            else:
                # Remember how far the empty stretch has been scanned, in case
                # the run stops before reaching the present.
                individual_state.scan_from = current_window_start + batch_window_size
                if end_at >= highest_date:
                    # Nothing up to the present: back off from this individual for a while.
                    await state_manager.set_state(
                        integration_id,
                        QUIET_STATE_ACTION_ID,
                        {"quiet": True},
                        source_id=ind.id,
                        expire=QUIET_PERIOD_SECONDS,
                    )

            total_observations_sent += len(observations)
            current_window_start = current_window_start + batch_window_size

        if current_window_start > highest_date:
            # Scanned up to the present: the next run starts from the sensor cursors again.
            individual_state.scan_from = None
        if individual_state.scan_from != saved_scan_from:
            await save_cursor_state()

    return {"observations_sent": total_observations_sent}


def _backfill_dry_run_report(job_id, action_config, individuals, ranges, queued, skipped_existing, now) -> dict:
    """What a backfill with this config would do, without doing it. One row per
    individual: its first Movebank record, the coverage floor recorded so far
    (None when it has never been pulled), the start the backfill would use, and
    the span still missing. Sorted so the largest gaps come first."""
    queued_ids = {i.id for i in queued}
    skipped = set(skipped_existing)
    rows = []
    for i in individuals:
        ts = i.timestamp_start
        first = _ensure_utc(ts if isinstance(ts, datetime) else parse_date(ts)) if ts else None
        if i.id in skipped:
            rows.append({"individual_id": i.id, "local_identifier": i.local_identifier,
                         "first_record": first.isoformat() if first else None, "coverage_floor": None,
                         "status": "skipped_existing", "days_missing": None})
            continue
        start_dt, end_dt = ranges[i.id]
        never_pulled = end_dt == now
        missing = max(0, (end_dt - start_dt).days) if i.id in queued_ids else 0
        rows.append({
            "individual_id": i.id, "local_identifier": i.local_identifier,
            "first_record": first.isoformat() if first else None,
            "coverage_floor": None if never_pulled else end_dt.isoformat(),
            "would_start": start_dt.isoformat(),
            "status": "needs_backfill" if i.id in queued_ids else "covered",
            "days_missing": missing,
        })
    rows.sort(key=lambda r: (-(r["days_missing"] or 0), r["local_identifier"] or ""))
    needs = sum(1 for r in rows if r["status"] == "needs_backfill")
    covered = sum(1 for r in rows if r["status"] == "covered")
    logger.info(
        f"Backfill {job_id} dry run for study {action_config.study_id}: {needs} of {len(rows)} individuals "
        f"need a backfill ({sum(r['days_missing'] or 0 for r in rows)} individual-days missing), "
        f"{covered} covered, {len(skipped)} skipped (history but no coverage floor)"
    )
    return {"job_id": job_id, "dry_run": True, "individuals": len(rows), "needs_backfill": needs,
            "covered": covered, "skipped_existing": len(skipped), "report": rows}


def _resolve_start(start, ind) -> datetime:
    """A concrete datetime for this individual: the operator's date, or for
    'all' the individual's earliest record (falling back to the lookback floor).

    `start` is the validated config value — the literal "all", or a date string
    (BackfillConfig.start). A datetime is also accepted for programmatic callers.
    """
    if isinstance(start, datetime):
        return _clamp_start_to_first_record(_ensure_utc(start), ind)
    if isinstance(start, str) and start.strip().lower() != "all":
        return _clamp_start_to_first_record(_ensure_utc(parse_date(start)), ind)
    # "all": the individual's first record itself. Deliberately NOT the clamp
    # margin: completed "all" runs recorded coverage_start == timestamp_start,
    # and a margin here would re-queue a one-day sliver for every such
    # individual on each re-run.
    if ind.timestamp_start:
        return ind.timestamp_start
    return datetime.now(tz=timezone.utc) - timedelta(days=3650)  # ~10y floor


def _clamp_start_to_first_record(start: datetime, ind) -> datetime:
    """Pull an operator's start up to the individual's first Movebank record
    (minus BACKFILL_START_CLAMP_MARGIN_SECONDS) when it lies earlier.

    Movebank reports `timestamp_start` per individual; nothing exists before
    it, so a 2016 start on a tag deployed in 2021 would otherwise send the
    cascade through five years of empty windows after the real data is done.
    The margin covers the row's second-level truncation and any event that
    predates the deployment summary.
    """
    if not ind.timestamp_start:
        return start
    floor = ind.timestamp_start - timedelta(seconds=settings.BACKFILL_START_CLAMP_MARGIN_SECONDS)
    if start < floor:
        logger.debug(
            f"Backfill start {start.isoformat()} for individual {ind.id} precedes its first "
            f"Movebank record {ind.timestamp_start.isoformat()}; clamped to {floor.isoformat()}"
        )
        return floor
    return start


async def _seed_pull_cursor_at_end(integration_id: str, study_id: str, ind, end: datetime) -> None:
    """Claim [end, +inf) for the steady-state pull cursor before backfill starts.

    Without this, an individual with no existing pull cursor lets the */10
    pull compute its own lookback-based start and reach back into the range
    backfill is about to cover — shipping duplicate observations, since
    loaded_at doesn't dedup them.
    """
    state = IndividualState(individual_id=ind.id, study_id=study_id, local_identifier=ind.local_identifier)
    state.coverage_start = end  # pull owns [end, +inf); everything before is backfill's to fill
    for stid in _supported_sensor_type_ids(ind):
        state.update_sensor_state(stid, end, 0)
    await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID, json.loads(state.json()), source_id=ind.id)


@action_title("(3) Backfill Movebank Data for a Study")
@activity_logger()
async def action_backfill(integration, action_config: BackfillConfig):
    integration_id = str(integration.id)
    now = datetime.now(tz=timezone.utc)

    # Deterministic job id from the USER-SUPPLIED parameters only: a redelivered
    # backfill command resumes the same job, and a later cancel/restart with the
    # same parameters targets it. Deliberately NOT derived from the fetched
    # study membership — that can drift while a whole-study job runs, which
    # would hash a cancel to a different id than the job it's aimed at.
    # individual_ids arrives deduped and sorted (BackfillConfig canonicalises it),
    # so equivalent spellings of the same request hash to the same job.
    ids_repr = action_config.individual_ids or "all"
    job_seed = f"{action_config.study_id}:{ids_repr}:{action_config.start}"
    job_id = "job-" + hashlib.sha256(job_seed.encode()).hexdigest()[:12]
    job = BackfillJob(integration_id, job_id)

    if action_config.cancel:
        # Operator stop: flag the job so nothing new dispatches and every
        # in-flight individual unwinds at its next step (they carry their
        # config in the PubSub message, so Redis cleanup alone can't stop
        # them — see action_backfill_events_for_individual's entry check).
        # Handled before any Movebank traffic: cancel stays usable even when
        # Movebank itself is down or saturated.
        if not await job.exists():
            logger.info(f"Backfill {job_id}: cancel requested but no active job")
            return {"job_id": job_id, "cancelled": False, "active": False}
        snap = await job.snapshot()
        await job.cancel()
        if snap["in_flight"] > 0:
            logger.warning(
                f"Backfill {job_id}: cancelled — dropped {snap['pending_remaining']} pending "
                f"individuals; {snap['in_flight']} in flight will stop at their next step"
            )
        else:
            # Nothing in flight to observe the flag and retire the job, so do it
            # here. Still a TTL'd tombstone rather than an outright delete: a
            # duplicate delivery of an already-finalized step can arrive after
            # in_flight has hit 0, and must not restart the cascade.
            await job.retire_cancelled(ttl_seconds=settings.CANCELLED_JOB_TOMBSTONE_SECONDS)
            logger.warning(f"Backfill {job_id}: cancelled idle job — retired with a tombstone")
        return {"job_id": job_id, "cancelled": True,
                "pending_cleared": snap["pending_remaining"], "in_flight": snap["in_flight"]}

    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        async with movebank_slot(auth_config.username):
            rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    individuals = list(generate_individuals(rows))
    if action_config.individual_ids:
        wanted = set(action_config.individual_ids)
        individuals = [i for i in individuals if i.id in wanted]

    if action_config.restart:
        # Operator recovery: wipe the deterministic job's state and its
        # per-individual backfill watermarks so it re-seeds and re-runs from
        # `start`, instead of returning already_active forever on a wedged job.
        await job.clear()
        for i in individuals:
            await state_manager.delete_state(
                integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=f"{job_id}.{i.id}"
            )
        await state_manager.delete_state(integration_id, f"backfill_progress.{job_id}")
        logger.warning(
            f"Backfill {job_id}: restart requested — cleared prior job state and watermarks"
        )

    # Idempotent seed: a redelivered/double-clicked backfill command hashes to
    # the SAME job_id. If that job is already active, re-seeding would
    # re-zero its counters and re-RPUSH individuals that are already in
    # flight or already dispatched — double-dispatching them. Bail out early.
    if not action_config.dry_run and await job.exists():
        snap = await job.snapshot()
        if await job.is_cancelled():
            # A cancelled job (possibly just its TTL'd tombstone) occupies this
            # job_id. While its in-flight steps are still unwinding, re-seeding
            # would double-dispatch exactly those individuals — refuse.
            if snap["in_flight"] > 0:
                logger.info(
                    f"Backfill {job_id}: cancelled job still draining "
                    f"({snap['in_flight']} in flight); not re-seeding"
                )
                return {"job_id": job_id, "cancelled_draining": True, "in_flight": snap["in_flight"]}
            # Fully drained: this run is an intentional re-start of the same
            # backfill, so drop the tombstone and seed fresh below. Retained
            # watermarks make each individual resume where cancel stopped it.
            await job.clear()
            logger.info(f"Backfill {job_id}: prior run was cancelled and has drained; seeding fresh")
        elif snap["in_flight"] == 0 and snap["pending_remaining"] > 0:
            # A prior invocation seeded the job (meta + pending) but crashed
            # before/while dispatching — nothing is in flight yet work is still
            # queued. There's no PubSub redelivery to restart it, so a re-run
            # would otherwise return already_active forever. Resume instead:
            # dispatch up to K from the pending queue (configs were stored at
            # seed time; _dispatch_backfill_individual skips any that are missing).
            logger.info(
                f"Backfill {job_id}: resuming stalled job "
                f"({snap['pending_remaining']} pending, none in flight)"
            )
            k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
            dispatched = 0
            while dispatched < k:
                next_id = await job.next_individual()
                if next_id is None:
                    break
                await _dispatch_backfill_individual(integration_id, job, next_id)
                dispatched += 1
            return {"job_id": job_id, "resumed": True, "dispatched": dispatched}
        else:
            logger.info(f"Backfill {job_id}: job already active, skipping re-seed")
            return {"job_id": job_id, "already_active": True}

    # Resolve each individual's backfill end from its steady-state coverage
    # floor. Backfill fills [start, end); the pull owns [end, +inf).
    #   - no cursor            -> end = now, and seed the pull cursor at now
    #   - coverage_start set   -> end = coverage_start (fill history behind the pull)
    #   - legacy seed (all event_id 0, no coverage_start) -> end = its timestamp
    #   - legacy real cursor (event_id > 0, no coverage_start) -> skip (unknown floor)
    # Parse the operator's start once; "all" stays the literal and resolves
    # per individual to its first record.
    operator_start = action_config.start
    if isinstance(operator_start, str) and operator_start.strip().lower() != "all":
        operator_start = _ensure_utc(parse_date(operator_start))
    ranges = {}
    seed_needed = set()
    skipped_existing = []
    for i in individuals:
        saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=i.id)
        if not saved:
            end_dt = now
            seed_needed.add(i.id)
        else:
            state = IndividualState.parse_obj(saved)
            if state.coverage_start is not None:
                end_dt = state.coverage_start
            elif all((ss.highest_event_id or 0) == 0 for ss in state.sensor_states.values()):
                stamps = [ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp]
                end_dt = min(stamps) if stamps else now
            else:
                skipped_existing.append(i.id)
                continue
            latest = max((ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp), default=None)
            has_real_coverage = any((ss.highest_event_id or 0) > 0 for ss in state.sensor_states.values())
            if has_real_coverage and latest and end_dt > latest - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS):
                logger.warning(
                    f"Backfill {job_id}/{i.id}: coverage span is below the accessory settling "
                    "margin; accessory rows near the boundary may duplicate."
                )
        ranges[i.id] = (_resolve_start(operator_start, i), end_dt)
    if skipped_existing:
        logger.warning(
            f"Backfill {job_id}: skipped {len(skipped_existing)} individuals with existing collection "
            "history but no recorded coverage floor; clear their cursor to force a full backfill."
        )

    # Only individuals with a non-empty range are worth queuing.
    queued = [i for i in individuals if i.id in ranges and ranges[i.id][0] < ranges[i.id][1]]
    if isinstance(operator_start, datetime):
        range_repr = f"[{action_config.start} (clamped to each individual's first record) .. per-individual coverage floor)"
        clamped = [i for i in queued if ranges[i.id][0] > operator_start]
        if clamped:
            logger.info(
                f"Backfill {job_id}: start {operator_start.date()} precedes the first Movebank record of "
                f"{len(clamped)} of {len(queued)} individuals; their start was clamped to that record "
                f"(earliest {min(ranges[i.id][0] for i in clamped).date()})"
            )
    else:
        range_repr = "[each individual's first record .. per-individual coverage floor)"
    if action_config.dry_run:
        # Audit only: report the plan the seed below would act on, per
        # individual, and touch nothing (no job, no cursors, no dispatch).
        return _backfill_dry_run_report(job_id, action_config, individuals, ranges, queued, skipped_existing, now)

    await job.seed([i.id for i in queued], total=len(queued), range_repr=range_repr)

    logger.info(f"Backfill {job_id} for study {action_config.study_id}: {len(queued)} individuals, range {range_repr}")

    # Store each queued individual's sub-action config up front, so the rolling
    # dispatch (here and from the sub-action's finalize step) always reads
    # from the same place regardless of when an individual is popped. Also
    # claim the pull cursor's floor for individuals that don't have one yet,
    # so a concurrent steady-state pull run can't reach back into the range
    # backfill is about to cover (see _seed_pull_cursor_at_end).
    for i in queued:
        start_dt, end_dt = ranges[i.id]
        # Only fresh individuals need a boundary-claim seed; an already-cursored
        # individual's cursor already owns [coverage_start, +inf).
        if i.id in seed_needed:
            await _seed_pull_cursor_at_end(integration_id, action_config.study_id, i, end_dt)
        await job.put_individual_config(
            i.id,
            BackfillEventsForIndividualConfig(
                study_id=action_config.study_id, individual=i,
                job_id=job_id, start=start_dt, end=end_dt,
            ).json(),
        )

    k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
    dispatched = 0
    while dispatched < k:
        next_id = await job.next_individual()
        if next_id is None:
            break
        await _dispatch_backfill_individual(integration_id, job, next_id)
        dispatched += 1

    return {
        "job_id": job_id, "individuals": len(queued), "dispatched": dispatched,
        "skipped_existing": len(skipped_existing),
    }


def _next_step(action_config: BackfillEventsForIndividualConfig) -> BackfillEventsForIndividualConfig:
    """The config to publish for this individual's next step (step_seq + 1)."""
    return action_config.copy(update={"step_seq": action_config.step_seq + 1})


async def _delay_before_retrigger(base_seconds: int) -> None:
    """Wait inside the step before re-publishing it, +/-50% jitter, capped at 60s
    so a late refusal still finishes under the step's hard cancel."""
    if base_seconds <= 0:
        return
    await asyncio.sleep(min(60.0, base_seconds * random.uniform(0.5, 1.5)))


async def _dispatch_backfill_individual(integration_id, job, individual_id):
    """Read the stored per-individual config and trigger the sub-action.

    Single dispatch path used both for the backfill action's first wave and
    for the sub-action's rolling dispatch-next on completion. If an
    individual's config is missing (e.g. a lost/evicted Redis entry), this
    advances to the next pending individual instead of stalling the driver
    chain — bounded by the queue's length at call time so a fully-missing
    configs hash can't loop forever.
    """
    snap = await job.snapshot()
    max_attempts = snap["pending_remaining"] + 1  # +1 for the individual_id passed in
    for _ in range(max_attempts):
        if individual_id is None:
            return
        blob = await job.get_individual_config(individual_id)
        if blob is not None:
            cfg = BackfillEventsForIndividualConfig.parse_raw(blob).copy(
                update={"step_seq": 0, "run_id": await job.run_id()}
            )
            await job.reset_steps(individual_id)
            await job.incr_in_flight()
            try:
                await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=cfg)
            except Exception:
                # Publish failed (e.g. commands topic unset, transient PubSub
                # error). Roll back the increment and return the individual to
                # the queue so it isn't lost — an inflated in_flight would also
                # block the resume path (which keys on in_flight == 0). Re-raise
                # so a persistent failure stays visible; state is now consistent
                # for a later resume.
                await job.decr_in_flight()
                await job.requeue(individual_id)
                raise
            return
        logger.warning(f"Backfill {job.job_id}: no stored config for individual {individual_id}; skipping")
        individual_id = await job.next_individual()
    logger.warning(f"Backfill {job.job_id}: exhausted pending queue looking for a valid config to dispatch")


async def _merge_backfill_into_pull_cursor(
        integration_id, ind, action_config, state, *, coverage_start, lower_only=False, create_if_missing=True
):
    """Merge the backfill watermark FORWARD into the steady-state pull cursor and
    set its coverage floor.

    Per sensor, take max(existing, backfill) on both the timestamp and the
    highest event id. This carries backfill's event-ids forward — so the pull's
    accessory settling re-read dedups against them — without ever moving a
    running pull's cursor backward (action_backfill may already have claimed a
    forward cursor at job start via _seed_pull_cursor_at_end, and a live pull may
    have advanced it further while backfill ran). In reverse backfill the sent
    windows are processed newest-first, so `state` holds the most-recent
    event-ids (see _advance_watermarks' cumulative-max) — exactly what the
    settling re-read needs to filter the seam.

    coverage_start handling:
      - lower_only=False (success finalize): set coverage_start = coverage_start
        (the backfill's `start` — full coverage claimed down to there).
      - lower_only=True (abandon): only lower coverage_start toward the reached
        floor; never raise it, so a prior deeper backfill's floor is preserved.

    create_if_missing=False (abandon) skips writing entirely when no pull cursor
    exists yet — in production the seed always ran first, and we don't want a
    caller that skipped the seed to materialise a cursor here.
    """
    existing_raw = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    if not existing_raw and not create_if_missing:
        return
    existed = bool(existing_raw)
    existing_state = IndividualState.parse_obj(existing_raw) if existing_raw else IndividualState(
        individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
    )
    # Track whether anything actually changes: this key is also written by the
    # scheduled pull, so every needless get/merge/set is another chance to
    # clobber a concurrent pull-cursor advance (TOCTOU). Only write when a field
    # really moves — or when we just created the cursor.
    changed = not existed
    for stid_str, backfill_ss in state.sensor_states.items():
        stid = int(stid_str)
        existing_ss = existing_state.get_sensor_state(stid)
        candidate_stamps = [t for t in (existing_ss.latest_timestamp, backfill_ss.latest_timestamp) if t]
        if candidate_stamps:
            new_ts = max(candidate_stamps)
            new_event_id = max(existing_ss.highest_event_id or 0, backfill_ss.highest_event_id or 0)
            if new_ts != existing_ss.latest_timestamp or new_event_id != (existing_ss.highest_event_id or 0):
                existing_state.update_sensor_state(stid, new_ts, new_event_id)
                changed = True
    if lower_only:
        if (existing_state.coverage_start is None or coverage_start < existing_state.coverage_start) \
                and existing_state.coverage_start != coverage_start:
            existing_state.coverage_start = coverage_start
            changed = True
    elif existing_state.coverage_start != coverage_start:
        existing_state.coverage_start = coverage_start
        changed = True
    if changed:
        await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                      json.loads(existing_state.json()), source_id=ind.id)


async def _finalize_backfill_individual(integration_id, job, ind, action_config, *, observations, state=None):
    if state is not None:
        # Success: carry backfill's per-sensor maxima forward and claim coverage
        # down to action_config.start.
        await _merge_backfill_into_pull_cursor(
            integration_id, ind, action_config, state, coverage_start=action_config.start
        )
    await job.record_completion(observations)
    await job.decr_in_flight()

    if state is not None:
        # Best-effort watermark cleanup, AFTER the job counters are updated:
        # the watermark has served its purpose (merged into the pull cursor
        # above), but a Redis hiccup here must not leave in_flight un-decremented
        # and the job stuck. A leftover watermark is harmless (this individual
        # is done; a re-run is refused by the idempotent-seed guard). NOT deleted
        # on the abandon path (state is None) so a re-run resumes from it.
        try:
            await state_manager.delete_state(
                integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=f"{action_config.job_id}.{ind.id}"
            )
        except Exception as exc:
            logger.warning(f"Backfill {action_config.job_id}/{ind.id}: watermark cleanup failed (harmless): {exc}")

    if await job.is_done():
        snap = await job.snapshot()
        logger.info(f"Backfill {action_config.job_id} finished: {snap['completed']}/{snap['total']} "
                    f"individuals, {snap['observations_sent']} observations")
    else:
        if await state_manager.set_if_absent(
            integration_id, f"backfill_progress.{action_config.job_id}", ttl_seconds=BACKFILL_PROGRESS_THROTTLE_SECONDS
        ):
            snap = await job.snapshot()
            logger.info(f"Backfill {action_config.job_id} progress: {snap['completed']}/{snap['total']} "
                        f"individuals, ~{snap['observations_sent']} observations")
        next_id = await job.next_individual()
        if next_id is not None:
            await _dispatch_backfill_individual(integration_id, job, next_id)

    return {"status": "completed", "observations_sent": observations}


def _saved_scan_from(saved) -> Optional[datetime]:
    """Parse the persisted descent position out of a backfill watermark blob."""
    if saved and saved.get("scan_from"):
        try:
            return _ensure_utc(parse_date(saved["scan_from"]))
        except Exception:
            return None
    return None


@dataclass
class _StepLease:
    """What the step holds while it runs: its lease token and sequence number,
    whether it already handed the chain to its successor (sync mode), and
    whether the lease was lost to a newer delivery (the heartbeat then cancels
    the step task so two workers never run the same individual for long)."""
    token: str
    seq: int
    handed_off: bool = False
    # lost: Redis said the lease is someone else's (a redelivery took it over).
    # unconfirmed: Redis stopped answering and the last confirmed expiry has
    # passed; nobody is known to hold the step, so it must be re-published.
    lost: bool = False
    unconfirmed: bool = False
    task: Optional[asyncio.Task] = None
    heartbeat: Optional[asyncio.Task] = None
    # Monotonic time until which Redis last confirmed the lease is ours. Set on
    # claim and on every successful renewal; renewals that error cannot move it.
    expires_at: float = 0.0


@activity_logger()
async def action_backfill_events_for_individual(integration, action_config: BackfillEventsForIndividualConfig):
    step_started = time.monotonic()
    ind = action_config.individual
    job = BackfillJob(str(integration.id), action_config.job_id)
    log_reference = f"job:{action_config.job_id},individual:{ind.id}"
    seq = action_config.step_seq

    # Pub/Sub is at-least-once. A second delivery of a step must not start a
    # second chain: every chain is self-perpetuating, so one duplicate becomes a
    # permanent parallel worker fetching and sending the same windows. Take this
    # step's lease first; it is renewed while the step runs, advanced when the
    # step hands off, and released (or left to lapse) when the step dies, so an
    # unfinished step can still be redelivered and run. A step dispatched under
    # an earlier run of this job (before a cancel+resume or a restart) is dropped.
    lease = _StepLease(token=uuid.uuid4().hex, seq=seq, task=asyncio.current_task())
    claim = await _claim_backfill_step(job, ind.id, seq, lease.token, action_config.run_id)
    if claim != STEP_ACQUIRED:
        logger.info(f"Backfill {log_reference}: duplicate delivery of step {seq} ({claim}); skipped")
        return {"status": "duplicate", "step_seq": seq}
    lease.expires_at = time.monotonic() + settings.BACKFILL_STEP_LEASE_SECONDS

    lease.heartbeat = asyncio.create_task(_hold_backfill_step_lease(job, ind.id, lease, log_reference))
    try:
        result = await _run_backfill_step(integration, action_config, job, step_started, lease)
    except asyncio.CancelledError:
        lease.heartbeat.cancel()
        if lease.lost:
            # A newer delivery took the lease (ours lapsed, e.g. a stalled
            # renewal) and is running this step; we stopped so the two never
            # overlap for more than one heartbeat. Nothing to hand off.
            logger.info(f"Backfill {log_reference}: step {seq} superseded by a newer delivery; stopped")
            return {"status": "superseded", "step_seq": seq}
        await _release_backfill_step(job, ind.id, lease.token, log_reference)
        if lease.unconfirmed:
            # Redis went quiet past our lease and nobody is known to hold the
            # step. Acking here would end the chain (main.py acks every result),
            # so re-publish THIS step unchanged: the redelivery claims the same
            # number once the lease is released or lapsed and resumes from the
            # persisted cursor. Pub/Sub does not depend on Redis, and the wait
            # gives Redis time to come back before the step is delivered again.
            await _delay_before_retrigger(settings.BACKFILL_NO_SLOT_RETRY_DELAY_SECONDS)
            await trigger_action(integration_id=str(integration.id), action_id=BACKFILL_ACTION_ID, config=action_config)
            logger.warning(f"Backfill {log_reference}: step {seq} stopped with its lease unconfirmed; re-published")
            return {"status": "retry", "reason": "lease_unconfirmed", "step_seq": seq}
        raise
    except BaseException:
        lease.heartbeat.cancel()
        await _release_backfill_step(job, ind.id, lease.token, log_reference)
        raise
    lease.heartbeat.cancel()
    if not lease.handed_off:
        try:
            if not await job.finish_step(ind.id, seq, lease.token):
                logger.warning(f"Backfill {log_reference}: step {seq} lease lapsed before hand-off")
        except Exception as exc:
            # The successor is already published (or the individual finalized).
            # Raising would only invite a redelivery that re-runs this step.
            logger.warning(f"Backfill {log_reference}: step {seq} hand-off failed: {exc}")
    return result


async def _release_backfill_step(job: BackfillJob, individual_id: str, token: str, log_reference: str) -> None:
    try:
        await job.release_step(individual_id, token)
    except Exception as exc:
        # The lease lapses on its own once the heartbeat stops.
        logger.warning(f"Backfill {log_reference}: step lease release failed (lapses in "
                       f"{settings.BACKFILL_STEP_LEASE_SECONDS}s): {exc}")


async def _claim_backfill_step(job: BackfillJob, individual_id: str, seq: int, token: str, run_id: str = "") -> str:
    """Claim the step, waiting while another worker holds the lease. A live
    holder renews its lease, so still BUSY after one full lease period means a
    concurrent duplicate (drop it); a dead holder's lease lapses inside the wait
    and this delivery takes the step over. The successor of a step also waits
    here for the few milliseconds between its publish and the hand-off."""
    lease = settings.BACKFILL_STEP_LEASE_SECONDS
    give_up_at = time.monotonic() + 1.5 * lease
    while True:
        claim = await job.claim_step(individual_id, seq, token, ttl_seconds=lease, run_id=run_id)
        if claim != STEP_BUSY or time.monotonic() >= give_up_at:
            return claim
        await asyncio.sleep(settings.BACKFILL_STEP_CLAIM_POLL_SECONDS)


async def _hold_backfill_step_lease(job: BackfillJob, individual_id: str, lease: _StepLease, log_reference: str) -> None:
    """Renew the step lease every third of its TTL until cancelled. If the
    lease is lost (lapsed and taken by a redelivery), cancel the step task:
    the other worker owns the individual now, and letting both run would send
    the same windows twice and move the shared cursors from two places."""
    ttl = settings.BACKFILL_STEP_LEASE_SECONDS
    while True:
        await asyncio.sleep(ttl / 3)
        try:
            # Bounded: a renewal that hangs (Redis not answering at all) must
            # not keep this loop from noticing that the lease has expired.
            renewed = await asyncio.wait_for(
                job.renew_step_lease(individual_id, lease.token, ttl_seconds=ttl), timeout=ttl / 3
            )
        except Exception as exc:
            # Redis did not answer. The lease is still ours until the last
            # confirmed expiry; past that, a redelivery may already hold it, so
            # stop exactly as if the renewal had said so.
            if time.monotonic() < lease.expires_at:
                logger.warning(f"Backfill {log_reference}: step lease renewal failed; "
                               f"{lease.expires_at - time.monotonic():.0f}s of lease left: {exc}")
                continue
            logger.warning(f"Backfill {log_reference}: step lease renewal failed and the lease has "
                           f"expired unconfirmed; stopping to re-publish the step: {exc}")
            lease.unconfirmed = True
            if lease.task is not None:
                lease.task.cancel()
            return
        if not renewed:
            logger.warning(f"Backfill {log_reference}: step lease lost; stopping in favour of the newer delivery")
            lease.lost = True
            if lease.task is not None:
                lease.task.cancel()
            return
        lease.expires_at = time.monotonic() + ttl


async def _publish_next_step(job: BackfillJob, integration_id: str,
                             action_config: BackfillEventsForIndividualConfig, lease: _StepLease) -> None:
    """Publish this individual's next step. Normally the successor is published
    first and the chain advanced after (so a failed publish leaves this step
    redeliverable); with TRIGGER_ACTIONS_ALWAYS_SYNC the successor runs inline
    inside trigger_action, so hand off first or it would wait on our own lease."""
    successor = _next_step(action_config)
    if settings.TRIGGER_ACTIONS_ALWAYS_SYNC and not lease.handed_off:
        if lease.heartbeat is not None:
            lease.heartbeat.cancel()
        lease.handed_off = await job.finish_step(action_config.individual.id, action_config.step_seq, lease.token)
    await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=successor)


async def _run_backfill_step(integration, action_config: BackfillEventsForIndividualConfig,
                             job: BackfillJob, step_started: float, lease: _StepLease):
    ind = action_config.individual
    integration_id = str(integration.id)
    watermark_source = f"{action_config.job_id}.{ind.id}"
    log_reference = f"job:{action_config.job_id},individual:{ind.id}"

    if await job.is_cancelled():
        # Every continuation path (dispatch, continued, retry, backoff) re-enters
        # here, so this single check stops the whole cascade within one step.
        # Carry whatever WAS sent (recent windows first) into the pull cursor so
        # its settling re-read dedups the seam, and lower coverage_start to the
        # reached floor — same bookkeeping as the abandon path. The watermark is
        # kept, so re-running the same backfill resumes this individual.
        saved = await state_manager.get_state(integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=watermark_source)
        if saved:
            state = IndividualState.parse_obj(saved)
            reached = _saved_scan_from(saved) or action_config.end
            await _merge_backfill_into_pull_cursor(
                integration_id, ind, action_config, state,
                coverage_start=reached, lower_only=True, create_if_missing=False,
            )
        remaining = await job.decr_in_flight()
        if remaining == 0:
            # Last in-flight unit: drop the working state, but keep the cancelled
            # flag as a TTL'd tombstone. A duplicate delivery of an
            # already-processed step can still arrive (at-least-once), and
            # without the flag it would read "not cancelled" and resume the
            # backfill from this individual's retained watermark.
            await job.retire_cancelled(ttl_seconds=settings.CANCELLED_JOB_TOMBSTONE_SECONDS)
            logger.info(f"Backfill {log_reference}: job cancelled; last in-flight individual "
                        "stopped — job retired with a tombstone")
        else:
            logger.info(f"Backfill {log_reference}: job cancelled; stopping "
                        f"({remaining} still in flight)")
        return {"status": "cancelled"}

    sensor_type_ids = _supported_sensor_type_ids(ind)
    if not sensor_type_ids:
        return await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)

    saved = await state_manager.get_state(integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=watermark_source)
    state = IndividualState.parse_obj(saved) if saved else IndividualState(
        individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
    )
    sensor_type_timestamps, minimum_event_ids = {}, {}
    for stid in sensor_type_ids:
        ss = state.get_sensor_state(stid)
        sensor_type_timestamps[stid] = ss.latest_timestamp or action_config.start
        minimum_event_ids[stid] = (ss.highest_event_id or 0) + 1

    # The scan position is a DURABLE cursor, persisted every window regardless
    # of whether events came back. Windows are anchored to it (below), not to
    # the per-sensor event cursor above — otherwise a sensor that returns
    # nothing for the whole range would never move its own cursor, and a
    # re-trigger after a budget-exhausted step would recompute the cursor from
    # that unmoved cursor and rescan the same empty span forever (livelock).
    # It descends from `end` toward `start`; at any point we have covered
    # `[scan_from, end)` and still owe `[start, scan_from)`.
    persisted_scan_from = _saved_scan_from(saved)

    auth_config = client.get_auth_config(integration)
    # Measured from the start of the request, so time spent waiting on the
    # step lease comes out of this step's budget, not on top of it.
    deadline = step_started + 0.8 * settings.MAX_ACTION_EXECUTION_TIME
    span_seconds = (action_config.end - action_config.start).total_seconds()
    window = _compute_batch_window(ind.number_of_events, span_seconds)
    if saved and saved.get("window_seconds"):
        try:
            window = timedelta(seconds=float(saved["window_seconds"]))
        except (TypeError, ValueError):
            pass
    min_window = timedelta(seconds=settings.MIN_BACKFILL_WINDOW_SECONDS)
    # Ceiling for empty-window growth: the absolute cap, but never more than a
    # few density-sized windows, so the first fetch that lands on data again
    # after a gap is bounded by a known multiple of the per-window record cap.
    density_window = max(min_window, _compute_batch_window(ind.number_of_events, span_seconds))
    max_window = max(min_window, min(
        timedelta(seconds=settings.MAX_BACKFILL_WINDOW_SECONDS),
        density_window * settings.BACKFILL_EMPTY_WINDOW_MAX_MULTIPLE,
    ))
    window = max(window, min_window)
    observations_sent = 0
    # Reverse (recent-first): `cursor` is the LOWER edge of coverage so far — we
    # have covered [cursor, end) and still owe [start, cursor). It begins at
    # `end` (= coverage_start) and descends toward `start`; a persisted position
    # resumes the descent. (The backfill watermark's scan_from stores it.)
    cursor = persisted_scan_from if persisted_scan_from is not None else action_config.end

    mb_client = client.MovebankClient(
        base_url=integration.base_url, username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    # Only the fetch/send cascade (and its pre-finalize watermark bookkeeping) is
    # guarded here: NoConnectionSlot and Movebank/transform errors originate in
    # this loop. The completion decision below (continue vs. finalize) must stay
    # OUTSIDE the try — if finalize's own post-processing (dispatch-next PubSub
    # publish, is_done, snapshot) raised while wrapped in this try, an already
    # -completed individual would be retried/abandoned and _finalize would run a
    # second time, double-counting record_completion/decr_in_flight.
    try:
        async with mb_client as mb:
            while cursor > action_config.start and time.monotonic() < deadline:
                window_lower = max(action_config.start, cursor - window)
                window_upper = cursor
                window_clipped = window_lower > cursor - window  # shortened by `start`
                events = []
                window_interrupted = False
                window_started_at = time.monotonic()
                for stid in sensor_type_ids:
                    # Check BETWEEN per-sensor fetches too, not just between
                    # windows: a dense window's fetches alone can exceed the
                    # budget, and an uncaught CancelledError from a killed
                    # task would leak in_flight and leave a permanent data gap
                    # (main.py's PubSub endpoint always acks — there is no
                    # redelivery to recover a killed step).
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        window_interrupted = True
                        break

                    async def _fetch_sensor(sensor_type_id=stid):
                        async with movebank_slot(auth_config.username):
                            async for event in mb.get_individual_events_by_time(
                                study_id=action_config.study_id, individual_id=ind.id,
                                timestamp_start=window_lower, timestamp_end=window_upper,
                                sensor_type_ids=[sensor_type_id], minimum_event_id=0,
                            ):
                                events.append(event)

                    # Bound the fetch itself by the remaining soft budget. A single
                    # slow fetch (a grown window landing on dense data, or a slow
                    # Movebank) must not run past the budget: unbounded, it would
                    # either finish late and proceed to sending, or reach the
                    # runner's hard cancellation with the enlarged window
                    # persisted and no continuation scheduled. Our own deadline
                    # routes it through the whole-window discard below (halve and
                    # re-trigger). Deliberately asyncio.wait + task.result() rather
                    # than wait_for: a timeout raised BY the fetch (the provider
                    # client's own TimeoutError) is the fetch failing, not budget
                    # exhaustion, and must reach the attempt-counted handler
                    # below like any other provider error (same split as
                    # action_runner's handler deadline).
                    fetch_task = asyncio.ensure_future(_fetch_sensor())
                    try:
                        done, _ = await asyncio.wait({fetch_task}, timeout=remaining)
                    except asyncio.CancelledError:
                        await _cancel_fetch(fetch_task)
                        raise
                    if done:
                        fetch_task.result()  # re-raises the fetch's own exception unchanged
                    else:
                        await _cancel_fetch(fetch_task)
                        window_interrupted = True
                        break

                if window_interrupted:
                    # Deadline hit mid-window (between sensors): discard the
                    # partial fetch — we only ever send a WHOLE window
                    # (atomicity). Cursor/scan_from stay put; the whole window is
                    # re-fetched next step. Nothing was sent, so there is no
                    # dedup floor to maintain and no duplicate on retry.
                    if _window_fetch_outran_budget(window_started_at, 0.8 * settings.MAX_ACTION_EXECUTION_TIME):
                        # THIS window's fetching ate most of the budget (a grown
                        # window landing on dense data, or a slow Movebank).
                        if window > min_window:
                            # Halve it so the retry can make progress instead
                            # of re-running the identical fetch.
                            window = max(min_window, window / 2)
                            blob = json.loads(state.json())
                            blob["scan_from"] = cursor.isoformat()
                            blob["window_seconds"] = window.total_seconds()
                            await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                                          blob, source_id=watermark_source)
                            logger.info(
                                f"Backfill {log_reference}: window fetch outran the step budget; "
                                f"shrunk to {window.total_seconds():.0f}s for the next step"
                            )
                        else:
                            # Already at the floor: nothing smaller to try.
                            # Fail the step so the attempt counter bounds the
                            # retries (BACKFILL_MAX_ATTEMPTS) instead of
                            # re-triggering the identical fetch forever.
                            raise RuntimeError(
                                f"window fetch at the {settings.MIN_BACKFILL_WINDOW_SECONDS}s floor "
                                f"outran the step budget"
                            )
                    # Otherwise this is just the normal end of a step: the
                    # window is fine, the budget was spent on earlier windows.
                    break

                # Timestamp-range ownership: keep only events whose timestamp is
                # in this window's half-open [lower, upper). Discards the client's
                # accessory 60-min pre-roll and the whole-second timestamp_end
                # truncation, so each event belongs to exactly one window and no
                # cross-window event-id dedup is needed. Unparseable timestamps
                # are dropped (build_observation would drop them anyway).
                kept = []
                for e in events:
                    try:
                        ts = _ensure_utc(parse_date(e.get("timestamp")))
                    except Exception:
                        continue
                    if window_lower <= ts < window_upper:
                        kept.append(e)

                # Overflow guard on KEPT (send) volume — see step-budget design.
                if (len(kept) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW
                        and window > min_window):
                    shrunk = (window.total_seconds()
                              * settings.MAX_RECORDS_PER_BACKFILL_WINDOW / len(kept)
                              * settings.BACKFILL_WINDOW_SHRINK_SAFETY)
                    window = max(min_window, timedelta(seconds=shrunk))
                    blob = json.loads(state.json())
                    blob["scan_from"] = cursor.isoformat()           # unchanged (cursor not advanced)
                    blob["window_seconds"] = window.total_seconds()
                    await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                                  blob, source_id=watermark_source)
                    logger.info(
                        f"Backfill {log_reference}: window over cap "
                        f"({len(kept)} > {settings.MAX_RECORDS_PER_BACKFILL_WINDOW}); "
                        f"shrunk to {window.total_seconds():.0f}s, retrying"
                    )
                    continue
                if len(kept) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW:
                    logger.warning(
                        f"Backfill {log_reference}: window at floor "
                        f"({settings.MIN_BACKFILL_WINDOW_SECONDS}s) still over cap "
                        f"({len(kept)} events); sending anyway"
                    )

                device_name = _display_name(ind)
                observations = [o for e in kept if (o := build_observation(event=e, device_name=device_name)) is not None]
                for batch in chunks(observations, settings.OBSERVATIONS_BATCH_SIZE):
                    await send_observations_to_gundi(observations=batch, integration_id=integration_id)

                # Advances per-sensor state from the kept events, cumulative-max
                # (see _advance_watermarks). In this REVERSE descent, windows are
                # visited recent-first, so `state` ends up holding the MOST
                # RECENT window's per-sensor timestamp/event-id — exactly what
                # _finalize_backfill_individual needs to merge forward into the
                # pull cursor. (An overwrite here would instead finalize with the
                # OLDEST window's values, handing the pull cursor a too-low
                # accessory event-id floor and letting its settling re-read
                # re-emit already-sent seam events as duplicates.)
                _advance_watermarks(state, kept, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
                observations_sent += len(observations)
                cursor = window_lower       # descend

                # An empty window means a data gap or the span before the tag's
                # first record: widen the next query (bounded) so the gap is
                # crossed in a handful of fetches. The overflow guard above
                # shrinks the window again the moment data reappears, at the
                # cost of one discarded fetch.
                if not events and not window_clipped and window < max_window:
                    growth = max(1.0, settings.BACKFILL_EMPTY_WINDOW_GROWTH)
                    window = max(min_window, min(max_window, window * growth))

                blob = json.loads(state.json())
                blob["scan_from"] = cursor.isoformat()
                blob["window_seconds"] = window.total_seconds()
                await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                              blob, source_id=watermark_source)

                # Per-step backstop: a dense window can return far more than
                # expected even with density-based sizing. Stop taking more
                # windows once this step's total crosses the cap, rather than
                # risking the invocation running past its execution budget.
                if observations_sent >= MAX_RECORDS_PER_BACKFILL_STEP:
                    break
    except asyncio.CancelledError:
        # Hard execution-timeout: asyncio.wait_for in the action runner cancels
        # this task, raising CancelledError (a BaseException — the handlers below
        # do NOT catch it). scan_from + window_seconds are persisted every
        # window, so no data is lost; a later trigger resumes from the last
        # completed window. We do not attempt async recovery here (re-trigger /
        # in_flight unwind): awaits during cancellation are themselves cancelled
        # and unreliable. Recover a job wedged by a timeout with restart=true.
        if lease.lost:
            logger.info(f"Backfill {log_reference}: stopping at scan_from={cursor.isoformat()}; "
                        "a newer delivery holds this step")
        else:
            logger.warning(
                f"Backfill {log_reference}: step hard-cancelled at scan_from={cursor.isoformat()}; "
                "resume on next trigger or re-run with restart=true"
            )
        raise
    except NoConnectionSlot:
        # No connection slot available: re-trigger THIS individual (same in-flight
        # unit continues) rather than losing the step or double-counting in-flight.
        # Wait first: an immediate re-publish loops hundreds of times per pull
        # tick while the slots are busy, and every push is a duplicate-delivery risk.
        await _delay_before_retrigger(settings.BACKFILL_NO_SLOT_RETRY_DELAY_SECONDS)
        await _publish_next_step(job, integration_id, action_config, lease)
        logger.info(f"Backfill {action_config.job_id}/{ind.id}: no connection slot, backing off")
        return {"status": "backoff"}
    except MBRateLimitError as exc:
        # Movebank asked us to slow down. Not a failed attempt: wait, then resume
        # from the persisted cursor without touching the attempts counter.
        await _delay_before_retrigger(settings.BACKFILL_RATE_LIMIT_RETRY_DELAY_SECONDS)
        await _publish_next_step(job, integration_id, action_config, lease)
        logger.info(f"Backfill {action_config.job_id}/{ind.id}: Movebank rate limited ({exc}); resuming after a pause")
        return {"status": "rate_limited"}
    except Exception as exc:
        attempts = await job.incr_attempts(ind.id)
        if attempts <= BACKFILL_MAX_ATTEMPTS:
            await _delay_before_retrigger(settings.BACKFILL_NO_SLOT_RETRY_DELAY_SECONDS)
            await _publish_next_step(job, integration_id, action_config, lease)
            logger.info(f"Backfill {action_config.job_id}/{ind.id}: attempt {attempts} failed ({exc}); retrying")
            return {"status": "retry", "attempts": attempts}
        logger.warning(f"Backfill {action_config.job_id}/{ind.id}: abandoned after {attempts} attempts: {exc}")
        # Carry whatever backfill DID send (recent windows, processed first)
        # forward into the pull cursor so its accessory settling re-read dedups
        # the seam, and lower coverage_start to the reached floor — WITHOUT
        # claiming full completion. Then finalize with state=None so it doesn't
        # merge again or move coverage_start to `start`.
        await _merge_backfill_into_pull_cursor(
            integration_id, ind, action_config, state,
            coverage_start=cursor, lower_only=True, create_if_missing=False,
        )
        result = await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)
        return {"status": "abandoned", **{k: v for k, v in result.items() if k != "status"}}

    # A successful step (fetch/send loop completed without raising) resets the
    # attempts counter — so transient errors spread thinly across a long
    # cascade don't accumulate toward abandonment; only CONSECUTIVE failures
    # count against BACKFILL_MAX_ATTEMPTS.
    await job.reset_attempts(ind.id)

    if cursor > action_config.start:
        # Budget exhausted mid-range: continue THIS individual on the next step
        # (descending from the persisted cursor).
        await _publish_next_step(job, integration_id, action_config, lease)
        logger.info(f"Backfill {log_reference} continued at {cursor.isoformat()} ({observations_sent} obs this step)")
        return {"status": "continued", "observations_sent": observations_sent}

    return await _finalize_backfill_individual(
        integration_id, job, ind, action_config, observations=observations_sent, state=state
    )
