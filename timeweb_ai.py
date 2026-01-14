from __future__ import annotations

import base64
import json
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


def _extract_text_from_chat_completions(data: dict[str, Any]) -> str:
    """
    Поддержка вариаций OpenAI-style ответа:
    - choices[0].message.content: str
    - choices[0].message.content: [{"type":"text","text":"..."}]
    - choices[0].text (редко/legacy)
    """
    choices = data.get("choices") or []
    if not choices:
        return ""

    c0 = choices[0] or {}

    # legacy
    if isinstance(c0.get("text"), str):
        return c0["text"]

    msg = c0.get("message") or {}
    content = msg.get("content")

    if isinstance(content, str):
        return content

    # content as list of parts
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
                continue
            if isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                    continue
                # иногда встречается {"type":"text","content":"..."}
                if isinstance(p.get("content"), str):
                    parts.append(p["content"])
                    continue
        return "\n".join(parts)

    # tool calls (иногда content пустой, а ответ лежит в arguments)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            if not isinstance(args, str) or not args.strip():
                continue
            # Попробуем распарсить JSON arguments и достать распространённые поля.
            try:
                obj = json.loads(args)
                if isinstance(obj, dict):
                    for k in ("caption", "text", "answer", "result", "output"):
                        v = obj.get(k)
                        if isinstance(v, str) and v.strip():
                            return v
            except Exception:
                pass
            return args

    return ""


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

    def _build_messages(include_image: bool, system_override: str | None = None) -> list[dict[str, Any]]:
        system_text = system_override or (
            "Ты остроумный русскоязычный автор подписей к картинкам. "
            "Твоя задача — смешно, но без токсичности, оскорблений и политики. "
            "Никаких лишних слов — только готовая подпись."
        )

        user_text = (
            "Придумай одну короткую смешную подпись (до 120 символов) к картинке. "
            "Верни только подпись, без кавычек, без хэштегов, без объяснений."
        )
        if original_caption:
            user_text += f"\nКонтекст/подпись автора поста: {original_caption}"

        if include_image:
            user_content: Any = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
        else:
            # Фолбэк: если vision не поддерживается — пусть хотя бы придумает подпись по контексту.
            user_content = user_text + "\nЕсли ты не видишь изображение, всё равно верни смешную подпись."

        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ]

    payload: dict[str, Any] = {
        "model": settings.timeweb_ai_model,
        "messages": _build_messages(include_image=bool(settings.timeweb_ai_send_image)),
        # Некоторые современные модели/провайдеры (в т.ч. через OpenAI-совместимые прокси)
        # используют max_completion_tokens вместо max_tokens.
        "max_completion_tokens": 80,
    }
    # Некоторые модели запрещают менять temperature (разрешено только значение по умолчанию).
    # Поэтому по умолчанию мы temperature НЕ отправляем.
    if settings.timeweb_ai_temperature is not None:
        payload["temperature"] = settings.timeweb_ai_temperature

    base = settings.timeweb_ai_base_url.rstrip("/")
    path = settings.timeweb_ai_chat_path.strip()
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    headers = {
        "Authorization": f"Bearer {settings.timeweb_ai_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
        r = await client.post(url, headers=headers, json=payload)

        # Если модель ругается на temperature — пробуем один раз без него.
        if r.status_code == 400 and "temperature" in r.text and "Only the default" in r.text:
            payload.pop("temperature", None)
            r = await client.post(url, headers=headers, json=payload)

        if r.status_code >= 400:
            hint = ""
            if r.status_code == 404:
                hint = f" (проверьте TIMEWEB_AI_BASE_URL/TIMEWEB_AI_CHAT_PATH; текущий URL: {url})"
            raise TimewebAIError(f"Timeweb AI HTTP {r.status_code}: {r.text[:500]}{hint}")

        data = r.json()

    text = _extract_text_from_chat_completions(data)

    text = (text or "").strip()
    # Чуть-чуть «очистки», чтобы бот не прислал пустое или многословное.
    text = text.replace("\n", " ").strip()

    # Если вдруг пришёл пустой текст (иногда бывает у прокси/агентов) — делаем ретраи:
    # 1) более жёсткая инструкция
    # 2) фолбэк без картинки (если vision не поддерживается)
    if not text:
        payload_retry = dict(payload)
        payload_retry["messages"] = _build_messages(
            include_image=bool(settings.timeweb_ai_send_image),
            system_override=(
                "Ты пишешь подписи к картинкам. Ответ НЕ может быть пустым. "
                "Верни одну короткую подпись, только текст."
            ),
        )

        async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
            r2 = await client.post(url, headers=headers, json=payload_retry)
            if r2.status_code >= 400:
                raise TimewebAIError(f"Timeweb AI HTTP {r2.status_code}: {r2.text[:500]}")
            data2 = r2.json()
        text = _extract_text_from_chat_completions(data2)
        text = (text or "").strip().replace("\n", " ").strip()

        if not text and settings.timeweb_ai_send_image:
            payload_retry2 = dict(payload)
            payload_retry2["messages"] = _build_messages(include_image=False)
            async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
                r3 = await client.post(url, headers=headers, json=payload_retry2)
                if r3.status_code >= 400:
                    raise TimewebAIError(f"Timeweb AI HTTP {r3.status_code}: {r3.text[:500]}")
                data3 = r3.json()
            text = _extract_text_from_chat_completions(data3)
            text = (text or "").strip().replace("\n", " ").strip()

        if not text:
            response_id = (
                data.get("response_id")
                or data.get("id")
                or data.get("request_id")
                or data.get("trace_id")
                or data.get("x_request_id")
            )
            suffix = f" (response_id={response_id})" if response_id else ""
            raise TimewebAIError(f"AI вернул пустую подпись{suffix}")

    return text[:400]

