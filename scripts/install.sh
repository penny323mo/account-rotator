#!/bin/zsh
# Install from a checkout: build the app, put it in ~/Applications and open it. The app registers its own login
# services (io.account-rotator.daemon / .web) pointing at the copy of the rotator inside it.
set -euo pipefail
ROOT=${0:A:h:h}
"$ROOT/scripts/build.sh"
mkdir -p "$HOME/Applications"
# Quit a running copy so the new one replaces it.
for pid in $(pgrep -x AGYRotator || true); do
  case "$(ps -p "$pid" -o comm=)" in
    */"Account Rotator.app"/Contents/MacOS/AGYRotator) kill "$pid" ;;
  esac
done
sleep 1
ditto "$ROOT/dist/Account Rotator.app" "$HOME/Applications/Account Rotator.app"
open "$HOME/Applications/Account Rotator.app"
