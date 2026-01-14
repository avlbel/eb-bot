from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Конфигурация через переменные окружения.
    Для локального запуска можно создать .env (не коммитить).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    public_base_url: str = Field(..., alias="PUBLIC_BASE_URL")  # например: https://my-app.timeweb.cloud
    webhook_path_secret: str = Field(..., alias="TELEGRAM_WEBHOOK_PATH_SECRET")  # часть URL пути
    telegram_webhook_secret_token: str = Field(
        ..., alias="TELEGRAM_WEBHOOK_SECRET_TOKEN"
    )  # проверяется по заголовку Telegram
    allowed_channel_id: int | None = Field(None, alias="TELEGRAM_ALLOWED_CHANNEL_ID")

    # Timeweb AI / OpenAI-совместимый API
    timeweb_ai_base_url: str = Field("https://api.timeweb.cloud", alias="TIMEWEB_AI_BASE_URL")
    timeweb_ai_api_key: str = Field(..., alias="TIMEWEB_AI_API_KEY")
    timeweb_ai_model: str = Field(..., alias="TIMEWEB_AI_MODEL")
    timeweb_ai_timeout_s: float = Field(30.0, alias="TIMEWEB_AI_TIMEOUT_S")

    # Поведение генерации
    caption_language: str = Field("ru", alias="CAPTION_LANGUAGE")

    @property
    def telegram_webhook_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}/webhook/{self.webhook_path_secret}"


settings = Settings()

