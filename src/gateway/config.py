from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GATEWAY_", extra="ignore")
    database_url: str = "postgresql+asyncpg://gateway:gateway@postgres:5432/gateway"
    admin_token: str = "change-me"
    admin_cookie_secure: bool = False
    notification_token: str = ""
    bot_accounts_json: str = "[]"
    worker_poll_seconds: float = 1.0
    handler_configs_json: str = "{}"
    log_level: str = "INFO"


settings = Settings()
