import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient

import app.actions.client as client
from app import settings
from app.actions.backfill_queue import BackfillJob
from app.actions.client import IndividualState, generate_individuals
from app.actions.configurations import (
    AuthenticateConfig,
    BackfillConfig,
    BackfillEventsForIndividualConfig,
    PullObservationsConfig,
    PullEventsForIndividualConfig,
)
from app.actions.transform import _ensure_utc, build_observation, chunks
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
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
OBSERVATIONS_BATCH_SIZE = 200

CURSOR_STATE_ACTION_ID = "pull_events_for_individual"
QUIET_STATE_ACTION_ID = "pull_events_for_individual_quiet"

BACKFILL_ACTION_ID = "backfill_events_for_individual"
BACKFILL_WATERMARK_ACTION_ID = "backfill_watermark"
BACKFILL_PROGRESS_THROTTLE_SECONDS = 300


def _query_start_for_sensor(sensor_type_id: int, sensor_start: datetime) -> datetime:
    """Accessory-measurements can arrive hours late, so its query re-reads back
    ACCESSORY_SETTLING_HOURS; GPS is prompt and uses its exact cursor."""
    if sensor_type_id == MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID["accessory-measurements"]:
        return sensor_start - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS)
    return sensor_start


async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(
        f"Executing auth action with integration {integration} and action_config {action_config}..."
    )
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
            logger.info(f"Auth successful for integration '{integration.name}'. Token: '{token['api-token']}'")
            return {"valid_credentials": True}
        else:
            logger.error(f"Auth unsuccessful for integration {integration}.")
            return {"valid_credentials": False}


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
    async with mb_client as mb:
        async with movebank_slot(auth_config.username):
            individual_rows = await mb.get_individuals_by_study(study_id=action_config.study_id)

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
    # Some individuals don't provide timestamp_end, so resolve a reasonable value.
    resolved_individual_timestamp_end = ind.timestamp_end or now
    highest_date = min(now, resolved_individual_timestamp_end)

    sensor_type_labels = [label.strip().lower() for label in ind.sensor_type_ids.split(",")]
    sensor_type_ids = [
        MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID[label]
        for label in sensor_type_labels
        if label in MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID
    ]
    if not sensor_type_ids:
        logger.info(f"Skip Movebank {log_reference} — no supported sensor types in '{ind.sensor_type_ids}'.")
        return {"skipped": "no_supported_sensors"}

    saved_state = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    try:
        individual_state = IndividualState.parse_obj(saved_state) if saved_state else None
    except pydantic.ValidationError:
        logger.exception(f"Failed parsing saved state for {log_reference}; starting fresh.")
        individual_state = None
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback window.
    default_start = resolved_individual_timestamp_end - timedelta(hours=action_config.maximum_lookback_hours)
    sensor_type_timestamps = {}
    minimum_event_ids = {}
    for sensor_type_id in sensor_type_ids:
        sensor_state = individual_state.get_sensor_state(sensor_type_id)
        sensor_type_timestamps[sensor_type_id] = sensor_state.latest_timestamp or default_start
        minimum_event_ids[sensor_type_id] = (sensor_state.highest_event_id or 0) + 1

    earliest_start = min(sensor_type_timestamps.values())
    if earliest_start >= resolved_individual_timestamp_end:
        logger.info(f"Skip Movebank {log_reference} for no new data.")
        return {"skipped": "no_new_data"}

    # Size the fetch window by event density: high-frequency individuals get a
    # window that should hold roughly HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events,
    # assuming an even distribution over the individual's active range.
    if (ind.number_of_events or 0) > HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD:
        # resolved_individual_timestamp_end falls back to now when the individual
        # omits timestamp_end, so density sizing works for those individuals too.
        total_seconds = (resolved_individual_timestamp_end - ind.timestamp_start).total_seconds()
        batch_window_size = timedelta(
            seconds=(HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD / ind.number_of_events) * total_seconds
        )
    else:
        batch_window_size = DEFAULT_BATCH_WINDOW
    if batch_window_size <= timedelta(0):
        # A zero/negative window (timestamp_end <= timestamp_start) would never
        # advance current_window_start and spin the fetch loop forever.
        batch_window_size = DEFAULT_BATCH_WINDOW

    logger.info(
        f"For individual {ind.id} ({ind.nick_name}), using a window size of {batch_window_size} "
        f"({ind.number_of_events} events on record)."
    )

    device_name = ind.nick_name or ind.local_identifier or ind.ring_id
    auth_config = client.get_auth_config(integration)
    total_observations_sent = 0
    current_window_start = earliest_start

    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        while current_window_start <= highest_date and total_observations_sent < MAXIMUM_RECORDS_PER_INDIVIDUAL:
            end_at = min(highest_date, current_window_start + batch_window_size)

            # One request per sensor type, each from its own cursor. Events are
            # grouped by the requesting sensor (each fetch is already scoped to
            # one sensor type), so cursor advancement doesn't depend on Movebank
            # echoing sensor_type_id back on every event.
            events = []
            events_by_sensor = {}
            for sensor_type_id in sensor_type_ids:
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue  # this sensor is already past the window
                query_start = _query_start_for_sensor(sensor_type_id, sensor_start)
                sensor_events = []
                async with movebank_slot(auth_config.username):
                    async for event in mb.get_individual_events_by_time(
                        study_id=action_config.study_id,
                        individual_id=ind.id,
                        timestamp_start=query_start,
                        timestamp_end=end_at,
                        sensor_type_ids=[sensor_type_id],
                        minimum_event_id=minimum_event_ids[sensor_type_id],
                    ):
                        sensor_events.append(event)
                events_by_sensor[sensor_type_id] = sensor_events
                events.extend(sensor_events)

            if not events_by_sensor:
                # Every sensor was already past this window: advance without
                # touching the quiet flag or the saved cursor (v1 behavior).
                current_window_start = current_window_start + batch_window_size
                continue

            observations = [
                obs for event in events
                if (obs := build_observation(event=event, device_name=device_name)) is not None
            ]
            for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)

            # Advance per-sensor cursors from what actually came back.
            for sensor_type_id in sensor_type_ids:
                sensor_events = events_by_sensor.get(sensor_type_id, [])
                sensor_timestamps = []
                sensor_event_ids = []
                for event in sensor_events:
                    try:
                        sensor_timestamps.append(_ensure_utc(parse_date(event.get("timestamp"))))
                    except Exception:
                        pass
                    try:
                        sensor_event_ids.append(int(event.get("event_id")))
                    except (TypeError, ValueError):
                        pass
                if sensor_timestamps or sensor_event_ids:
                    # Events with unparseable timestamps still advance the event-id
                    # cursor (the client filters on minimum_event_id, so those records
                    # aren't refetched forever); the timestamp cursor only moves when
                    # a timestamp actually parsed.
                    new_latest = max(sensor_timestamps) if sensor_timestamps else sensor_type_timestamps[sensor_type_id]
                    new_max_event_id = max(sensor_event_ids) if sensor_event_ids else minimum_event_ids[sensor_type_id] - 1
                    individual_state.update_sensor_state(sensor_type_id, new_latest, new_max_event_id)
                    sensor_type_timestamps[sensor_type_id] = new_latest
                    minimum_event_ids[sensor_type_id] = new_max_event_id + 1

            logger.info(
                f"Processed Movebank data for {log_reference}: {len(observations)} observations, "
                f"events by sensor: { {k: len(v) for k, v in events_by_sensor.items()} }"
            )

            if events:
                # Persist cursors whenever events were processed — sends happened
                # above, and even if the transform dropped every record, advancing
                # keeps unusable events from being refetched on the next run.
                await state_manager.set_state(
                    integration_id,
                    CURSOR_STATE_ACTION_ID,
                    json.loads(individual_state.json()),
                    source_id=ind.id,
                )
            else:
                # No events in this window: back off from this individual for a while.
                await state_manager.set_state(
                    integration_id,
                    QUIET_STATE_ACTION_ID,
                    {"quiet": True},
                    source_id=ind.id,
                    expire=QUIET_PERIOD_SECONDS,
                )

            total_observations_sent += len(observations)
            current_window_start = current_window_start + batch_window_size

    return {"observations_sent": total_observations_sent}


def _resolve_start(start, ind) -> datetime:
    """A concrete datetime for this individual: the operator's date, or for
    'all' the individual's earliest record (falling back to the lookback floor)."""
    if isinstance(start, datetime):
        return start
    # start == "all"
    if ind.timestamp_start:
        return ind.timestamp_start
    return datetime.now(tz=timezone.utc) - timedelta(days=3650)  # ~10y floor


async def _resolve_end(integration_id: str, ind, now: datetime) -> datetime:
    """Freeze the boundary where backfill meets the steady-state pull: the
    individual's existing pull cursor if any, else now (and the pull is seeded
    to now by the individual sub-action on completion)."""
    saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    if saved:
        state = IndividualState.parse_obj(saved)
        stamps = [s.latest_timestamp for s in state.sensor_states.values() if s.latest_timestamp]
        if stamps:
            return min(stamps)
    return now


@activity_logger()
async def action_backfill(integration, action_config: BackfillConfig):
    integration_id = str(integration.id)
    now = datetime.now(tz=timezone.utc)
    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    individuals = list(generate_individuals(rows))
    if action_config.individual_ids:
        wanted = set(action_config.individual_ids)
        individuals = [i for i in individuals if i.id in wanted]

    # Deterministic job id: a redelivered backfill command resumes the same job.
    job_seed = f"{action_config.study_id}:{sorted(i.id for i in individuals)}:{action_config.start}"
    job_id = "job-" + hashlib.sha256(job_seed.encode()).hexdigest()[:12]
    job = BackfillJob(integration_id, job_id)

    ranges = {i.id: (_resolve_start(action_config.start, i), await _resolve_end(integration_id, i, now)) for i in individuals}
    # Only individuals with a non-empty range are worth queuing.
    queued = [i for i in individuals if ranges[i.id][0] < ranges[i.id][1]]
    range_repr = f"[{action_config.start} .. {now.isoformat()})"
    await job.seed([i.id for i in queued], total=len(queued), range_repr=range_repr)

    logger.info(f"Backfill {job_id} for study {action_config.study_id}: {len(queued)} individuals, range {range_repr}")

    k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
    by_id = {i.id: i for i in queued}
    dispatched = 0
    while dispatched < k:
        next_id = await job.next_individual()
        if next_id is None:
            break
        ind = by_id[next_id]
        start_dt, end_dt = ranges[next_id]
        await job.incr_in_flight()
        await trigger_action(
            integration_id=integration_id,
            action_id=BACKFILL_ACTION_ID,
            config=BackfillEventsForIndividualConfig(
                study_id=action_config.study_id, individual=ind,
                job_id=job_id, start=start_dt, end=end_dt,
            ),
        )
        dispatched += 1

    return {"job_id": job_id, "individuals": len(queued), "dispatched": dispatched}
