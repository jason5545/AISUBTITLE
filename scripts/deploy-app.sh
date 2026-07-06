#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE_APP="$PWD/dist/AISubtitle.app"
TARGET_APP="${AISUBTITLE_DEPLOY_APP:-/Applications/AISubtitle.app}"
SIGN_IDENTITY="${AISUBTITLE_CODESIGN_IDENTITY:-}"

scripts/build-app-bundle.sh >/dev/null

if [[ ! -d "$SOURCE_APP" ]]; then
  echo "Missing built app: $SOURCE_APP" >&2
  exit 1
fi

mkdir -p "$(dirname "$TARGET_APP")"

# Keep the deployed app path stable for macOS TCC. Do not delete/recreate
# /Applications/AISubtitle.app during redeploy; update it in place.
ditto "$SOURCE_APP" "$TARGET_APP"

if [[ -z "$SIGN_IDENTITY" ]]; then
  SIGN_IDENTITY="$(
    security find-identity -v -p codesigning 2>/dev/null \
      | awk -F '"' '/Apple Development: Jui Chen Chien/ { print $2; exit }'
  )"
fi

if [[ -z "$SIGN_IDENTITY" ]]; then
  SIGN_IDENTITY="$(
    security find-identity -v -p codesigning 2>/dev/null \
      | awk -F '"' '/valid identities found/ { next } NF > 1 { print $2; exit }'
  )"
fi

if [[ -z "$SIGN_IDENTITY" ]]; then
  echo "Missing code signing identity. Set AISUBTITLE_CODESIGN_IDENTITY." >&2
  exit 1
fi

codesign --force --deep --timestamp=none --sign "$SIGN_IDENTITY" "$TARGET_APP"
codesign --verify --deep --strict --verbose=2 "$TARGET_APP"

echo "$TARGET_APP"
