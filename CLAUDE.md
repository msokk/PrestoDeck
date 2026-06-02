# CLAUDE.md

PrestoDeck — a Spotify controller for the **Pimoroni Presto** (RP2350), written in **MicroPython**. App code lives in `src/`; entry point is `src/main.py` → `applications/spotify/spotify.py`.

## Environment & constraints

- Target firmware: MicroPython 1.26 (`_machine='Presto'`), 480×480 display (`full_res=True`).
- **No `_thread`**: the Presto firmware reserves core 1 for the display. Concurrency is **single-core cooperative `asyncio`** only — never reach for threads/multicore.
- **Cannot run on the dev machine** (hardware MicroPython). Only on-device testing is real.
- Local check is syntax-only: `python3 -m py_compile src/applications/spotify/*.py` (firmware modules `jpegdec`/`pngdec`/`presto`/`touch`/`tls` won't import here). It writes `__pycache__/` — delete it after (`rm -rf src/applications/spotify/__pycache__`).

## On-device workflow (mpremote)

- `mpremote mount src run main.py` — run live from local `src/` (fastest iteration; prints go to the terminal).
- `mpremote cp src/applications/spotify/spotify.py :applications/spotify/spotify.py` — push one file (device FS = contents of `src/` at root).
- `mpremote reset && mpremote repl` — reboot and watch serial output (the app `print()`s state/errors).

## Secrets & on-device setup

- **Two storage tiers.** Provisioned-by-owner: only `SPOTIFY_CLIENT_ID` (the shared Spotify app identity) lives in the **gitignored** `secrets` source (`src/secrets.py` or `/sd/secrets.json`; `*.example` are templates) — **never commit it**, avoid `git add -A` / `git commit -a`. User-configured: WiFi creds + Spotify `refresh_token` (plus the settings-screen prefs `backlight`/`control_sounds`) are written to **`/config.json` on internal flash** (`config_store.py`) — this is the tier the reset wipes. Settings writes go through `_save_settings` (load-modify-save the whole dict) so they never clobber the WiFi/token keys.
- **Self-serve setup flow** (`setup.py`, `setup_server.py`, `keyboard.py`): on first boot (no usable `/config.json`), `launch()` runs `SetupApp` instead of `Spotify`. Step 1 — scan WiFi, pick SSID, type password on the on-screen keyboard (masked field has a Show/Hide eye, visible by default). Step 2 — the device runs an HTTP server (`asyncio.start_server`, port 8080) and shows its `http://<ip>:8080` URL; the user opens it on a phone and taps "Connect Spotify". (mDNS/`prestodeck.local` was dropped — unreliable on RP2; the shown IP is used.)
- **PKCE token rotation must persist:** Spotify rotates the refresh token on every refresh and revokes the old one. `Session(on_credentials_changed=...)` writes the rotated token back to `/config.json` (`_persist_refresh_token` in `spotify.py`), else auth dies on the next reboot. `/callback` is idempotent — replaying a single-use auth code makes Spotify revoke the issued tokens.
- **OAuth is PKCE, no client secret.** Spotify only allows `http://` redirect URIs for the literal loopback `127.0.0.1`, so the device can't be the registered redirect. A static GitHub Pages helper (`docs/cb/index.html`, served at `HELPER_REDIRECT` in `setup_server.py`) is registered instead; it bounces the phone back to `http://<ip>:8080/callback` via a **top-level navigation** (exempt from mixed-content rules), carrying the device `ip:port` in the OAuth `state`. Register that helper URL as the Redirect URI in the Spotify app; allowlist friends' emails (Dev Mode = 25 users).
- **Settings screen:** the top-right cog (`settings.png`, replaces the old Light button) opens it (`state.show_settings`; rendered by `draw_settings`, its own widget set `settings_buttons` driven by the same touch loop). Holds vector toggles for backlight (ambient LEDs) and control sounds (the piezo press-buzz, default **off** — gates the `_buzz` task), both persisted via `_save_settings`. Back chevron (top-right) returns; on exit the cover cache is invalidated so the album art redraws.
- **Reset:** the red **Reset setup** button on the settings screen (tap → "Tap again to reset" within ~4s, `state.reset_pending`/`_reset_until`) → `_do_reset()` = `clear_user_config()` + `machine.reset()` back into setup. The `client_id` survives. (Replaced the old 10s Light-button hold.) **Offline escape hatch:** the settings reset is unreachable if WiFi never connects (the app loop hasn't started), so `Spotify.__init__` connects via the raw STA interface with a bounded timeout (`_connect_wifi`, not blocking `presto.connect()`) and, on each failed attempt, offers a synchronous **hold-screen-3s-to-reset** (`_held_for_reset`, red LED ramp). Legacy/no-`/config.json` boots keep the old EzWiFi retry (a reset there would wipe nothing).
- Legacy `SPOTIFY_CREDENTIALS` (full dict from `adhoc/generate_token.sh`) in secrets still boots directly, bypassing setup.
- Remotes: `origin` = personal fork (msokk), `upstream` = fatihak/PrestoDeck.

## Architecture (spotify.py)

- One asyncio loop, three tasks: `touch_handler_loop`, `display_loop`, `network_loop`.
- **All** network I/O runs in `network_loop`; UI tasks must never block on network.
- `spotify_client.py` is a custom **async HTTP/TLS client** (`Connection`, keep-alive to `api.spotify.com`) — not `urequests`.
- **Optimistic UI**: button handlers flip local `state` + enqueue a command; `network_loop` reconciles via polling. Next/Back use a prefetched queue track + cached prev track.
- Cross-task handoff via the `Shared` object — no locks needed (cooperative: each mutation is a single statement with no `await` in between).
- Touch is **level-triggered** in firmware; the handler edge-triggers (`was_touching`) so one tap = one action.

## Gotchas

- Spotify PUT/POST need `Content-Length: 0` even with no body, else **HTTP 411**.
- Player commands return **404 `NO_ACTIVE_DEVICE`** when nothing is active; `current_playing` is eventually-consistent after a skip and returns 204 when idle.
- TLS: use `CERT_NONE` (no CA bundle/RTC on device) and pass `server_hostname` for SNI.
- PicoGraphics **vector fonts** (`"sans"`) are positioned by the **baseline**, not top-left (bitmap fonts use top-left). Use `display.measure_text(text, scale)` to center horizontally.
- `jpegdec` only scales by **powers of two** (FULL/HALF/QUARTER/EIGHTH) — no arbitrary resize. Cover art is Spotify's 640px decoded at HALF (~320px), centered.
- Two display layers: layer 0 = cover art, layer 1 = controls/text. `presto.update()` only flushes on a render — force one when a new cover decodes, else it stays in the buffer.
- Setup/OAuth relies on firmware modules `hashlib.sha256` (PKCE needs S256; Spotify rejects `plain`), `os.urandom`, `binascii`, `asyncio.start_server`, and `network.WLAN(STA_IF).scan()` — all verified present on the Presto 1.26 firmware. Setup connects via the **raw `network.WLAN` STA interface**, not `presto.connect()` (EzWiFi blocks the cooperative loop and can't be interrupted).
