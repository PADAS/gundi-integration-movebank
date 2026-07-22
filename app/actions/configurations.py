from datetime import datetime
from typing import List, Literal, Optional, Union

from pydantic import Field, SecretStr

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


class BackfillConfig(GenericActionConfiguration, ExecutableActionMixin):
    """Operator-triggered study backfill. Executable, NOT scheduled — it must
    not subclass PullActionConfiguration."""
    study_id: str = Field(..., title="Movebank Study ID")
    individual_ids: Optional[List[str]] = Field(
        None,
        title="Individual IDs",
        description="Leave empty to backfill the whole study, or list specific individual IDs.",
    )
    start: Union[datetime, Literal["all"]] = Field(
        "all",
        title="Start",
        description="Earliest datetime to backfill from, or 'all' to fetch from each individual's earliest record.",
    )
    backfill_max_concurrency: Optional[int] = Field(
        None,
        title="Max Concurrency",
        description="Individuals processed in parallel. Defaults to the service's BACKFILL_MAX_CONCURRENCY.",
        ge=1,
    )


class BackfillEventsForIndividualConfig(InternalActionConfiguration):
    study_id: str
    individual: Individual
    job_id: str
    start: datetime
    end: datetime
