"""The `movebank` integration type in Gundi is shared with other services.

This runner registers `auth` (and its own pull, backfill and reference
actions). Two other services read configurations on the same type:

- gundi-movebank-dispatcher reads the `auth` configuration for its Movebank
  credentials, through gundi-core's MBAuthActionConfig, and owns
  `push_observations`.
- The portal backend (cdip) owns `permissions`.

Nothing else ties these together, so these tests fail when a change here
would break one of the other services. See "Shared integration type" in the
README before changing them.
"""
from gundi_core.schemas.v2 import MBAuthActionConfig, MovebankActions

from app.actions.configurations import AuthenticateConfig
from app.actions.core import AuthActionConfiguration, discover_actions

# Action names on the shared type that belong to another service.
OWNED_ELSEWHERE = {
    MovebankActions.PERMISSIONS.value,        # cdip (portal backend)
    MovebankActions.PUSH_OBSERVATIONS.value,  # gundi-movebank-dispatcher
}


def _registered_actions():
    return discover_actions("app.actions.handlers", "action_")


def test_the_auth_action_keeps_the_name_the_dispatcher_looks_up():
    handlers = _registered_actions()
    name = MovebankActions.AUTHENTICATE.value
    assert name in handlers
    assert handlers[name][1] is AuthenticateConfig
    assert issubclass(AuthenticateConfig, AuthActionConfiguration)


def test_the_auth_form_asks_for_exactly_what_the_dispatcher_requires():
    ours, theirs = AuthenticateConfig.schema(), MBAuthActionConfig.schema()
    # A field the dispatcher requires but this form does not collect leaves
    # every saved configuration unreadable to it.
    assert set(theirs["required"]) <= set(ours["properties"])
    # A field this form requires under another name would do the same.
    assert set(ours["required"]) == set(theirs["required"])


def test_a_saved_auth_configuration_parses_as_the_dispatcher_reads_it():
    # What the portal stores for a form filled in from this runner's schema.
    saved = {"username": "mb-account", "password": "s3cret"}
    assert AuthenticateConfig.parse_obj(saved).password.get_secret_value() == "s3cret"
    theirs = MBAuthActionConfig.parse_obj(saved)
    assert (theirs.username, theirs.password) == ("mb-account", "s3cret")


def test_this_runner_registers_no_action_owned_by_another_service():
    # Registering one would overwrite the owner's schema on the shared type.
    assert not OWNED_ELSEWHERE & set(_registered_actions())
