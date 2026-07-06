# AISubtitle

macOS floating subtitle pipeline:

1. Capture Helium audio with ScreenCaptureKit.
2. Stream 16 kHz mono signed 16-bit PCM into local native Qwen3-ASR 1.7B.
3. Send non-Chinese ASR segments to OpenRouter Gemini 3.1 Flash Lite for Traditional Chinese translation.
4. Skip Chinese ASR segments by default; when macOS output volume is muted or zero, send them through direct OpenCC post-processing and bypass the LLM.
5. Show translated or direct-processed text in an always-on-top floating window.

When Helium allows Apple Events JavaScript, AISubtitle scans Helium tabs for the one that is actually playing audio/video. The sounded tab URL becomes the translation session key, and recent same-URL subtitle turns are sent to Gemini as context. If that Helium setting is disabled, AISubtitle falls back to the active tab URL.

## Real Run

Helium must be running. The first capture run will need macOS Screen Recording permission for the terminal or packaged app that launches this.

For precise sounded-tab context, enable Helium's `View > Developer > Allow JavaScript from Apple Events`, and allow AISubtitle to control Helium when macOS asks.

```bash
./scripts/run-real.sh
```

For the normal macOS GUI launch path:

```bash
./scripts/deploy-app.sh
open -n /Applications/AISubtitle.app
```

## Terminal Media Run

For cmux/terminal use, play a local video as audio-only and stream subtitles in the terminal:

```bash
./scripts/run-media.sh
./scripts/run-media.sh "/path/to/video.mp4"
```

With no media path, AISubtitle scans `~/Downloads` for videos and shows an interactive picker. Use ↑/↓ to select, Enter to start, or `q` to cancel. If the selected media file has chapters, the terminal then shows the same picker for chapters. The selected media/chapter is played with `ffplay -nodisp` and transcribed through the same Qwen3-ASR stdin contract.

During playback, the terminal switches to a now-playing view with progress, current subtitle, and recent subtitle lines. Subtitles are also mirrored to the AISubtitle floating window. If a floating window already exists, the terminal player connects to it and takes over that window; otherwise it starts a lightweight `aisubtitle --overlay-stdin` helper.

Useful options:

```bash
./scripts/run-media.sh --list-videos
./scripts/run-media.sh --media-dir "$HOME/Movies"
./scripts/run-media.sh --list-chapters "/path/to/video.mp4"
./scripts/run-media.sh --chapter 3 "/path/to/video.mp4"
./scripts/run-media.sh --chapter "Intro" "/path/to/video.mp4"
./scripts/run-media.sh --all "/path/to/video.mp4"
./scripts/run-media.sh --no-play --no-translate "/path/to/video.mp4"
./scripts/run-media.sh --plain "/path/to/video.mp4"
./scripts/run-media.sh --no-floating-window "/path/to/video.mp4"
./scripts/run-media.sh --json "/path/to/video.mp4"
```

Chinese ASR output is sent through translator direct mode by default, so OpenCC handles it without spending LLM calls. Use `--no-direct-chinese` only when you explicitly want Chinese text to go through the LLM translator.

`scripts/run-real.sh`, `scripts/build-app-bundle.sh`, and `scripts/deploy-app.sh` build both products first:

- `.build/debug/qwen3-asr-stdin`
- `.build/debug/aisubtitle`

If `config.json` is absent, the app uses real local defaults:

- Helium bundle id: `net.imput.helium`
- Qwen model: `/Users/jianruicheng/Library/Application Support/com.jasonchien.Voco/Qwen3Models/mlx-community_Qwen3-ASR-1.7B-8bit`
- Translator: `scripts/codex-translate-lines.sh`, backed by OpenRouter model `google/gemini-3.1-flash-lite`

Store API keys in `.secret`:

```bash
OPENROUTER_API_KEY=...
CLINE_API_KEY=...
```

## Config

Copy and edit only when the defaults are wrong:

```bash
cp config.example.json config.json
./scripts/run-real.sh --config config.json
```

ASR command contract:

- stdin: raw PCM, signed 16-bit little-endian, mono, 16 kHz.
- stdout: JSONL transcript events.

```json
{"text":"hello world","language":"English","is_final":true}
```

Translator command contract:

- stdin: JSONL transcript events.
- stdout: JSONL translation events.
- Chinese direct mode: set `"direct": true`; the translator bypasses the LLM, applies OpenCC `s2twp`, and reports usage as `Direct`.

```json
{"text":"哈囉世界"}
```
