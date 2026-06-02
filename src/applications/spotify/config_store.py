"""User-configured settings persisted to internal flash (no SD card needed).

MicroPython's filesystem (LittleFS on the RP2350's flash) is writable, so the
setup flow stores the user's WiFi credentials and the Spotify refresh token here
at /config.json. This is the tier that the 10s long-press reset wipes; the
Spotify client_id stays in the provisioned secrets source (see secrets_loader).
"""

import json

CONFIG_PATH = "/config.json"


def load_user_config():
    """Returns the saved config dict, or None if missing/unreadable/corrupt."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save_user_config(config):
    """Writes the config dict to flash, overwriting any existing file."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f)


def clear_user_config():
    """Removes the saved config so the device boots back into setup."""
    try:
        import uos
        uos.remove(CONFIG_PATH)
    except Exception:
        pass


def is_configured(config):
    """A config is usable only with both WiFi and a Spotify refresh token."""
    return bool(config and config.get("wifi_ssid") and config.get("refresh_token"))
