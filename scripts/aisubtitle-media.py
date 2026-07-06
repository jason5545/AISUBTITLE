#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = (
    Path.home()
    / "Library/Application Support/com.jasonchien.Voco/Qwen3Models/mlx-community_Qwen3-ASR-1.7B-8bit"
)
DEFAULT_ASR_BIN = REPO_ROOT / ".build/debug/qwen3-asr-stdin"
DEFAULT_OVERLAY_BIN = REPO_ROOT / ".build/debug/aisubtitle"
DEFAULT_OVERLAY_SOCKET = f"/tmp/aisubtitle-overlay-{os.getuid()}.sock"
DEFAULT_TRANSLATOR = REPO_ROOT / "scripts/codex-translate-lines.sh"
VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
    ".wmv",
}


@dataclass(frozen=True)
class Chapter:
    index: int
    title: str
    start: float
    end: float | None

    @property
    def duration(self) -> float | None:
        if self.end is None:
            return None
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class MediaCandidate:
    path: Path
    size: int
    modified_at: float


def default_media_directory() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return downloads
    return Path.home() / "Download"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play a media file as audio-only while streaming subtitles in the terminal.",
    )
    parser.add_argument("media", nargs="?", help="Video or audio file. Omit to pick a video from Downloads.")
    parser.add_argument("--media-dir", default=str(default_media_directory()), help="Directory to scan when media is omitted.")
    parser.add_argument("--list-videos", action="store_true", help="List videos in the media directory and exit.")
    parser.add_argument("--chapter", help="1-based chapter number or title substring.")
    parser.add_argument("--all", action="store_true", help="Use the whole file; skip chapter selection.")
    parser.add_argument("--list-chapters", action="store_true", help="List chapters and exit.")
    parser.add_argument("--audio-only", action="store_true", help="Accepted for readability; audio-only playback is the default.")
    parser.add_argument("--no-play", action="store_true", help="Do not play audio; only transcribe/translate.")
    parser.add_argument("--no-translate", action="store_true", help="Print ASR text without translation.")
    parser.add_argument("--no-floating-window", action="store_true", help="Do not mirror subtitles to the floating overlay window.")
    parser.add_argument("--plain", action="store_true", help="Print subtitle lines instead of the now-playing terminal UI.")
    parser.add_argument("--json", action="store_true", help="Print raw JSONL events.")
    parser.add_argument("--show-usage", action="store_true", help="Append translator usage display to text output.")
    parser.add_argument("--no-direct-chinese", action="store_true", help="Translate Chinese ASR through the LLM instead of OpenCC direct mode.")
    parser.add_argument("--language", default="auto", help="ASR language, default: auto.")
    parser.add_argument("--prompt", help="Optional ASR prompt.")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR), help="Qwen3-ASR model directory.")
    parser.add_argument("--asr-bin", default=str(DEFAULT_ASR_BIN), help="Path to qwen3-asr-stdin.")
    parser.add_argument("--overlay-bin", default=str(DEFAULT_OVERLAY_BIN), help="Path to AISubtitle overlay executable.")
    parser.add_argument("--overlay-socket", default=DEFAULT_OVERLAY_SOCKET, help="Unix socket for an existing AISubtitle floating window.")
    parser.add_argument("--translator", default=str(DEFAULT_TRANSLATOR), help="Translator command.")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg", help="ffmpeg executable.")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe", help="ffprobe executable.")
    parser.add_argument("--ffplay", default=shutil.which("ffplay") or "ffplay", help="ffplay executable.")
    parser.add_argument("--min-segment-seconds", default="1.1")
    parser.add_argument("--max-segment-seconds", default="3.2")
    parser.add_argument("--silence-seconds", default="0.28")
    parser.add_argument("--silence-rms")
    return parser.parse_args()


def require_executable(path: str, label: str) -> str:
    expanded = os.path.expanduser(path)
    resolved = shutil.which(expanded) if os.path.sep not in expanded else expanded
    if not resolved or not os.path.exists(resolved) or not os.access(resolved, os.X_OK):
        raise SystemExit(f"{label} not executable: {path}")
    return resolved


def require_directory(path: str, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise SystemExit(f"{label} not found: {resolved}")
    if not resolved.is_dir():
        raise SystemExit(f"{label} is not a directory: {resolved}")
    return resolved


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_size(bytes_count: int) -> str:
    value = float(bytes_count)
    for suffix in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or suffix == "GB":
            if suffix == "B":
                return f"{int(value)} {suffix}"
            return f"{value:.1f} {suffix}"
        value /= 1024.0
    return f"{value:.1f} GB"


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS


def scan_video_files(directory: Path) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for root, dirnames, filenames in os.walk(directory):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            path = Path(root) / filename
            if not is_video_file(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append(MediaCandidate(path=path.resolve(), size=stat.st_size, modified_at=stat.st_mtime))

    return sorted(candidates, key=lambda item: (-item.modified_at, str(item.path).casefold()))


def media_line(candidate: MediaCandidate, base_directory: Path) -> str:
    try:
        display_path = candidate.path.relative_to(base_directory)
    except ValueError:
        display_path = candidate.path
    modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(candidate.modified_at))
    return f"{modified} · {fmt_size(candidate.size):>8}  {display_path}"


def print_videos(candidates: list[MediaCandidate], base_directory: Path) -> None:
    if not candidates:
        print(f"No videos found in {base_directory}")
        return
    for index, candidate in enumerate(candidates, start=1):
        print(f"{index:>2}. {media_line(candidate, base_directory)}")


def styled_selected(line: str, selected: bool) -> str:
    if not selected:
        return line
    return f"\x1b[7m{line}\x1b[0m"


def interactive_select_media(candidates: list[MediaCandidate], base_directory: Path) -> Path:
    if not candidates:
        raise ValueError("interactive_select_media requires candidates")

    selected = 0
    old_settings = termios.tcgetattr(sys.stdin.fileno())

    def draw() -> None:
        window = 14
        half = window // 2
        start = max(0, min(selected - half, max(0, len(candidates) - window)))
        end = min(len(candidates), start + window)
        sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.write(f"選影片  {len(candidates)} files\n")
        sys.stdout.write("↑/↓ 或 j/k 移動，Enter 開始，q 取消\n")
        sys.stdout.write(f"{base_directory}\n\n")
        for offset in range(start, end):
            prefix = "▶" if offset == selected else " "
            line = f"{prefix} {offset + 1:>2}. {media_line(candidates[offset], base_directory)}"
            sys.stdout.write(styled_selected(line, offset == selected) + "\n")
        selected_item = candidates[selected]
        sys.stdout.write("\n")
        sys.stdout.write(f"Selected: {selected_item.path.name}\n")
        sys.stdout.write(f"Path: {selected_item.path}\n")
        sys.stdout.flush()

    try:
        tty.setraw(sys.stdin.fileno())
        draw()
        while True:
            key = sys.stdin.buffer.read(1)
            if key in (b"\r", b"\n"):
                return candidates[selected].path
            if key in (b"q", b"Q", b"\x03"):
                raise SystemExit("Canceled.")
            if key == b"\x1b":
                rest = sys.stdin.buffer.read(2)
                if rest == b"[A":
                    selected = max(0, selected - 1)
                elif rest == b"[B":
                    selected = min(len(candidates) - 1, selected + 1)
                elif rest == b"[5":
                    sys.stdin.buffer.read(1)
                    selected = max(0, selected - 10)
                elif rest == b"[6":
                    sys.stdin.buffer.read(1)
                    selected = min(len(candidates) - 1, selected + 10)
                else:
                    raise SystemExit("Canceled.")
                draw()
            elif key in (b"k", b"K"):
                selected = max(0, selected - 1)
                draw()
            elif key in (b"j", b"J"):
                selected = min(len(candidates) - 1, selected + 1)
                draw()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
        sys.stdout.flush()


def resolve_media(args: argparse.Namespace) -> Path | None:
    if args.media:
        media_path = Path(args.media).expanduser().resolve()
        if media_path.is_file():
            if args.list_videos:
                print(f" 1. {media_path}")
                return None
            return media_path
        if not media_path.exists():
            raise SystemExit(f"media not found: {media_path}")
        if not media_path.is_dir():
            raise SystemExit(f"media is not a file or directory: {media_path}")
        base_directory = media_path
    else:
        base_directory = require_directory(args.media_dir, "media directory")

    candidates = scan_video_files(base_directory)
    if args.list_videos:
        print_videos(candidates, base_directory)
        return None
    if not candidates:
        raise SystemExit(f"No videos found in {base_directory}")
    if len(candidates) == 1:
        return candidates[0].path
    if sys.stdin.isatty():
        return interactive_select_media(candidates, base_directory)
    raise SystemExit("Multiple videos found. Pass a media file, run in an interactive terminal, or use --list-videos.")


def ffprobe_json(ffprobe: str, media: Path) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_chapters",
        "-show_format",
        str(media),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SystemExit(f"ffprobe failed: {detail}")
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"ffprobe returned invalid JSON: {error}") from error


def read_chapters(ffprobe: str, media: Path) -> tuple[list[Chapter], float | None]:
    payload = ffprobe_json(ffprobe, media)
    duration = None
    fmt = payload.get("format")
    if isinstance(fmt, dict):
        try:
            duration = float(fmt.get("duration"))
        except (TypeError, ValueError):
            duration = None

    chapters: list[Chapter] = []
    for raw in payload.get("chapters", []):
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start_time"))
        except (TypeError, ValueError):
            continue
        try:
            end = float(raw.get("end_time"))
        except (TypeError, ValueError):
            end = None
        if end is not None and end <= start:
            end = None

        tags = raw.get("tags")
        title = ""
        if isinstance(tags, dict):
            title = str(tags.get("title") or tags.get("TITLE") or "").strip()
        chapters.append(Chapter(index=len(chapters) + 1, title=title or f"Chapter {len(chapters) + 1}", start=start, end=end))

    return chapters, duration


def chapter_line(chapter: Chapter) -> str:
    duration = chapter.duration
    tail = f" · {fmt_time(duration)}" if duration is not None else ""
    return f"{chapter.index:>2}. {fmt_time(chapter.start)}{tail}  {chapter.title}"


def print_chapters(chapters: list[Chapter], duration: float | None) -> None:
    if not chapters:
        print(f"  whole file · {fmt_time(duration)}")
        return
    for chapter in chapters:
        print(chapter_line(chapter))


def resolve_chapter_selector(selector: str, chapters: list[Chapter]) -> Chapter:
    value = selector.strip()
    if value.isdigit():
        index = int(value)
        for chapter in chapters:
            if chapter.index == index:
                return chapter
        raise SystemExit(f"Chapter number out of range: {index}")

    needle = value.casefold()
    matches = [chapter for chapter in chapters if needle in chapter.title.casefold()]
    if not matches:
        raise SystemExit(f"No chapter title matches: {selector}")
    if len(matches) > 1:
        choices = ", ".join(str(chapter.index) for chapter in matches)
        raise SystemExit(f"Chapter selector matched more than one chapter: {choices}")
    return matches[0]


def interactive_select_chapter(chapters: list[Chapter]) -> Chapter:
    if not chapters:
        raise ValueError("interactive_select_chapter requires chapters")

    selected = 0
    old_settings = termios.tcgetattr(sys.stdin.fileno())

    def draw() -> None:
        window = 12
        half = window // 2
        start = max(0, min(selected - half, max(0, len(chapters) - window)))
        end = min(len(chapters), start + window)
        sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.write(f"選章節  {len(chapters)} chapters\n")
        sys.stdout.write("↑/↓ 或 j/k 移動，Enter 開始，q 取消\n\n")
        for offset in range(start, end):
            prefix = "▶" if offset == selected else " "
            line = f"{prefix} {chapter_line(chapters[offset])}"
            sys.stdout.write(styled_selected(line, offset == selected) + "\n")
        selected_item = chapters[selected]
        sys.stdout.write("\n")
        sys.stdout.write(f"Selected: {selected_item.title} · {fmt_time(selected_item.duration)}\n")
        sys.stdout.flush()

    try:
        tty.setraw(sys.stdin.fileno())
        draw()
        while True:
            key = sys.stdin.buffer.read(1)
            if key in (b"\r", b"\n"):
                return chapters[selected]
            if key in (b"q", b"Q", b"\x03"):
                raise SystemExit("Canceled.")
            if key == b"\x1b":
                rest = sys.stdin.buffer.read(2)
                if rest == b"[A":
                    selected = max(0, selected - 1)
                elif rest == b"[B":
                    selected = min(len(chapters) - 1, selected + 1)
                elif rest == b"[5":
                    sys.stdin.buffer.read(1)
                    selected = max(0, selected - 10)
                elif rest == b"[6":
                    sys.stdin.buffer.read(1)
                    selected = min(len(chapters) - 1, selected + 10)
                else:
                    raise SystemExit("Canceled.")
                draw()
            elif key in (b"k", b"K"):
                selected = max(0, selected - 1)
                draw()
            elif key in (b"j", b"J"):
                selected = min(len(chapters) - 1, selected + 1)
                draw()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
        sys.stdout.flush()


def choose_chapter(args: argparse.Namespace, chapters: list[Chapter], duration: float | None) -> Chapter:
    whole = Chapter(index=0, title="Whole file", start=0.0, end=duration)
    if args.all:
        return whole
    if args.chapter:
        if not chapters:
            raise SystemExit("Media file has no chapters.")
        return resolve_chapter_selector(args.chapter, chapters)
    if not chapters:
        return whole
    if sys.stdin.isatty() and len(chapters) > 1:
        return interactive_select_chapter(chapters)
    return chapters[0]


def bounded_args(chapter: Chapter) -> list[str]:
    args: list[str] = []
    if chapter.start > 0:
        args.extend(["-ss", f"{chapter.start:.3f}"])
    if chapter.duration is not None and chapter.duration > 0:
        args.extend(["-t", f"{chapter.duration:.3f}"])
    return args


def ffmpeg_command(ffmpeg: str, media: Path, chapter: Chapter) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        *bounded_args(chapter),
        "-re",
        "-i",
        str(media),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "-",
    ]


def ffplay_command(ffplay: str, media: Path, chapter: Chapter) -> list[str]:
    return [
        ffplay,
        "-nodisp",
        "-vn",
        "-autoexit",
        "-hide_banner",
        "-loglevel",
        "warning",
        *bounded_args(chapter),
        str(media),
    ]


def asr_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(Path(args.asr_bin).expanduser().resolve()),
        "--model-dir",
        str(Path(args.model_dir).expanduser()),
        "--language",
        args.language,
        "--min-segment-seconds",
        args.min_segment_seconds,
        "--max-segment-seconds",
        args.max_segment_seconds,
        "--silence-seconds",
        args.silence_seconds,
    ]
    if args.silence_rms:
        command.extend(["--silence-rms", args.silence_rms])
    if args.prompt:
        command.extend(["--prompt", args.prompt])
    return command


def pipe_stderr(process: subprocess.Popen[bytes], label: str) -> threading.Thread:
    def worker() -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                print(f"{label}: {line}", file=sys.stderr, flush=True)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def parse_event(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {"text": line}
    if not isinstance(payload, dict):
        return {"text": line}
    return payload


def looks_like_chinese_text(text: str) -> bool:
    han = 0
    latin = 0
    kana_or_hangul = 0
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            han += 1
        elif 0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A:
            latin += 1
        elif 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF:
            kana_or_hangul += 1
    return han > 0 and latin == 0 and kana_or_hangul == 0


def should_direct_chinese(payload: dict[str, Any]) -> bool:
    language = str(payload.get("language") or payload.get("lang") or "").strip().lower()
    if language:
        return (
            language == "zh"
            or language.startswith("zh-")
            or language == "cmn"
            or language.startswith("cmn-")
            or language == "yue"
            or language.startswith("yue-")
            or language in {
                "chinese",
                "mandarin",
                "traditionalchinese",
                "simplifiedchinese",
                "traditional chinese",
                "simplified chinese",
            }
        )
    return looks_like_chinese_text(str(payload.get("text") or ""))


def event_text(line: str, show_usage: bool = False) -> str:
    payload = parse_event(line)
    text = str(payload.get("text") or payload.get("translation") or payload.get("result") or "").strip()
    if not text:
        return line.strip()
    if show_usage:
        usage = payload.get("usage_display")
        if not isinstance(usage, str):
            usage_dict = payload.get("usage")
            usage = usage_dict.get("display") if isinstance(usage_dict, dict) else None
        if isinstance(usage, str) and usage.strip():
            text += f"  [{usage.strip()}]"
    return text


def usage_display(payload: dict[str, Any]) -> str | None:
    usage = payload.get("usage_display")
    if isinstance(usage, str) and usage.strip():
        return usage.strip()
    usage_dict = payload.get("usage")
    if isinstance(usage_dict, dict):
        display = usage_dict.get("display")
        if isinstance(display, str) and display.strip():
            return display.strip()
    return None


def progress_bar(progress: float | None, width: int = 28) -> str:
    if progress is None:
        return "[" + "-" * width + "]"
    progress = max(0.0, min(progress, 1.0))
    filled = int(round(progress * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class NowPlayingUI:
    def __init__(self, media: Path, chapter: Chapter, enabled: bool):
        self.media = media
        self.chapter = chapter
        self.enabled = enabled
        self.started_at = time.monotonic()
        self.status = "Starting"
        self.latest = ""
        self.latest_usage = ""
        self.recent: list[str] = []
        self.lock = threading.Lock()
        self.draw_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            print(f"Media: {self.media.name}", file=sys.stderr)
            print(f"Chapter: {chapter_line(self.chapter) if self.chapter.index else 'whole file'}", file=sys.stderr)
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_status(self, status: str) -> None:
        with self.lock:
            self.status = status
        self.redraw()

    def emit_line(self, line: str, raw_json: bool, show_usage: bool) -> None:
        if raw_json or not self.enabled:
            print(line if raw_json else event_text(line, show_usage=show_usage), flush=True)
            return

        payload = parse_event(line)
        text = event_text(line, show_usage=False)
        usage = usage_display(payload) or ""
        if show_usage and usage:
            text = f"{text}  [{usage}]"

        with self.lock:
            self.latest = text
            self.latest_usage = usage
            self.recent.append(text)
            self.recent = self.recent[-8:]
            self.status = "Playing"
        self.redraw()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=0.6)
        if self.enabled:
            self.redraw(final=True)
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _run(self) -> None:
        while not self.stop_event.wait(0.5):
            self.redraw()

    def redraw(self, final: bool = False) -> None:
        if not self.enabled:
            return

        with self.draw_lock:
            self._redraw_locked(final=final)

    def _redraw_locked(self, final: bool = False) -> None:
        with self.lock:
            status = "Stopped" if final else self.status
            latest = self.latest or "Waiting for subtitles..."
            latest_usage = self.latest_usage
            recent = list(self.recent)

        elapsed = max(0.0, time.monotonic() - self.started_at)
        duration = self.chapter.duration
        progress = (elapsed / duration) if duration and duration > 0 else None
        elapsed_display = fmt_time(elapsed)
        duration_display = fmt_time(duration)
        bar = progress_bar(progress)
        chapter_title = self.chapter.title if self.chapter.index else "Whole file"

        sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")
        sys.stdout.write("AISubtitle terminal player\n")
        sys.stdout.write(f"{status} · {self.media.name}\n")
        sys.stdout.write(f"{chapter_title}\n")
        sys.stdout.write(f"{bar} {elapsed_display} / {duration_display}\n")
        if latest_usage:
            sys.stdout.write(f"usage: {latest_usage}\n")
        sys.stdout.write("\n")
        sys.stdout.write(latest + "\n")
        if recent:
            sys.stdout.write("\nRecent\n")
            for item in recent[-6:]:
                sys.stdout.write(f"  {item}\n")
        sys.stdout.write("\nCtrl-C stops playback.\n")
        if final:
            sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


class OverlayClient:
    def __init__(self, args: argparse.Namespace, media: Path, chapter: Chapter):
        self.args = args
        self.media = media
        self.chapter = chapter
        self.process: subprocess.Popen[bytes] | None = None
        self.socket: socket.socket | None = None

    def start(self) -> None:
        if self.args.no_floating_window:
            return

        if self.connect_existing_window():
            self.send_status(f"Now playing: {self.media.name}")
            return

        self.start_helper()
        self.send_status(f"Now playing: {self.media.name}")

    def connect_existing_window(self) -> bool:
        socket_path = str(Path(self.args.overlay_socket).expanduser())
        if not os.path.exists(socket_path):
            return False

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.5)
            client.connect(socket_path)
            client.settimeout(None)
            self.socket = client
            return True
        except OSError:
            self.socket = None
            return False

    def start_helper(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        try:
            overlay_bin = require_executable(self.args.overlay_bin, "AISubtitle overlay")
            self.process = subprocess.Popen(
                [overlay_bin, "--overlay-stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT),
            )
            pipe_stderr(self.process, "overlay")
        except Exception as error:
            print(f"overlay unavailable: {error}", file=sys.stderr, flush=True)
            self.process = None

    def send_status(self, status: str) -> None:
        self.send_payload({"type": "status", "status": status})

    def send_line(self, line: str) -> None:
        payload = parse_event(line)
        if "text" not in payload and "translation" not in payload and "result" not in payload:
            payload["text"] = line
        payload["type"] = "subtitle"
        payload["status"] = "Playing"
        payload["media_title"] = self.media.name
        payload["source"] = self.source_label()
        self.send_payload(payload)

    def close(self) -> None:
        if self.socket is not None:
            try:
                data = (json.dumps({"type": "status", "status": "Stopped"}, ensure_ascii=False) + "\n").encode("utf-8")
                self.socket.sendall(data)
            except Exception:
                pass
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None

        if self.process is None:
            return
        try:
            self.send_status("Stopped")
        except Exception:
            pass
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            terminate(self.process)
        self.process = None

    def source_label(self) -> str:
        if self.chapter.index:
            return f"{self.media.name} · {self.chapter.title}"
        return self.media.name

    def send_payload(self, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        if self.socket is not None:
            try:
                self.socket.sendall(data)
                return
            except OSError:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.socket = None
                self.start_helper()

        if self.process is None or self.process.stdin is None:
            return
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.process = None


def forward_asr_to_translator(
    asr_stdout: BinaryIO,
    translator_stdin: BinaryIO,
    media: Path,
    chapter: Chapter,
    direct_chinese: bool,
) -> None:
    sequence = 0
    for raw in iter(asr_stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        payload = parse_event(line)
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        sequence += 1
        payload["id"] = sequence
        payload["context_source"] = "media"
        payload["context_url"] = media.as_uri()
        payload["context_title"] = chapter.title
        if direct_chinese and should_direct_chinese(payload):
            payload["direct"] = True
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            translator_stdin.write(data)
            translator_stdin.flush()
        except BrokenPipeError:
            break
    try:
        translator_stdin.close()
    except OSError:
        pass


def print_stream(
    stream: BinaryIO,
    raw_json: bool,
    show_usage: bool,
    display: NowPlayingUI,
    overlay: OverlayClient,
) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        overlay.send_line(line)
        display.emit_line(line, raw_json=raw_json, show_usage=show_usage)


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_pipeline(args: argparse.Namespace, media: Path, chapter: Chapter) -> int:
    ffmpeg = require_executable(args.ffmpeg, "ffmpeg")
    require_directory(args.model_dir, "model directory")
    require_executable(args.asr_bin, "qwen3-asr-stdin")
    translator = "" if args.no_translate else require_executable(args.translator, "translator")
    if not args.no_play:
        ffplay = require_executable(args.ffplay, "ffplay")
    else:
        ffplay = ""

    display = NowPlayingUI(
        media=media,
        chapter=chapter,
        enabled=sys.stdout.isatty() and not args.plain and not args.json,
    )
    overlay = OverlayClient(args=args, media=media, chapter=chapter)

    ffplay_process: subprocess.Popen[bytes] | None = None
    ffmpeg_process: subprocess.Popen[bytes] | None = None
    asr_process: subprocess.Popen[bytes] | None = None
    translator_process: subprocess.Popen[bytes] | None = None

    interrupted = False

    def handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        terminate(translator_process)
        terminate(asr_process)
        terminate(ffmpeg_process)
        terminate(ffplay_process)

    old_int = signal.signal(signal.SIGINT, handle_signal)
    old_term = signal.signal(signal.SIGTERM, handle_signal)

    try:
        display.start()
        overlay.start()
        display.set_status("Starting audio")

        if not args.no_play:
            ffplay_process = subprocess.Popen(
                ffplay_command(ffplay, media, chapter),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            pipe_stderr(ffplay_process, "ffplay")
            time.sleep(0.15)

        display.set_status("Starting ASR")
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command(ffmpeg, media, chapter),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        pipe_stderr(ffmpeg_process, "ffmpeg")

        if ffmpeg_process.stdout is None:
            raise RuntimeError("ffmpeg stdout unavailable")
        asr_process = subprocess.Popen(
            asr_command(args),
            stdin=ffmpeg_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )
        ffmpeg_process.stdout.close()
        pipe_stderr(asr_process, "asr")

        if asr_process.stdout is None:
            raise RuntimeError("ASR stdout unavailable")

        if args.no_translate:
            display.set_status("Transcribing")
            overlay.send_status(f"Transcribing: {media.name}")
            print_stream(
                asr_process.stdout,
                raw_json=args.json,
                show_usage=False,
                display=display,
                overlay=overlay,
            )
        else:
            display.set_status("Starting translator")
            translator_process = subprocess.Popen(
                [translator],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT),
            )
            pipe_stderr(translator_process, "translator")
            if translator_process.stdin is None or translator_process.stdout is None:
                raise RuntimeError("translator pipe unavailable")

            forward_thread = threading.Thread(
                target=forward_asr_to_translator,
                args=(
                    asr_process.stdout,
                    translator_process.stdin,
                    media,
                    chapter,
                    not args.no_direct_chinese,
                ),
                daemon=True,
            )
            forward_thread.start()
            display.set_status("Playing")
            overlay.send_status(f"Playing: {media.name}")
            print_stream(
                translator_process.stdout,
                raw_json=args.json,
                show_usage=args.show_usage,
                display=display,
                overlay=overlay,
            )
            forward_thread.join(timeout=1.0)

        statuses = []
        for process in (translator_process, asr_process, ffmpeg_process):
            if process is not None:
                statuses.append(process.wait())
        if ffplay_process is not None:
            if ffplay_process.poll() is None:
                terminate(ffplay_process)
            else:
                ffplay_process.wait()

        if interrupted:
            return 130
        for status in statuses:
            if status not in (0, None):
                return int(status)
        return 0
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        display.stop()
        overlay.close()
        terminate(translator_process)
        terminate(asr_process)
        terminate(ffmpeg_process)
        terminate(ffplay_process)


def main() -> int:
    args = parse_args()
    media = resolve_media(args)
    if media is None:
        return 0

    ffprobe = require_executable(args.ffprobe, "ffprobe")
    chapters, duration = read_chapters(ffprobe, media)
    if args.list_chapters:
        print_chapters(chapters, duration)
        return 0

    chapter = choose_chapter(args, chapters, duration)
    return run_pipeline(args, media, chapter)


if __name__ == "__main__":
    raise SystemExit(main())
