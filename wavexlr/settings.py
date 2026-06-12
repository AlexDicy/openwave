"""App settings, persisted to ~/.config/openwave/settings.json."""

import json
import os

CONFIG_PATH = os.path.expanduser("~/.config/openwave/settings.json")


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def get(key, default=None):
    return load().get(key, default)


def set(key, value):
    settings = load()
    settings[key] = value
    _atomic_write(CONFIG_PATH, settings)
