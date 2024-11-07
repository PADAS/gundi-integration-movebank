from datetime import datetime, timezone
from pydantic import Field

from app.actions import AuthActionConfiguration, PullActionConfiguration


class AuthenticateConfig(AuthActionConfiguration):
    username: str
    password: str


class FetchIndividualEventsConfig(PullActionConfiguration):
    start_time: datetime
    study_id: int
    individual_id: int


class FetchStudyIndividualsConfig(PullActionConfiguration):
    study_id: str = Field(
        title='Movebank Study IDs',
        description='ID of the desired Movebank Study.',
    )
    start_time: datetime = Field(
        title='Start Datetime',
        description='Datetime events are going to be fetched from.',
        default=datetime.now(tz=timezone.utc)
    )
