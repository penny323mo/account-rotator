"""Keep agy's model one the live account can use.

Not every account offers every Gemini model (2026-11-26: some accounts listed Gemini 4 Neon / Carbon / Argon, others
only Gemini 4 Hydrogen and Gemini 3.8 Flash, and agy refused the missing ones with exit 1). agy starts every session on
the model in its settings.json, so after a switch onto an account without that model, the setting becomes Gemini 4
Hydrogen at the same effort; once the live account offers the chosen model again, it is put back. A model picked by
hand in the meantime becomes the chosen one.

The accounts' models come from Google's quota summary ("Models within this group: Gemini 4 Hydrogen, ..."); when they
are unknown, nothing is changed."""
import json
import os
import re

SETTINGS = os.path.expanduser('~/.gemini/antigravity-cli/settings.json')
FALLBACKS = ('Gemini 4 Hydrogen', 'Gemini 3.8 Flash')
EFFORTS = {'Gemini 3.8 Flash': ('Low', 'Medium', 'High')}  # Hydrogen has every effort (Low .. XHigh)
LABEL = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$')  # "Gemini 4 Carbon (High)" -> base, effort
LISTED = re.compile(r'Models within this group:\s*(.*)', re.S)


def listed_models(description):
    """['Gemini 4 Hydrogen', 'Gemini 3.8 Flash'] from a quota group's description, else []."""
    m = LISTED.search(description or '') if isinstance(description, str) else None
    return [x.strip() for x in m.group(1).split(',') if x.strip()] if m else []


def split(label):
    m = LABEL.match(label or '')
    return (m.group(1).strip(), m.group(2).strip()) if m else ((label or '').strip(), None)


def choose(preferred, models):
    """The model to use on an account offering `models` (bases), or None when nothing fits."""
    base, effort = split(preferred)
    if base in models:
        return preferred
    for fallback in FALLBACKS:
        if fallback in models:
            efforts = EFFORTS.get(fallback)
            if effort and efforts and effort not in efforts:
                effort = 'High'
            return f'{fallback} ({effort})' if effort else fallback
    return None


def _read(path):
    try:
        with open(path) as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, value):
    temp = f'{path}.rotator-tmp'
    with open(temp, 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
    try:
        os.chmod(temp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(temp, path)


def fit(models, state_path, settings_path=SETTINGS):
    """Make agy's model one of `models` (the live account's). Returns (before, after) when the setting changed."""
    if not models:
        return None
    settings = _read(settings_path)
    current = settings.get('model') if settings else None
    if not isinstance(current, str) or not current:
        return None
    state = _read(state_path) or {}
    # What the rotator set last time stands for the model chosen before; anything else was picked by hand
    preferred = state.get('preferred') if state.get('set') == current and state.get('preferred') else current
    want = choose(preferred, models)
    if not want:
        return None
    _write(state_path, {'preferred': preferred, 'set': want})
    if want == current:
        return None
    settings = _read(settings_path)  # agy may have written it meanwhile: change only the model
    if not settings or settings.get('model') != current:
        return None
    settings['model'] = want
    _write(settings_path, settings)
    return current, want
