"""Loads credentials from the SD card, falling back to the bundled secrets.py.

Prefers /sd/secrets.json (same keys as secrets.py: WIFI_SSID, WIFI_PASSWORD,
SPOTIFY_CREDENTIALS) so credentials can live on a removable card instead of being
flashed onto the device. If the card is missing/unreadable or the file isn't there,
it falls back to importing the secrets.py module.
"""

SD_SECRETS_PATH = "/sd/secrets.json"


class _DictSecrets:
    """Attribute view over the parsed JSON so callers can use the same
    `secrets.WIFI_SSID` / `getattr(secrets, ...)` / `hasattr(...)` access as the module."""
    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name)


def _read_sd_secrets():
    """Mounts the SD card (Presto: SPI0 on pins 34/35/36, CS on 39) and parses the
    JSON. Returns the dict, or None if the card/file isn't available."""
    try:
        import uos
        # The mount survives soft resets, so only mount if it isn't already there.
        if "sd" not in uos.listdir("/"):
            import machine
            import sdcard
            spi = machine.SPI(
                0,
                sck=machine.Pin(34, machine.Pin.OUT),
                mosi=machine.Pin(35, machine.Pin.OUT),
                miso=machine.Pin(36, machine.Pin.OUT),
            )
            uos.mount(sdcard.SDCard(spi, machine.Pin(39)), "/sd")

        import json
        with open(SD_SECRETS_PATH) as f:
            data = json.load(f)
        print("Loaded secrets from", SD_SECRETS_PATH)
        return data
    except Exception as e:
        print("SD secrets unavailable ({}); falling back to secrets.py".format(e))
        return None


def load_secrets():
    """Returns a secrets source supporting attribute access: the SD card's
    /sd/secrets.json if present, otherwise the bundled secrets.py module."""
    data = _read_sd_secrets()
    if data is not None:
        return _DictSecrets(data)
    import secrets
    return secrets
