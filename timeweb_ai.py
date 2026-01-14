from __future__ import annotations

import base64
from typing import Any

import httpx

from config import get_settings


class TimewebAIError(RuntimeError):
    pass


def _guess_mime(image_bytes: bytes) -> str:
    # Очень простой guess — достаточно для большинства фото из Telegram.
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


async def generate_funny_caption(image_bytes: bytes, original_caption: str | None) -> str:
    """
    Пытаемся получить 1 короткую смешную подпись к картинке через AI-агента Timeweb.
    Реализация рассчитана на OpenAI-совместимый endpoint /v1/chat/completions.
    """
    mime = _guess_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    # Важно: просим вернуть ТОЛЬКО подпись, без кавычек и пояснений.
    # original_caption может помочь, если в посте уже есть контекст/тема.
    user_text = (
        "Придумай одну короткую смешную подпись (до 120 символов) к картинке. "
        "Верни только подпись, без кавычек, без хэштегов, без объяснений."
    )
    if original_caption:
        user_text += f"\nКонтекст/подпись автора поста: {original_caption}"

    settings = get_settings()

    payload: dict[str, Any] = {
        "model": settings.timeweb_ai_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты остроумный русскоязычный автор подписей к картинкам. "
                    "Твоя задача — смешно, но без токсичности, оскорблений и политики. "
                    "Никаких лишних слов — только готовая подпись."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.9,
        "max_tokens": 80,
    }

    url = settings.timeweb_ai_base_url.rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.timeweb_ai_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 400:
            raise TimewebAIError(f"Timeweb AI HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()

    # OpenAI-style: choices[0].message.content
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise TimewebAIError(f"Неожиданный формат ответа Timeweb AI: {data}") from e

    text = (text or "").strip()
    # Чуть-чуть «очистки», чтобы бот не прислал пустое или многословное.
    text = text.replace("\n", " ").strip()
    if not text:
        raise TimewebAIError("AI вернул пустую подпись")
    return text[:400]

