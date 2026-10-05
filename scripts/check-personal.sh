#!/bin/zsh
# Fails when a tracked file holds something personal: a real mailbox address, a home folder path, or a token.
set -uo pipefail
cd "${0:A:h:h}"
pattern='[A-Za-z0-9._%+-]+@(gmail|hotmail|outlook|icloud|yahoo|proton)\.|/Users/[a-z0-9]{3,}/|github_pat_|sk-ant-|ya29\.'
if git grep -nIE "$pattern" -- . ':!scripts/check-personal.sh'; then
  echo 'Personal data found (above). Remove it before committing.' >&2
  exit 1
fi
echo 'No personal data found.'
