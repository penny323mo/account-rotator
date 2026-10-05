"""Where the account helpers live: <repo>/helpers next to daemon/, or $ACCOUNT_ROTATOR_HELPERS (a folder)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def helper(name):
    return Path(os.environ.get('ACCOUNT_ROTATOR_HELPERS') or ROOT / 'helpers') / name
