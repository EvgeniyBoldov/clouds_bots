from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GATEWAY_", extra="ignore")
    database_url: str = "postgresql+asyncpg://gateway:gateway@postgres:5432/gateway"
    admin_token: str = "change-me"
    admin_cookie_secure: bool = False
    notification_token: str = ""
    bot_accounts_json: str = "[]"
    worker_poll_seconds: float = 1.0
    chat_reconcile_seconds: float = Field(default=300.0, gt=0)
    handler_configs_json: str = "{}"
    kopatych_user_tokens: dict[str, SecretStr] = {}
    kopatych_connector_id: str = "express"
    kopatych_timezone: str = "Europe/Moscow"
    kopatych_delivery_mode: Literal["push", "poll"] = "poll"
    log_level: str = "INFO"


settings = Settings()
