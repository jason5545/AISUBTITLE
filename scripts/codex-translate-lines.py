#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
import concurrent.futures
import http.client
import threading
import urllib.parse
from dataclasses import dataclass
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
DRAIN_SECONDS = float(
    os.environ.get("AISUBTITLE_TRANSLATE_DRAIN_SECONDS")
    or os.environ.get("CODEX_TRANSLATE_DRAIN_SECONDS", "0.05")
)
CLINE_WEEKLY_LIMIT_HOURS = float(os.environ.get("CLINE_USAGE_WEEKLY_LIMIT_HOURS", "5"))
CLINE_MONTHLY_LIMIT_HOURS = float(os.environ.get("CLINE_USAGE_MONTHLY_LIMIT_HOURS", str(CLINE_WEEKLY_LIMIT_HOURS * 4)))
SESSION_HISTORY_LIMIT = int(os.environ.get("AISUBTITLE_TRANSLATE_SESSION_HISTORY_LIMIT", "6"))
LOOKAHEAD_LINES = min(1, max(0, int(os.environ.get("AISUBTITLE_TRANSLATE_LOOKAHEAD_LINES", "1"))))
LOOKAHEAD_MAX_DELAY_SECONDS = max(
    0.0,
    float(os.environ.get("AISUBTITLE_TRANSLATE_LOOKAHEAD_MAX_DELAY_SECONDS", "1.2")),
)
PREVIOUS_SOURCE_CONTEXT_LINES = max(
    0,
    int(os.environ.get("AISUBTITLE_TRANSLATE_PREVIOUS_SOURCE_CONTEXT_LINES", "2")),
)
ORDERED_EMISSION = (
    os.environ.get("AISUBTITLE_TRANSLATE_ORDERED_EMISSION", "1" if LOOKAHEAD_LINES > 0 else "0")
    .strip()
    .lower()
    in {"1", "true", "yes", "on"}
)
OPENCC_CONFIG = os.environ.get("AISUBTITLE_OPENCC_CONFIG", "s2twp.json").strip() or "s2twp.json"
OPENCC_BIN = os.environ.get("AISUBTITLE_OPENCC_BIN", "").strip()
VERBOSE_LOG = os.environ.get("AISUBTITLE_VERBOSE_LOG", "").strip().lower() in {"1", "true", "yes", "on"}

API_PARTS = urllib.parse.urlsplit(API_URL)
API_SCHEME = API_PARTS.scheme.lower()
API_HOST = API_PARTS.hostname or ""
API_PORT = API_PARTS.port
API_PATH = urllib.parse.urlunsplit(("", "", API_PARTS.path or "/", API_PARTS.query, ""))

MAX_IN_FLIGHT_TRANSLATIONS = max(
    1,
    int(
        os.environ.get("AISUBTITLE_TRANSLATE_MAX_IN_FLIGHT")
        or os.environ.get("CODEX_TRANSLATE_MAX_IN_FLIGHT", "3")
    ),
)

openrouter_session_cost = 0.0
translation_sessions: dict[str, list[dict[str, str]]] = {}
opencc_converter: object | None = None
opencc_import_failed = False
opencc_cli_path: str | None = None
missing_api_key_logged = False

_http_connection_local = threading.local()
_log_lock = threading.Lock()
_translation_sessions_lock = threading.Lock()
_usage_state_lock = threading.RLock()

_CONNECTION_ERRORS = (
    http.client.HTTPException,
    BrokenPipeError,
    ConnectionResetError,
    TimeoutError,
    OSError,
)

READ_TIMEOUT = object()

SUPPLEMENTARY_OPENCC_MAPPINGS = (
    ("優盤", "隨身碟"),
    ("拍制", "拍製"),
    ("賬", "帳"),
)

JAPANESE_NAME_OPENCC_REVERTS = (
    ("裡沙", "里沙"),
    ("裡奈", "里奈"),
    ("裡美", "里美"),
    ("裡香", "里香"),
    ("裡穗", "里穗"),
    ("裡菜", "里菜"),
    ("裡帆", "里帆"),
)


@dataclass(frozen=True)
class TranslationResult:
    source_id: int | None
    source_text: str
    translated_text: str
    metadata: dict[str, object] | None
    session_key: str | None
    context_url: object


@dataclass(frozen=True)
class TranslationJob:
    text: str
    source_id: int | None
    context: dict[str, object]
    session_key: str | None


class LineReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = b""
        self.eof = False

    def read_line(self, timeout: float | None = None) -> str | None | object:
        deadline = time.monotonic() + max(0.0, timeout) if timeout is not None else None

        while b"\n" not in self.buffer and not self.eof:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return READ_TIMEOUT
                readable, _, _ = select.select([self.fd], [], [], remaining)
                if not readable:
                    return READ_TIMEOUT

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
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} pid={os.getpid()} {message}\n")


def verbose(message: str) -> None:
    if VERBOSE_LOG:
        log("verbose " + message)


def preview(text: str, limit: int = 80) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def seconds_label(value: float | None) -> str:
    return f"{value:.3f}s" if value is not None else "na"


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

    direct = payload.get("direct")
    if isinstance(direct, bool):
        parsed["direct"] = direct

    language = payload.get("language")
    if isinstance(language, str) and language.strip():
        parsed["language"] = language.strip()

    for key in ("issued_at", "duration_seconds", "avg_logprob"):
        value = numeric(payload.get(key))
        if value is not None:
            parsed[key] = value

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
    with _translation_sessions_lock:
        return [dict(item) for item in translation_sessions.get(session_key, [])]


def record_session_turn(session_key: str | None, source_text: str, translated_text: str) -> None:
    if not session_key:
        return

    with _translation_sessions_lock:
        history = translation_sessions.setdefault(session_key, [])
        history.append({"source": source_text, "translation": translated_text})
        if len(history) > SESSION_HISTORY_LIMIT:
            del history[: len(history) - SESSION_HISTORY_LIMIT]


def current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_usage_state() -> dict[str, object]:
    with _usage_state_lock:
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
    with _usage_state_lock:
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

    with _usage_state_lock:
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
        session_cost = openrouter_session_cost
        openrouter_state["month"] = month_key
        openrouter_state["month_cost"] = month_cost
        state["openrouter"] = openrouter_state
        save_usage_state(state)

    return {
        "provider": "openrouter",
        "model": MODEL,
        "elapsed_seconds": round(elapsed, 3),
        "cost": cost,
        "session_cost": session_cost,
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


def direct_usage_metadata(elapsed: float) -> dict[str, object]:
    return {
        "provider": "direct",
        "model": "opencc",
        "elapsed_seconds": round(elapsed, 3),
        "display": "Direct",
    }


def apply_opencc_supplements(text: str) -> str:
    result = text
    for source, target in SUPPLEMENTARY_OPENCC_MAPPINGS:
        result = result.replace(source, target)
    for source, target in JAPANESE_NAME_OPENCC_REVERTS:
        result = result.replace(source, target)
    return result


def convert_with_python_opencc(text: str) -> str | None:
    global opencc_converter, opencc_import_failed

    if opencc_import_failed:
        return None

    try:
        if opencc_converter is None:
            from opencc import OpenCC  # type: ignore

            config_name = OPENCC_CONFIG.removesuffix(".json")
            opencc_converter = OpenCC(config_name)
        converted = opencc_converter.convert(text)  # type: ignore[attr-defined]
        return str(converted)
    except Exception as error:
        opencc_import_failed = True
        log(f"python opencc unavailable for direct mode: {error!r}")
        return None


def resolve_opencc_cli() -> str | None:
    global opencc_cli_path

    if opencc_cli_path is not None:
        return opencc_cli_path or None

    candidates = []
    if OPENCC_BIN:
        candidates.append(OPENCC_BIN)
    candidates.extend(
        [
            shutil.which("opencc"),
            "/opt/homebrew/bin/opencc",
            "/usr/local/bin/opencc",
        ]
    )

    for candidate in candidates:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            opencc_cli_path = candidate
            return candidate

    opencc_cli_path = ""
    return None


def convert_with_opencc_cli(text: str) -> str | None:
    executable = resolve_opencc_cli()
    if not executable:
        return None

    try:
        completed = subprocess.run(
            [executable, "-c", OPENCC_CONFIG],
            input=text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2.0,
            check=False,
        )
    except Exception as error:
        log(f"opencc cli failed for direct mode: {error!r}")
        return None

    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        log(f"opencc cli failed status={completed.returncode}: {detail}")
        return None

    return completed.stdout.strip()


def convert_chinese_direct(text: str) -> str:
    converted = convert_with_python_opencc(text)
    if converted is None:
        converted = convert_with_opencc_cli(text)
    if converted is None:
        log("opencc unavailable for direct mode; returning source text")
        converted = text
    return apply_opencc_supplements(converted).strip()


def direct_process(text: str) -> tuple[str, dict[str, object]]:
    started_at = time.perf_counter()
    converted = convert_chinese_direct(text)
    elapsed = time.perf_counter() - started_at
    log(f"direct subtitle ok opencc={OPENCC_CONFIG} elapsed={elapsed:.3f}s")
    return converted, direct_usage_metadata(elapsed)


def source_context_item(job: TranslationJob) -> dict[str, object]:
    item: dict[str, object] = {"text": job.text}
    if isinstance(job.source_id, int):
        item["id"] = job.source_id

    language = job.context.get("language")
    if isinstance(language, str) and language:
        item["language"] = language

    return item


def format_source_context_item(item: object) -> str | None:
    if not isinstance(item, dict):
        return None

    text = str(item.get("text", "")).strip()
    if not text:
        return None

    labels: list[str] = []
    source_id = item.get("id")
    if isinstance(source_id, int):
        labels.append(f"#{source_id}")
    language = item.get("language")
    if isinstance(language, str) and language.strip():
        labels.append(language.strip())

    prefix = f"[{', '.join(labels)}] " if labels else ""
    return prefix + text


def context_lines(context: dict[str, object]) -> list[str]:
    lines: list[str] = []

    previous = context.get("previous_subtitles")
    target = context.get("target_subtitle")
    next_subtitle = context.get("next_subtitle")
    has_nearby_context = (
        (isinstance(previous, list) and bool(previous))
        or isinstance(target, dict)
        or isinstance(next_subtitle, dict)
    )
    if has_nearby_context:
        lines.append("Nearby source subtitles for semantic context:")
        if isinstance(previous, list):
            previous_items = previous[-PREVIOUS_SOURCE_CONTEXT_LINES:] if PREVIOUS_SOURCE_CONTEXT_LINES > 0 else []
            for item in previous_items:
                formatted = format_source_context_item(item)
                if formatted:
                    lines.append(f"- Previous: {formatted}")

        formatted_target = format_source_context_item(target)
        if formatted_target:
            lines.append(f"- TARGET: {formatted_target}")

        formatted_next = format_source_context_item(next_subtitle)
        if formatted_next:
            lines.append(f"- Next: {formatted_next}")

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
        if isinstance(context.get("target_subtitle"), dict):
            user_parts.append(
                "Use this context to translate the TARGET subtitle naturally. "
                "Previous and Next lines are context only: do not translate them into the output, "
                "but use them to choose natural Taiwan Mandarin word order, references, and tone. "
                "Keep the output suitable for the TARGET subtitle timing. Handle ASR subtitle splits "
                "carefully: if TARGET ends with an incomplete number, currency amount, percentage, "
                "proper noun, or fixed phrase that clearly continues in Next, do not finalize a wrong "
                "partial translation; output a natural lead-in for TARGET. If TARGET clearly continues "
                "a number, currency amount, percentage, proper noun, or fixed phrase from Previous, "
                "translate the completed local fragment for TARGET so the subtitle reads continuously.\n"
                + context_part
            )
        else:
            user_parts.append(
                "Use this page/session context only to resolve names, pronouns, and terminology. "
                "Do not mention the context in the output.\n" + context_part
            )

    if isinstance(context.get("target_subtitle"), dict):
        user_parts.append(
            "Translate ONLY the TARGET subtitle to zh-TW Traditional Chinese. "
            "The output must be one subtitle line, with no quotes, labels, or notes:\n" + text
        )
    else:
        user_parts.append("Translate this subtitle to zh-TW:\n" + text)

    payload: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a live subtitle translator. Output ONLY zh-TW Traditional Chinese. "
                    "Use natural Taiwan Mandarin wording and Traditional Chinese characters. Translate "
                    "for subtitles instead of doing literal sentence-by-sentence conversion. You may adjust "
                    "word order using nearby context, and you may repair obvious subtitle-boundary fragments "
                    "such as split numbers or currency amounts, but output only the requested subtitle line. "
                    "NEVER output Simplified Chinese characters. Do not include reasoning, notes, quotes, "
                    "prefixes, or alternatives."
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
    if PROVIDER == "openrouter":
        payload["provider"] = {"sort": "latency"}
    return payload


def close_persistent_connection() -> None:
    connection = getattr(_http_connection_local, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
    _http_connection_local.connection = None


def persistent_connection() -> http.client.HTTPSConnection:
    if API_SCHEME != "https" or not API_HOST:
        raise ValueError(f"unsupported API_URL for persistent HTTPS connection: {API_URL!r}")

    connection = getattr(_http_connection_local, "connection", None)
    if connection is None:
        connection = http.client.HTTPSConnection(API_HOST, API_PORT, timeout=TIMEOUT_SECONDS)
        _http_connection_local.connection = connection
    return connection


def post_json_once(api_key: str, body: bytes) -> tuple[int, str]:
    connection = persistent_connection()
    connection.request("POST", API_PATH, body=body, headers=request_headers(api_key))
    response = connection.getresponse()
    try:
        raw_response = response.read().decode("utf-8", errors="replace")
        status = response.status
        should_close = response.will_close
    except Exception:
        close_persistent_connection()
        raise

    if should_close:
        close_persistent_connection()
    return status, raw_response


def post_json_with_retry(api_key: str, body: bytes) -> tuple[int, str]:
    last_error: BaseException | None = None
    for attempt in range(2):
        try:
            return post_json_once(api_key, body)
        except _CONNECTION_ERRORS as error:
            last_error = error
            close_persistent_connection()
            if attempt == 0:
                continue
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("persistent HTTP request failed without an exception")


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
    source_id = context.get("id")

    started_at = time.perf_counter()
    verbose(f"http-start id={source_id} host={API_HOST} model={MODEL} bytes={len(body)}")
    try:
        status, raw_response = post_json_with_retry(api_key, body)
    except Exception as error:
        elapsed = time.perf_counter() - started_at
        log(f"{PROVIDER} translation failed before response id={source_id} elapsed={elapsed:.3f}s: {error!r}")
        print(f"{PROVIDER} translation failed; logged to {LOG_FILE}", file=sys.stderr, flush=True)
        return None

    elapsed = time.perf_counter() - started_at
    verbose(f"http-response id={source_id} status={status} elapsed={elapsed:.3f}s bytes={len(raw_response)}")
    if status >= 400:
        detail = raw_response[:1000]
        log(f"{PROVIDER} translation failed id={source_id} status={status} elapsed={elapsed:.3f}s: {detail}")
        print(f"{PROVIDER} translation failed; logged to {LOG_FILE}", file=sys.stderr, flush=True)
        return None

    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        log(f"{PROVIDER} translation returned non-JSON id={source_id} after {elapsed:.2f}s")
        return None

    translated = extract_translation(payload)
    if not translated:
        log(f"{PROVIDER} translation returned empty content id={source_id} after {elapsed:.2f}s model={MODEL}")
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

    log(f"{PROVIDER} translation ok id={source_id} model={MODEL} reasoning={REASONING} elapsed={elapsed:.2f}s{usage_note}{context_note}")
    return translated, metadata


def run_translation_job(
    text: str,
    source_id: int | None,
    api_key: str,
    context: dict[str, object],
) -> TranslationResult | None:
    started_wall = time.time()
    started = time.perf_counter()
    issued_at = numeric(context.get("issued_at"))
    received_at = numeric(context.get("_received_at"))
    queue_wait = numeric(context.get("_queue_wait_seconds"))
    issue_age = started_wall - issued_at if issued_at is not None else None
    input_wait = started_wall - received_at if received_at is not None else None
    verbose(f"job-start id={source_id} issue_age={seconds_label(issue_age)}")
    verbose(
        f"job-context id={source_id} "
        f"queue_wait={seconds_label(queue_wait)} "
        f"input_wait={seconds_label(input_wait)} "
        f"text={preview(text)!r}"
    )
    result = translate(text, api_key, context)
    if result is None:
        verbose(f"job-failed id={source_id} elapsed={time.perf_counter() - started:.3f}s")
        return None

    verbose(f"job-done id={source_id} elapsed={time.perf_counter() - started:.3f}s")
    translated, metadata = result
    session_key = context.get("session_key")
    return TranslationResult(
        source_id=source_id,
        source_text=text,
        translated_text=translated,
        metadata=metadata,
        session_key=session_key if isinstance(session_key, str) and session_key else None,
        context_url=context.get("context_url"),
    )


def result_payload(result: TranslationResult) -> dict[str, object]:
    payload: dict[str, object] = {"text": result.translated_text}
    if isinstance(result.source_id, int):
        payload["id"] = result.source_id
    if result.session_key:
        payload["context_url"] = result.context_url
        payload["session_key"] = result.session_key
    if result.metadata is not None:
        payload["usage"] = result.metadata
        display = result.metadata.get("display")
        if isinstance(display, str) and display:
            payload["usage_display"] = display
    return payload


def main() -> int:
    global missing_api_key_logged

    load_secret_file()
    key_name = api_key_name()

    last_emitted_id: int | None = None
    next_expected_emit_id: int | None = None
    expected_emit_ids: set[int] = set()
    pending_emit_results: dict[int, TranslationResult] = {}
    recent_sources_by_session: dict[str, list[dict[str, object]]] = {}
    emit_lock = threading.Lock()
    in_flight = threading.BoundedSemaphore(MAX_IN_FLIGHT_TRANSLATIONS)

    def emit_now(result: TranslationResult) -> None:
        nonlocal last_emitted_id

        source_id = result.source_id
        if isinstance(source_id, int):
            last_emitted_id = source_id

        record_session_turn(result.session_key, result.source_text, result.translated_text)
        verbose(f"emit id={source_id} text={preview(result.translated_text)!r}")
        print(json.dumps(result_payload(result), ensure_ascii=False), flush=True)

    def next_expected_id() -> int | None:
        return min(expected_emit_ids) if expected_emit_ids else None

    def flush_ordered_results_locked() -> None:
        nonlocal next_expected_emit_id

        while next_expected_emit_id is not None:
            source_id = next_expected_emit_id
            result = pending_emit_results.pop(source_id, None)
            if result is None:
                if source_id in expected_emit_ids:
                    return
                next_expected_emit_id = next_expected_id()
                continue

            expected_emit_ids.discard(source_id)
            if last_emitted_id is not None and source_id <= last_emitted_id:
                log(f"{PROVIDER} skipped duplicate ordered id={source_id} last_emitted_id={last_emitted_id}")
            else:
                emit_now(result)
            next_expected_emit_id = next_expected_id()

    def register_expected_emit_id(source_id: int | None) -> None:
        nonlocal next_expected_emit_id

        if not ORDERED_EMISSION or not isinstance(source_id, int):
            return

        with emit_lock:
            if last_emitted_id is not None and source_id <= last_emitted_id:
                return
            expected_emit_ids.add(source_id)
            if next_expected_emit_id is None or source_id < next_expected_emit_id:
                next_expected_emit_id = source_id

    def mark_result_failed(source_id: int | None) -> None:
        if not ORDERED_EMISSION or not isinstance(source_id, int):
            return

        with emit_lock:
            expected_emit_ids.discard(source_id)
            pending_emit_results.pop(source_id, None)
            flush_ordered_results_locked()

    def emit_result(result: TranslationResult | None) -> None:
        nonlocal next_expected_emit_id

        if result is None:
            return

        source_id = result.source_id
        if ORDERED_EMISSION and isinstance(source_id, int):
            with emit_lock:
                if last_emitted_id is not None and source_id <= last_emitted_id:
                    log(f"{PROVIDER} skipped out-of-order id={source_id} last_emitted_id={last_emitted_id}")
                    return
                expected_emit_ids.add(source_id)
                if next_expected_emit_id is None or source_id < next_expected_emit_id:
                    next_expected_emit_id = source_id
                pending_emit_results[source_id] = result
                flush_ordered_results_locked()
            return

        with emit_lock:
            if isinstance(source_id, int):
                if last_emitted_id is not None and source_id <= last_emitted_id:
                    log(f"{PROVIDER} skipped out-of-order id={source_id} last_emitted_id={last_emitted_id}")
                    return
            emit_now(result)

    def finish_future(source_id: int | None, future: concurrent.futures.Future[TranslationResult | None]) -> None:
        try:
            result = future.result()
            if result is None:
                mark_result_failed(source_id)
            else:
                emit_result(result)
        except Exception as error:
            mark_result_failed(source_id)
            log(f"{PROVIDER} translation worker failed: {error!r}")
            print(f"{PROVIDER} translation failed; logged to {LOG_FILE}", file=sys.stderr, flush=True)
        finally:
            in_flight.release()

    def source_history_key(session_key: str | None) -> str:
        return session_key or "__default__"

    def previous_source_context(job: TranslationJob) -> list[dict[str, object]]:
        if PREVIOUS_SOURCE_CONTEXT_LINES <= 0:
            return []

        history = recent_sources_by_session.get(source_history_key(job.session_key), [])
        return [dict(item) for item in history[-PREVIOUS_SOURCE_CONTEXT_LINES:]]

    def record_source_context(job: TranslationJob) -> None:
        if PREVIOUS_SOURCE_CONTEXT_LINES <= 0:
            return

        key = source_history_key(job.session_key)
        history = recent_sources_by_session.setdefault(key, [])
        history.append(source_context_item(job))
        history_limit = max(PREVIOUS_SOURCE_CONTEXT_LINES, SESSION_HISTORY_LIMIT, 4)
        if len(history) > history_limit:
            del history[: len(history) - history_limit]

    def same_source_session(left: TranslationJob, right: TranslationJob) -> bool:
        return source_history_key(left.session_key) == source_history_key(right.session_key)

    def can_use_next_context(left: TranslationJob, right: TranslationJob) -> bool:
        if not same_source_session(left, right):
            return False

        if isinstance(left.source_id, int) and isinstance(right.source_id, int):
            return right.source_id == left.source_id + 1

        return True

    def translation_context(job: TranslationJob, next_job: TranslationJob | None = None) -> dict[str, object]:
        context = dict(job.context)
        context["target_subtitle"] = source_context_item(job)

        previous = previous_source_context(job)
        if previous:
            context["previous_subtitles"] = previous

        if next_job is not None and can_use_next_context(job, next_job):
            context["next_subtitle"] = source_context_item(next_job)

        return context

    def submit_translation_job(
        executor: concurrent.futures.ThreadPoolExecutor,
        job: TranslationJob,
        next_job: TranslationJob | None = None,
    ) -> bool:
        global missing_api_key_logged

        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            if not missing_api_key_logged:
                log(f"{PROVIDER} translation failed: {key_name} missing. secret={SECRET_FILE}")
                missing_api_key_logged = True
            return False

        context = translation_context(job, next_job)
        wait_started = time.perf_counter()
        in_flight.acquire()
        queue_wait = time.perf_counter() - wait_started
        context["_queue_wait_seconds"] = queue_wait
        if queue_wait >= 0.05:
            log(f"{PROVIDER} waited for translation slot id={job.source_id} queue_wait={queue_wait:.3f}s")
        else:
            verbose(f"slot-ready id={job.source_id} queue_wait={queue_wait:.3f}s")

        register_expected_emit_id(job.source_id)
        try:
            future = executor.submit(run_translation_job, job.text, job.source_id, api_key, context)
        except Exception:
            in_flight.release()
            mark_result_failed(job.source_id)
            raise
        future.add_done_callback(lambda future, source_id=job.source_id: finish_future(source_id, future))
        record_source_context(job)
        return True

    def pending_read_timeout(pending: TranslationJob | None) -> float | None:
        if LOOKAHEAD_LINES <= 0 or pending is None:
            return None

        received_at = numeric(pending.context.get("_received_at")) or time.time()
        elapsed = max(0.0, time.time() - received_at)
        return max(0.0, LOOKAHEAD_MAX_DELAY_SECONDS - elapsed)

    reader = LineReader(sys.stdin.fileno())
    pending_translation: TranslationJob | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT_TRANSLATIONS) as executor:
        while True:
            raw_line = reader.read_line(timeout=pending_read_timeout(pending_translation))
            if raw_line is READ_TIMEOUT:
                if pending_translation is not None:
                    verbose(f"lookahead-timeout id={pending_translation.source_id}")
                    submit_translation_job(executor, pending_translation)
                    pending_translation = None
                continue

            if raw_line is None:
                break

            line = str(raw_line)
            if not line:
                continue
            line, dropped = reader.drain_to_latest(line, DRAIN_SECONDS)
            if dropped:
                log(f"{PROVIDER} drained stale input lines dropped={dropped} window={DRAIN_SECONDS:.3f}s")

            parsed = parse_line(line)
            received_at = time.time()
            text = str(parsed.get("text", "")).strip()
            source_id = parsed.get("id")
            source_id = source_id if isinstance(source_id, int) else None
            if not text:
                continue
            parsed["_received_at"] = received_at
            issued_at = numeric(parsed.get("issued_at"))
            issue_age = received_at - issued_at if issued_at is not None else None
            verbose(
                f"recv id={source_id} "
                f"issue_age={seconds_label(issue_age)} "
                f"lang={parsed.get('language', 'unknown')} "
                f"direct={parsed.get('direct') is True} "
                f"text={preview(text)!r}"
            )

            session_key = canonical_session_key(parsed.get("context_url"))
            parsed["session_key"] = session_key or ""
            parsed["session_history"] = session_history(session_key)
            job = TranslationJob(text=text, source_id=source_id, context=dict(parsed), session_key=session_key)

            if parsed.get("direct") is True:
                if pending_translation is not None:
                    submit_translation_job(executor, pending_translation, next_job=job)
                    pending_translation = None

                register_expected_emit_id(source_id)
                direct_started = time.perf_counter()
                verbose(f"direct-start id={source_id}")
                translated, metadata = direct_process(text)
                verbose(f"direct-done id={source_id} elapsed={time.perf_counter() - direct_started:.3f}s")
                emit_result(
                    TranslationResult(
                        source_id=source_id,
                        source_text=text,
                        translated_text=translated,
                        metadata=metadata,
                        session_key=session_key,
                        context_url=parsed.get("context_url"),
                    )
                )
                record_source_context(job)
                continue

            if LOOKAHEAD_LINES > 0:
                if pending_translation is not None:
                    submit_translation_job(executor, pending_translation, next_job=job)
                pending_translation = job
                verbose(
                    f"lookahead-pending id={source_id} "
                    f"delay={LOOKAHEAD_MAX_DELAY_SECONDS:.3f}s "
                    f"text={preview(text)!r}"
                )
                continue

            submit_translation_job(executor, job)

        if pending_translation is not None:
            verbose(f"lookahead-flush-eof id={pending_translation.source_id}")
            submit_translation_job(executor, pending_translation)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
