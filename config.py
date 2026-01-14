from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic import field_validator
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Конфигурация через переменные окружения.
    Для локального запуска можно создать .env (не коммитить).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        str_strip_whitespace=True,
    )

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
    timeweb_ai_chat_path: str = Field("/v1/chat/completions", alias="TIMEWEB_AI_CHAT_PATH")
    timeweb_ai_api_key: str = Field(..., alias="TIMEWEB_AI_API_KEY")
    timeweb_ai_model: str = Field(..., alias="TIMEWEB_AI_MODEL")
    timeweb_ai_timeout_s: float = Field(30.0, alias="TIMEWEB_AI_TIMEOUT_S")
    timeweb_ai_temperature: float | None = Field(None, alias="TIMEWEB_AI_TEMPERATURE")
    timeweb_ai_send_image: bool = Field(True, alias="TIMEWEB_AI_SEND_IMAGE")

    # Поведение генерации
    caption_language: str = Field("ru", alias="CAPTION_LANGUAGE")

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _normalize_bot_token(cls, v: str) -> str:
        # Частая проблема в панелях деплоя: пробелы/переносы/кавычки.
        if v is None:
            return v
        if not isinstance(v, str):
            v = str(v)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1].strip()
        return v

    @property
    def telegram_webhook_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}/webhook/{self.webhook_path_secret}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_settings_or_error() -> tuple[Settings | None, str | None]:
    """
    Для healthcheck/стартов: вернуть (settings, None) либо (None, str_error).
    """
    try:
        return get_settings(), None
    except ValidationError as e:
        return None, str(e)

