import datetime
import httpx
import logging
import stamina

import app.services.gundi as gundi_tools
import app.settings.integration as settings
import app.actions.client as client

from app.actions.configurations import AuthenticateConfig, FetchStudyIndividualsConfig, FetchIndividualEventsConfig
from app.services.activity_logger import activity_logger
from app.services.state import IntegrationStateManager


logger = logging.getLogger(__name__)


state_manager = IntegrationStateManager()


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
    except Exception as e:
        logger.exception(f"Auth unsuccessful for integration {integration}. Exception: {e}")
        return {"valid_credentials": False}

    if token:
        logger.info(f"Auth successful for integration '{integration.name}'. Token: '{token['api-token']}'")
        return {"valid_credentials": True}
    else:
        logger.error(f"Auth unsuccessful for integration {integration}.")
        return {"valid_credentials": False}
