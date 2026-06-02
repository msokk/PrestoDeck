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

## Secrets

- `src/secrets.py` is **gitignored**; `src/secrets.py.example` is the template. It holds real WiFi/Spotify credentials — **never commit it**; avoid `git add -A` / `git commit -a`.
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
