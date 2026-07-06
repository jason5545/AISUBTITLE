#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import select
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone


PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
os.environ["PATH"] = f"{PATH_PREFIX}:{os.environ.get('PATH', '')}"

WORKDIR = (
    os.environ.get("AISUBTITLE_TRANSLATE_WORKDIR")
    or os.environ.get("OPENROUTER_TRANSLATE_WORKDIR")
    or os.environ.get("CLINE_TRANSLATE_WORKDIR")
    or os.environ.get("CODEX_TRANSLATE_WORKDIR")
    or os.getcwd()
)
LOG_FILE = (
    os.environ.get("OPENROUTER_TRANSLATE_LOG")
    or os.environ.get("CLINE_TRANSLATE_LOG")
    or os.environ.get("CODEX_TRANSLATE_LOG")
    or os.path.join(WORKDIR, "codex-translate.log")
)
SECRET_FILE = os.environ.get("AISUBTITLE_SECRET_FILE", os.path.join(WORKDIR, ".secret"))
USAGE_STATE_FILE = os.environ.get("AISUBTITLE_USAGE_STATE_FILE", os.path.join(WORKDIR, ".aisubtitle-usage.json"))
PROVIDER = os.environ.get("AISUBTITLE_TRANSLATE_PROVIDER", "openrouter").strip().lower()
if PROVIDER not in {"openrouter", "cline"}:
    PROVIDER = "openrouter"

if PROVIDER == "openrouter":
    API_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
    MODEL = (
        os.environ.get("AISUBTITLE_TRANSLATE_MODEL")
        or os.environ.get("OPENROUTER_TRANSLATE_MODEL")
        or "google/gemini-3.1-flash-lite"
    )
    REASONING = os.environ.get("OPENROUTER_REASONING_EFFORT", "none")
else:
    API_URL = os.environ.get("CLINE_API_URL", "https://api.cline.bot/api/v1/chat/completions")
    MODEL = (
        os.environ.get("AISUBTITLE_TRANSLATE_MODEL")
        or os.environ.get("CLINE_TRANSLATE_MODEL")
        or os.environ.get("CODEX_TRANSLATE_MODEL")
        or "cline-pass/deepseek-v4-flash"
    )
    REASONING = os.environ.get("CLINE_REASONING_EFFORT", "none")

MAX_TOKENS = int(os.environ.get("AISUBTITLE_TRANSLATE_MAX_TOKENS", "240"))
TIMEOUT_SECONDS = float(os.environ.get("AISUBTITLE_TRANSLATE_TIMEOUT_SECONDS", "12"))
DRAIN_SECONDS = float(os.environ.get("CODEX_TRANSLATE_DRAIN_SECONDS", "0.02"))
CLINE_WEEKLY_LIMIT_HOURS = float(os.environ.get("CLINE_USAGE_WEEKLY_LIMIT_HOURS", "5"))
CLINE_MONTHLY_LIMIT_HOURS = float(os.environ.get("CLINE_USAGE_MONTHLY_LIMIT_HOURS", str(CLINE_WEEKLY_LIMIT_HOURS * 4)))
SESSION_HISTORY_LIMIT = int(os.environ.get("AISUBTITLE_TRANSLATE_SESSION_HISTORY_LIMIT", "6"))

openrouter_session_cost = 0.0
translation_sessions: dict[str, list[dict[str, str]]] = {}


class LineReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = b""
        self.eof = False

    def read_line(self) -> str | None:
        while b"\n" not in self.buffer and not self.eof:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.eof = True
                break
            self.buffer += chunk

        if b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            return raw.decode("utf-8", errors="replace").rstrip("\r")

        if self.buffer:
            raw = self.buffer
            self.buffer = b""
            return raw.decode("utf-8", errors="replace").rstrip("\r")

        return None

    def drain_to_latest(self, line: str, seconds: float) -> tuple[str, int]:
        dropped = 0
        deadline = time.monotonic() + max(0.0, seconds)

        while True:
            while b"\n" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\n", 1)
                newer = raw.decode("utf-8", errors="replace").rstrip("\r")
                if newer:
                    line = newer
                    dropped += 1

            timeout = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([self.fd], [], [], timeout)
            if not readable:
                break

            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.eof = True
                break

            self.buffer += chunk
            if time.monotonic() >= deadline:
                continue

        return line, dropped


def log(message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def load_secret_file() -> None:
    if not os.path.exists(SECRET_FILE):
        return

    with open(SECRET_FILE, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def api_key_name() -> str:
    return "OPENROUTER_API_KEY" if PROVIDER == "openrouter" else "CLINE_API_KEY"


def request_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if PROVIDER == "openrouter":
        headers["HTTP-Referer"] = os.environ.get("OPENROUTER_HTTP_REFERER", "https://local.aisubtitle")
        headers["X-Title"] = os.environ.get("OPENROUTER_APP_TITLE", "AISubtitle")
    return headers


def parse_line(line: str) -> dict[str, object]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {"text": line}

    text = payload.get("text", line)
    source_id = payload.get("id")
    parsed: dict[str, object] = {"text": str(text)}
    if isinstance(source_id, int):
        parsed["id"] = source_id

    for key in ("context_url", "context_title", "context_source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parsed[key] = value.strip()

    return parsed


def canonical_session_key(url: object) -> str | None:
    if not isinstance(url, str):
        return None

    raw = url.strip()
    if not raw:
        return None

    parsed = urllib.parse.urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw

    ignored_query_keys = {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "spm",
    }
    query_items = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in ignored_query_keys:
            continue
        query_items.append((key, value))

    normalized_query = urllib.parse.urlencode(query_items, doseq=True)
    normalized_path = parsed.path or "/"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            normalized_path,
            normalized_query,
            "",
        )
    )


def session_history(session_key: str | None) -> list[dict[str, str]]:
    if not session_key:
        return []
    return translation_sessions.get(session_key, [])


def record_session_turn(session_key: str | None, source_text: str, translated_text: str) -> None:
    if not session_key:
        return

    history = translation_sessions.setdefault(session_key, [])
    history.append({"source": source_text, "translation": translated_text})
    if len(history) > SESSION_HISTORY_LIMIT:
        del history[: len(history) - SESSION_HISTORY_LIMIT]


def current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_usage_state() -> dict[str, object]:
    try:
        with open(USAGE_STATE_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as error:
        log(f"usage state read failed: {error!r}")
        return {}


def save_usage_state(state: dict[str, object]) -> None:
    temp_path = USAGE_STATE_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, USAGE_STATE_FILE)
    except Exception as error:
        log(f"usage state write failed: {error!r}")


def numeric(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def openrouter_usage_metadata(response: dict[str, object], elapsed: float) -> dict[str, object]:
    global openrouter_session_cost

    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {
            "provider": "openrouter",
            "model": MODEL,
            "elapsed_seconds": round(elapsed, 3),
            "display": "OR cost n/a",
        }

    cost = numeric(usage.get("cost"))
    if cost is None:
        return {
            "provider": "openrouter",
            "model": MODEL,
            "elapsed_seconds": round(elapsed, 3),
            "display": "OR cost n/a",
        }

    state = load_usage_state()
    openrouter_state = state.get("openrouter")
    if not isinstance(openrouter_state, dict):
        openrouter_state = {}

    month_key = current_month_key()
    if openrouter_state.get("month") != month_key:
        openrouter_state = {"month": month_key, "month_cost": 0.0}

    month_cost = numeric(openrouter_state.get("month_cost")) or 0.0
    month_cost += cost
    openrouter_session_cost += cost
    openrouter_state["month"] = month_key
    openrouter_state["month_cost"] = month_cost
    state["openrouter"] = openrouter_state
    save_usage_state(state)

    return {
        "provider": "openrouter",
        "model": MODEL,
        "elapsed_seconds": round(elapsed, 3),
        "cost": cost,
        "session_cost": openrouter_session_cost,
        "month": month_key,
        "month_cost": month_cost,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "display": f"OR ${cost:.5f} · M ${month_cost:.5f}",
    }


def usage_metadata(response: dict[str, object], elapsed: float) -> dict[str, object] | None:
    if PROVIDER == "openrouter":
        return openrouter_usage_metadata(response, elapsed)

    return {
        "provider": "cline",
        "model": MODEL,
        "elapsed_seconds": round(elapsed, 3),
        "display": "Cline usage: dashboard",
    }


def context_lines(context: dict[str, object]) -> list[str]:
    lines: list[str] = []

    url = context.get("context_url")
    if isinstance(url, str) and url:
        source = context.get("context_source")
        source_note = f" ({source})" if isinstance(source, str) and source else ""
        lines.append(f"Page URL{source_note}: {url}")

    title = context.get("context_title")
    if isinstance(title, str) and title:
        lines.append(f"Page title: {title}")

    history = context.get("session_history")
    if isinstance(history, list) and history:
        lines.append("Recent same-URL subtitle context:")
        for item in history[-SESSION_HISTORY_LIMIT:]:
            if not isinstance(item, dict):
                continue
            source_text = str(item.get("source", "")).strip()
            translation = str(item.get("translation", "")).strip()
            if source_text and translation:
                lines.append(f"- Source: {source_text}")
                lines.append(f"  zh-TW: {translation}")

    return lines


def request_payload(text: str, context: dict[str, object]) -> dict[str, object]:
    user_parts = ["/no_think"]
    context_part = "\n".join(context_lines(context))
    if context_part:
        user_parts.append(
            "Use this page/session context only to resolve names, pronouns, and terminology. "
            "Do not mention the context in the output.\n" + context_part
        )
    user_parts.append("Translate this subtitle to zh-TW:\n" + text)

    return {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a live subtitle translator. Output ONLY zh-TW Traditional Chinese. "
                    "Use Taiwan Mandarin wording and Traditional Chinese characters. NEVER output "
                    "Simplified Chinese characters. Do not include reasoning, notes, quotes, prefixes, "
                    "or alternatives."
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(user_parts),
            },
        ],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING,
    }


def extract_translation(response: dict[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list):
        data = response.get("data")
        if isinstance(data, dict):
            choices = data.get("choices")

    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""

    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()

    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


def translate(text: str, api_key: str, context: dict[str, object]) -> tuple[str, dict[str, object] | None] | None:
    body = json.dumps(request_payload(text, context), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers=request_headers(api_key),
        method="POST",
    )

    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw_response = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        log(f"{PROVIDER} translation failed status={error.code}: {detail}")
        print(f"{PROVIDER} translation failed; logged to {LOG_FILE}", file=sys.stderr, flush=True)
        return None
    except Exception as error:
        log(f"{PROVIDER} translation failed before response: {error!r}")
        print(f"{PROVIDER} translation failed; logged to {LOG_FILE}", file=sys.stderr, flush=True)
        return None

    elapsed = time.perf_counter() - started_at
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        log(f"{PROVIDER} translation returned non-JSON after {elapsed:.2f}s")
        return None

    translated = extract_translation(payload)
    if not translated:
        log(f"{PROVIDER} translation returned empty content after {elapsed:.2f}s model={MODEL}")
        return None

    metadata = usage_metadata(payload, elapsed)
    usage_note = ""
    if metadata:
        usage_note = " " + str(metadata.get("display", ""))

    context_note = ""
    session_key = context.get("session_key")
    if isinstance(session_key, str) and session_key:
        context_source = context.get("context_source")
        source_text = context_source if isinstance(context_source, str) and context_source else "context"
        context_note = f" context={source_text} session={session_key[:120]}"

    log(f"{PROVIDER} translation ok model={MODEL} reasoning={REASONING} elapsed={elapsed:.2f}s{usage_note}{context_note}")
    return translated, metadata


def main() -> int:
    load_secret_file()
    key_name = api_key_name()
    api_key = os.environ.get(key_name, "").strip()
    if not api_key:
        log(f"{PROVIDER} translation failed before launch: {key_name} missing. secret={SECRET_FILE}")
        return 1

    reader = LineReader(sys.stdin.fileno())
    while True:
        line = reader.read_line()
        if line is None:
            break
        if not line:
            continue

        line, dropped = reader.drain_to_latest(line, DRAIN_SECONDS)
        if dropped:
            log(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} live translator dropped {dropped} stale ASR line(s)")

        parsed = parse_line(line)
        text = str(parsed.get("text", "")).strip()
        source_id = parsed.get("id")
        text = text.strip()
        if not text:
            continue

        session_key = canonical_session_key(parsed.get("context_url"))
        parsed["session_key"] = session_key or ""
        parsed["session_history"] = session_history(session_key)

        result = translate(text, api_key, parsed)
        if result is None:
            continue

        translated, metadata = result
        record_session_turn(session_key, text, translated)

        payload: dict[str, object] = {"text": translated}
        if isinstance(source_id, int):
            payload["id"] = source_id
        if session_key:
            payload["context_url"] = parsed.get("context_url")
            payload["session_key"] = session_key
        if metadata is not None:
            payload["usage"] = metadata
            display = metadata.get("display")
            if isinstance(display, str) and display:
                payload["usage_display"] = display

        print(json.dumps(payload, ensure_ascii=False), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
