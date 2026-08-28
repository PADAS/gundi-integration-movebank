import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Union

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient as _MovebankClient
# Not used in this module directly — re-exported for consumers that access them
# through this namespace (e.g. `client.MBForbiddenError` in app/actions/handlers.py).
from movebank_client.errors import MBClientError, MBForbiddenError

from app.services.errors import ConfigurationNotFound
from app.services.study_attributes_cache import (
    get_cached_study_attributes,
    set_cached_study_attributes,
)
from app.services.utils import find_config_for_action

logger = logging.getLogger(__name__)

DEFAULT_MOVEBANK_BASE_URL = "https://www.movebank.org"


class MovebankClient(_MovebankClient):
    """Defaults base_url to the public Movebank server when the integration
    record leaves it unset (None or empty string), and backs the study-attribute
    lookup with a shared Redis cache."""

    def __init__(self, **kwargs):
        if not kwargs.get("base_url"):
            kwargs["base_url"] = DEFAULT_MOVEBANK_BASE_URL
        super().__init__(**kwargs)

    async def get_study_attributes(self, study_id: str = None, sensor_type_id: str = None) -> list:
        """Three-layer lookup: this client instance, then Redis, then Movebank.

        movebank-client caches study attributes on the client instance, which is
        never reused here — `pull_events_for_individual` builds a fresh client
        per individual, so every individual in a study re-fetched the same list
        on every tick. That made study-attribute requests roughly half of all
        Movebank traffic and was the main driver of the 429s. Redis moves the
        cache out to a scope the whole fleet shares: one fetch per study per TTL,
        across individuals, integrations, instances and ticks.
        """
        instance_key = (study_id, sensor_type_id)
        if instance_key in self.study_attributes_cache:
            return self.study_attributes_cache[instance_key]

        cached = await get_cached_study_attributes(self.base_url, study_id, sensor_type_id)
        if cached is not None:
            # Warm the instance cache so a multi-window loop skips Redis too.
            self.study_attributes_cache[instance_key] = cached
            return cached

        attributes = await super().get_study_attributes(
            study_id=study_id, sensor_type_id=sensor_type_id
        )
        if attributes is not None:
            # Only a real answer is cached. A failed fetch returns None, and
            # caching that would pin the study to attributes='all' for the TTL.
            # Populate both layers here rather than leaning on the base class's
            # own instance-cache write, so this method's caching is self-contained.
            self.study_attributes_cache[instance_key] = attributes
            await set_cached_study_attributes(
                self.base_url, study_id, sensor_type_id, attributes
            )
        return attributes


def get_auth_config(integration):
    from app.actions.configurations import AuthenticateConfig

    # Look for the login credentials, needed for any action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)


class Individual(pydantic.BaseModel):
    id: str
    local_identifier: str
    nick_name: str
    ring_id: str
    sex: str
    taxon_canonical_name: str

    # Tolerate timestamps with empty-string. Let validator coerce to datetime.
    timestamp_start: Optional[Union[str, datetime]]
    timestamp_end: Optional[Union[datetime, str]]
    number_of_events: Optional[int] = 0
    number_of_deployments: Optional[int] = 0
    sensor_type_ids: str
    taxon_detail: str

    @pydantic.validator('timestamp_start', 'timestamp_end')
    def clean_timestamp(cls, val):
        if val is None:
            return None
        if isinstance(val, str):
            try:
                val = parse_date(val)
            except Exception:
                return None
        return val.astimezone(timezone.utc) if val.tzinfo else val.replace(tzinfo=timezone.utc)


def generate_individuals(items):
    for item in items:
        try:
            val = Individual.parse_obj(item)
        except Exception:
            logger.exception('Failed parsing Individual %s', item)
        else:
            yield val


class SensorState(pydantic.BaseModel):
    """Cursor for a single sensor type of one individual."""
    highest_event_id: Optional[int] = 0
    latest_timestamp: Optional[datetime] = None


class IndividualState(pydantic.BaseModel):
    individual_id: str
    study_id: str
    local_identifier: Optional[str] = None
    # Per-sensor-type cursors (key = sensor_type_id as string, because JSON keys are strings)
    sensor_states: Dict[str, SensorState] = pydantic.Field(default_factory=dict)
    # Oldest timestamp this individual's steady-state coverage begins at. Set once
    # (pull first run, or backfill seed/finalize); never advanced forward. Backfill
    # fills [start, coverage_start) for an already-cursored individual.
    coverage_start: Optional[datetime] = None

    def get_sensor_state(self, sensor_type_id: int) -> SensorState:
        key = str(sensor_type_id)
        if key not in self.sensor_states:
            self.sensor_states[key] = SensorState()
        return self.sensor_states[key]

    def update_sensor_state(self, sensor_type_id: int, latest_timestamp: datetime, highest_event_id: int):
        self.sensor_states[str(sensor_type_id)] = SensorState(
            latest_timestamp=latest_timestamp,
            highest_event_id=highest_event_id
        )
