# AISubtitle

macOS floating subtitle pipeline:

1. Capture Helium audio with ScreenCaptureKit.
2. Stream 16 kHz mono signed 16-bit PCM into local native Qwen3-ASR 1.7B.
3. Send non-Chinese ASR segments to OpenRouter Gemini 3.1 Flash Lite for Traditional Chinese translation.
4. Show translated text in an always-on-top floating window.

## Real Run

Helium must be running. The first capture run will need macOS Screen Recording permission for the terminal or packaged app that launches this.

```bash
./scripts/run-real.sh
```

For the normal macOS GUI launch path:

```bash
./scripts/deploy-app.sh
open -n /Applications/AISubtitle.app
```

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

```json
{"text":"哈囉世界"}
```
