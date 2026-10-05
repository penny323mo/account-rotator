#!/bin/zsh
# dist/Account-Rotator-<version>.dmg: the app plus an Applications shortcut to drag it onto.
set -euo pipefail
ROOT=${0:A:h:h}
"$ROOT/scripts/build.sh"
APP="$ROOT/dist/Account Rotator.app"
VERSION=$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Contents/Info.plist")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/Account Rotator.app"
ln -s /Applications "$STAGE/Applications"
DMG="$ROOT/dist/Account-Rotator-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "Account Rotator" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG" >/dev/null
echo "$DMG"
