#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

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

if [[ -z "$METALLIB_SOURCE" ]]; then
  echo "Missing MLX default.metallib. Set MLX_METALLIB_SOURCE to a built default.metallib." >&2
  exit 1
fi

cp "$METALLIB_SOURCE" .build/debug/mlx.metallib

APP_DIR="$PWD/dist/AISubtitle.app"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cp .build/debug/aisubtitle "$APP_DIR/Contents/MacOS/AISubtitle"
chmod +x "$APP_DIR/Contents/MacOS/AISubtitle"

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>AISubtitle</string>
  <key>CFBundleIdentifier</key>
  <string>com.jasonchien.AISubtitle</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>AISubtitle</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>14.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

echo "$APP_DIR"
