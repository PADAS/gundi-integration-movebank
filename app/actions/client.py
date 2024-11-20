from movebank_client import MovebankClient
from movebank_client.errors import MBClientError

from app.actions.configurations import AuthenticateConfig
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action


def get_auth_config(integration):
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
