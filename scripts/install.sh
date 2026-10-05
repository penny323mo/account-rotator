#!/bin/zsh
set -euo pipefail
# Another copy of the rotator (an older install) must not run next to this one: both use port 3082 and the same
# Keychain items.
if lsof -nP -iTCP:3082 -sTCP:LISTEN 2>/dev/null | grep -q . && ! launchctl print "gui/$(id -u)/io.account-rotator.web" >/dev/null 2>&1; then
  echo 'Port 3082 is already used by another program (another Account Rotator?). Stop it first.' >&2; exit 1
fi
ROOT=${0:A:h:h}
"$ROOT/scripts/build.sh"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents" "$HOME/.agy-rotator"
chmod 700 "$HOME/.agy-rotator"
python3 - "$ROOT" <<'PY'
import os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
allowed = {str(Path.home() / f'Applications/{name}.app/Contents/MacOS/AGYRotator')
           for name in ('Account Rotator',)} | {
          str(root / f'dist/{name}.app/Contents/MacOS/AGYRotator') for name in ('Account Rotator',)}
pids = subprocess.run(['pgrep', '-x', 'AGYRotator'], capture_output=True, text=True).stdout.split()
for value in pids:
    pid = int(value)
    name = subprocess.run(['ps', '-p', value, '-o', 'comm='], capture_output=True, text=True).stdout.strip()
    if name in allowed:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            try: os.kill(pid, 0)
            except ProcessLookupError: break
            time.sleep(.1)
        else: raise SystemExit('Account Rotator app did not exit; installation stopped')
PY
ditto "$ROOT/dist/Account Rotator.app" "$HOME/Applications/Account Rotator.app"
python3 - "$ROOT" <<'PY'
import os, plistlib, sys
from pathlib import Path
root = Path(sys.argv[1])
home = Path.home()
plist = {
    'Label': 'io.account-rotator.daemon',
    'ProgramArguments': [sys.executable, str(root / 'agy-rotator'), 'daemon'],
    'WorkingDirectory': str(root),
    'RunAtLoad': True,
    'KeepAlive': True,
    'ThrottleInterval': 10,
    'StandardOutPath': str(home / '.agy-rotator/daemon.log'),
    'StandardErrorPath': str(home / '.agy-rotator/daemon-error.log'),
    'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
    'Umask': 0o077,
}
web = {
    'Label': 'io.account-rotator.web',
    'ProgramArguments': [sys.executable, str(root / 'web/server.py')],  # 127.0.0.1:3082 only
    'WorkingDirectory': str(root),
    'RunAtLoad': True,
    'KeepAlive': True,
    'ThrottleInterval': 10,
    'StandardOutPath': str(home / '.agy-rotator/web.log'),
    'StandardErrorPath': str(home / '.agy-rotator/web-error.log'),
    'Umask': 0o077,
}
for label, doc in (('io.account-rotator.daemon', plist), ('io.account-rotator.web', web)):
    path = home / f'Library/LaunchAgents/{label}.plist'
    with path.open('wb') as f:
        plistlib.dump(doc, f)
    os.chmod(path, 0o600)
PY
DOMAIN="gui/$(id -u)"
for LABEL in io.account-rotator.daemon io.account-rotator.web; do
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$LABEL.plist"
done
open "$HOME/Applications/Account Rotator.app"
