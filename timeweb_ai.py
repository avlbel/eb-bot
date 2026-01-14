from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from config import get_settings


class TimewebAIError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


def _guess_mime(image_bytes: bytes) -> str:
    # Очень простой guess — достаточно для большинства фото из Telegram.
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


def _extract_text_from_responses_api(data: dict[str, Any]) -> str:
    """
    OpenAI Responses API style:
    - output_text: "..."
    - output: [{type:"message", content:[{type:"output_text", text:"..."}]}]
    """
    ot = data.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot

    out = data.get("output")
    if not isinstance(out, list):
        return ""

    parts: list[str] = []
    for item in out:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "\n".join(parts)


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
        # Некоторые прокси кладут отказ отдельно, а content оставляют пустым.
        if not content.strip() and isinstance(msg.get("refusal"), str) and msg["refusal"].strip():
            return msg["refusal"]
        # Иногда текст попадает в annotations.
        if not content.strip():
            ann = msg.get("annotations")
            if isinstance(ann, list):
                for a in ann:
                    if not isinstance(a, dict):
                        continue
                    for k in ("text", "content", "annotation", "value", "message"):
                        v = a.get(k)
                        if isinstance(v, str) and v.strip():
                            return v
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


def _finish_reason_from_chat_completions(data: dict[str, Any]) -> str | None:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0]
        if isinstance(c0, dict) and isinstance(c0.get("finish_reason"), str):
            return c0["finish_reason"]
    return None


def _response_meta(data: dict[str, Any]) -> dict[str, Any]:
    """
    Безопасная диагностика: только метаданные, без промптов/картинок.
    """
    meta: dict[str, Any] = {
        "id": data.get("id") or data.get("response_id"),
        "object": data.get("object"),
        "model": data.get("model"),
        "keys": sorted(list(data.keys()))[:50],
    }
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        meta["finish_reason"] = c0.get("finish_reason")
        msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        meta["message_keys"] = sorted(list(msg.keys()))[:50] if isinstance(msg, dict) else None
        # content может быть None/""/list — фиксируем тип
        meta["content_type"] = type(msg.get("content")).__name__ if isinstance(msg, dict) else None
        if isinstance(msg, dict):
            c = msg.get("content")
            r = msg.get("refusal")
            meta["content_len"] = len(c) if isinstance(c, str) else None
            meta["refusal_len"] = len(r) if isinstance(r, str) else None
            ann = msg.get("annotations")
            meta["annotations_len"] = len(ann) if isinstance(ann, list) else None
    return meta


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
        "max_completion_tokens": int(settings.timeweb_ai_max_completion_tokens),
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

    # Сначала пробуем OpenAI chat.completions, затем Responses API (некоторые прокси так отвечают).
    text = _extract_text_from_chat_completions(data)
    if not text:
        text = _extract_text_from_responses_api(data)

    text = (text or "").strip()
    # Чуть-чуть «очистки», чтобы бот не прислал пустое или многословное.
    text = text.replace("\n", " ").strip()

    # Если вдруг пришёл пустой текст (иногда бывает у прокси/агентов) — делаем ретраи:
    # 1) более жёсткая инструкция
    # 2) фолбэк без картинки (если vision не поддерживается)
    if not text:
        # Если модель отрезала ответ по длине ещё до появления текста — попробуем ретрай с большим лимитом.
        finish_reason = _finish_reason_from_chat_completions(data)
        if finish_reason == "length":
            payload_more = dict(payload)
            payload_more["max_completion_tokens"] = max(int(payload.get("max_completion_tokens", 0) or 0), 2048)
            async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
                r_more = await client.post(url, headers=headers, json=payload_more)
                if r_more.status_code >= 400:
                    raise TimewebAIError(f"Timeweb AI HTTP {r_more.status_code}: {r_more.text[:500]}")
                data_more = r_more.json()
            text = (_extract_text_from_chat_completions(data_more) or _extract_text_from_responses_api(data_more)).strip()
            text = text.replace("\n", " ").strip()

        if text:
            return text[:400]

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
        text = _extract_text_from_chat_completions(data2) or _extract_text_from_responses_api(data2)
        text = (text or "").strip().replace("\n", " ").strip()

        if not text and settings.timeweb_ai_send_image:
            payload_retry2 = dict(payload)
            payload_retry2["messages"] = _build_messages(include_image=False)
            async with httpx.AsyncClient(timeout=settings.timeweb_ai_timeout_s) as client:
                r3 = await client.post(url, headers=headers, json=payload_retry2)
                if r3.status_code >= 400:
                    raise TimewebAIError(f"Timeweb AI HTTP {r3.status_code}: {r3.text[:500]}")
                data3 = r3.json()
            text = _extract_text_from_chat_completions(data3) or _extract_text_from_responses_api(data3)
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
            # Пишем в лог безопасные метаданные ответа, чтобы понять формат/причину пустоты.
            try:
                logger.error("Timeweb AI empty response meta: %s", _response_meta(data))
            except Exception:
                pass
            raise TimewebAIError(f"AI вернул пустую подпись{suffix}")

    return text[:400]

