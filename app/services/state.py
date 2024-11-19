import json
import stamina
import httpx
import redis.asyncio as redis
from app import settings
from gundi_core.schemas.v2 import Integration
from gundi_client_v2 import GundiClient


_portal = GundiClient()


class IntegrationStateManager:

    def __init__(self, **kwargs):
        host = kwargs.get("host", settings.REDIS_HOST)
        port = kwargs.get("port", settings.REDIS_PORT)
        db = kwargs.get("db", settings.REDIS_STATE_DB)
        self.db_client = redis.Redis(host=host, port=port, db=db)

    async def get_state(self, integration_id: str, action_id: str, source_id: str = "no-source") -> dict:
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                json_value = await self.db_client.get(f"integration_state.{integration_id}.{action_id}.{source_id}")
        value = json.loads(json_value) if json_value else {}
        return value

    async def set_state(self, integration_id: str, action_id: str, state: dict, source_id: str = "no-source", expire: int = None):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.set(
                    f"integration_state.{integration_id}.{action_id}.{source_id}",
                    json.dumps(state, default=str),
                    ex=expire
                )

    async def delete_state(self, integration_id: str, action_id: str, source_id: str = "no-source"):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.delete(
                    f"integration_state.{integration_id}.{action_id}.{source_id}"
                )

    def __str__(self):
        return f"IntegrationStateManager(host={self.db_client.host}, port={self.db_client.port}, db={self.db_client.db})"

    def __repr__(self):
        return self.__str__()


class IntegrationConfigurationManager:

    def __init__(self, **kwargs):
        host = kwargs.get("host", settings.REDIS_HOST)
        port = kwargs.get("port", settings.REDIS_PORT)
        db = kwargs.get("db", settings.REDIS_STATE_DB)
        self.db_client = redis.Redis(host=host, port=port, db=db)

    async def get_integration_config(self, integration_id: str) -> Integration:
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                json_value = await self.db_client.get(f"integration_config.{integration_id}")

        value = json.loads(json_value) if json_value else {}

        if not value:
            async for attempt in stamina.retry_context(
                    on=httpx.HTTPError,
                    wait_initial=1.0,
                    wait_jitter=5.0,
                    wait_max=32.0
            ):
                with attempt:
                    # ToDo: Store configs and update it on changes (event-driven architecture)
                    integration = await _portal.get_integration_details(integration_id=integration_id)
                    # Save the integration data in cache (will last 1 hr)
                    await self.set_integration_config(
                        str(integration_id),
                        json.loads(integration.json()),
                        600
                    )
                    return integration

        return Integration.parse_obj(value)

    async def set_integration_config(self, integration_id: str, config: dict, expire: int = None):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.set(
                    f"integration_config.{integration_id}",
                    json.dumps(config, default=str),
                    ex=expire
                )

    async def delete_integration_config(self, integration_id: str):
        for attempt in stamina.retry_context(on=redis.RedisError, attempts=5, wait_initial=1.0, wait_max=30, wait_jitter=3.0):
            with attempt:
                await self.db_client.delete(
                    f"integration_config.{integration_id}"
                )

    def __str__(self):
        return f"IntegrationConfigurationManager(host={self.db_client.host}, port={self.db_client.port}, db={self.db_client.db})"

    def __repr__(self):
        return self.__str__()
