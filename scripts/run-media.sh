#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-module-cache"
mkdir -p "$CLANG_MODULE_CACHE_PATH"

swift build --product qwen3-asr-stdin
swift build --product aisubtitle

METALLIB_SOURCE="${MLX_METALLIB_SOURCE:-}"
if [[ -z "$METALLIB_SOURCE" ]]; then
  METALLIB_SOURCE="$(
    find "$HOME/Library/Developer/Xcode/DerivedData" \
      -path '*mlx-swift_Cmlx.bundle/Contents/Resources/default.metallib' \
      -print -quit 2>/dev/null || true
  )"
fi

if [[ -n "$METALLIB_SOURCE" ]]; then
  cp "$METALLIB_SOURCE" .build/debug/mlx.metallib
else
  echo "Missing MLX default.metallib. Set MLX_METALLIB_SOURCE to a built default.metallib." >&2
  exit 1
fi

exec /usr/bin/python3 "$PWD/scripts/aisubtitle-media.py" "$@"
