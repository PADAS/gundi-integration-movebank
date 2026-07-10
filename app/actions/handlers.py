import logging

import app.actions.client as client
from app.actions.client import generate_individuals
from app.actions.configurations import AuthenticateConfig, PullObservationsConfig, PullEventsForIndividualConfig
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.state import IntegrationStateManager


logger = logging.getLogger(__name__)


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
