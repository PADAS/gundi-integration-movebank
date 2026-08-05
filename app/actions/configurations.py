from datetime import datetime
from typing import List, Optional

from dateutil.parser import parse as parse_date
from pydantic import Field, SecretStr, root_validator, validator

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
    restart: bool = Field(
        False,
        title="Restart",
        description="Clear any existing job for these parameters and start over from the "
                    "beginning. Use to recover a stuck backfill. Only use on a stuck job: "
                    "clearing a healthy in-flight job can double-dispatch its remaining work.",
    )
    cancel: bool = Field(
        False,
        title="Cancel",
        description="Stop the active job for these parameters: nothing new is dispatched and "
                    "in-flight individuals stop at their next step. Submit with the SAME Study "
                    "ID, Individual IDs and Start as the running job. Data already sent is "
                    "kept; re-running the same backfill resumes where cancel stopped it.",
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["study_id", "individual_ids", "start", "backfill_max_concurrency", "restart", "cancel"],
    )

    @validator("individual_ids")
    def _canonical_individual_ids(cls, value):
        # Canonical form: deduped and sorted. The job_id is hashed from these,
        # and execution filters with set(individual_ids) — so two spellings that
        # run identically (e.g. a pasted list with a repeat) must hash
        # identically, or a later cancel/restart won't target the running job.
        if not value:
            return value
        return sorted(set(value))

    @root_validator
    def _cancel_xor_restart(cls, values):
        if values.get("cancel") and values.get("restart"):
            raise ValueError("cancel and restart are mutually exclusive — pick one")
        return values

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
