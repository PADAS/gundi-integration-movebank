import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Union

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient
from movebank_client.errors import MBClientError, MBForbiddenError

from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action

logger = logging.getLogger(__name__)


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
