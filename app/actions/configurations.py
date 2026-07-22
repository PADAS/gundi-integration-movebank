from datetime import datetime
from typing import List, Optional

from dateutil.parser import parse as parse_date
from pydantic import Field, SecretStr, validator

from app.actions import AuthActionConfiguration, ExecutableActionMixin, PullActionConfiguration
from app.actions.client import Individual
from app.actions.core import GenericActionConfiguration, InternalActionConfiguration
from app.services.utils import GlobalUISchemaOptions


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    username: str
    password: SecretStr = Field(..., format="password")
    ui_global_options = GlobalUISchemaOptions(
        order=["username", "password"],
    )


class PullObservationsConfig(PullActionConfiguration):
    study_id: str = Field(
        ...,
        title="Movebank Study ID",
        description="ID of the Movebank study to pull observations from.",
    )
    maximum_lookback_hours: int = Field(
        24,
        title="Maximum Lookback (hours)",
        description=(
            "How far back to fetch events for an individual that has no saved state yet. "
            "Override this on a manual run to backfill historical data."
        ),
    )


class PullEventsForIndividualConfig(InternalActionConfiguration):
    """Config for the internal sub-action that pulls events for one individual."""
    study_id: str
    individual: Individual
    maximum_lookback_hours: int = 24


# Executable, NOT scheduled — must not subclass PullActionConfiguration
# (that would register it for type-wide scheduling).
class BackfillConfig(GenericActionConfiguration, ExecutableActionMixin):
    """Back-fill historical Movebank data for a study."""
    study_id: str = Field(..., title="Movebank Study ID")
    individual_ids: Optional[List[str]] = Field(
        None,
        title="Individual IDs",
        description="Leave empty to backfill the whole study, or list specific individual IDs.",
    )
    start: str = Field(
        "all",
        title="Start",
        description="A start date (e.g. 2024-01-01) or 'all' to fetch from each individual's earliest record.",
    )
    backfill_max_concurrency: Optional[int] = Field(
        None,
        title="Max Concurrency",
        description="Individuals processed in parallel. Defaults to the service's BACKFILL_MAX_CONCURRENCY.",
        ge=1,
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["study_id", "individual_ids", "start", "backfill_max_concurrency"],
    )

    @validator("start")
    def _validate_start(cls, value):
        # A real date or the literal "all" — never let a typo silently become
        # full-history "all".
        if value.strip().lower() == "all":
            return "all"
        try:
            parse_date(value)
        except (ValueError, OverflowError, TypeError):
            raise ValueError("start must be a date (e.g. 2024-01-01) or 'all'")
        return value


class BackfillEventsForIndividualConfig(InternalActionConfiguration):
    study_id: str
    individual: Individual
    job_id: str
    start: datetime
    end: datetime
