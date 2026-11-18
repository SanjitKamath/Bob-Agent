#!/usr/bin/env bash
# Packs release/mac-*/Bob.app into a drag-to-Applications disk image.
# Run through `npm run dmg`, which builds and ad-hoc signs Bob.app first.
set -euo pipefail
cd "$(dirname "$0")/.."

RELEASE="${BOB_RELEASE:-release}"
APP=$(ls -d "$RELEASE"/mac*/Bob.app 2>/dev/null | head -n 1 || true)
if [ -z "$APP" ]; then
  echo "No Bob.app under $RELEASE/. Run: npm run dist (or dist:share)" >&2
  exit 1
fi
if ! codesign --verify --deep --strict "$APP" 2>/dev/null; then
  echo "$APP is not signed. Run: npm run dist" >&2
  exit 1
fi

VERSION=$(node -p "require('./package.json').version")
ARCH=$(uname -m)
OUT="$RELEASE/${BOB_DMG_NAME:-Bob}-${VERSION}-${ARCH}.dmg"

STAGE=$(mktemp -d "${TMPDIR:-/tmp}/bob-dmg.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
# ditto keeps the bundle's signature, symlinks and extended attributes intact.
ditto "$APP" "$STAGE/Bob.app"
ln -s /Applications "$STAGE/Applications"

rm -f "$OUT"
hdiutil create -volname "Bob" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
hdiutil verify "$OUT" >/dev/null
echo "Built $OUT"
echo "Open it and drag Bob into Applications."
