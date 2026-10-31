"""Application settings via pydantic-settings, reading from environment and .env."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    model_api_key: str = ""
    muse_chat_model: str = "muse-spark-1.3"
    app_env: str = "dev"


settings = Settings()
