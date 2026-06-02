# PrestoDeck

<img src="./docs/presto.jpg" />

PrestoDeck is a Spotify music controller for the Pimoroni Presto. It displays the album cover art, name, and artist of the currently playing track and provides basic controls for playback.

## What's new in this fork

This fork reworks the app for a much more responsive UI and adds a self-serve, on-device setup so end users never need a cable or Thonny. All network I/O runs on its own cooperative `asyncio` task using non-blocking TLS sockets, so polling and album-art downloads no longer freeze touch or rendering. Highlights:

- **Self-serve on-device setup** — on first boot the device walks you through joining WiFi (with an on-screen keyboard) and linking Spotify from your phone. No client secret, no `generate_token.sh`, no re-flashing to change accounts. See [Setup](#setup) below.
- **Settings screen** — tap the cog (top-right, while controls are shown) for a dedicated screen with toggles for the **ambient backlight** and **control sounds** (the piezo press-buzz, off by default), plus a **Reset setup** button. Both toggles persist across reboots.
- **Non-blocking networking** — a custom async HTTP/TLS client (`spotify_client.py`) with a keep-alive connection to `api.spotify.com`, replacing blocking `urequests`. The TLS handshake is paid once instead of on every poll.
- **Optimistic controls** — play/pause/shuffle/repeat update the UI instantly and reconcile with the next poll (a short window prevents not-yet-applied polls from reverting them).
- **Instant Next/Back** — the upcoming track is prefetched from the play queue and the previous track is cached, so skips update title *and* cover art immediately; a stale-track suppressor avoids flip-revert-flip during Spotify's propagation delay.
- **Snappy under load** — `gc.collect()` used to run on every loop iteration, but a full scan of the Presto's 8 MB heap takes ~100 ms regardless of garbage; running it ~20×/s pegged the single core and stalled in-flight requests for *seconds*. It now runs only after real work. Combined with disabling WiFi power-save and only refetching the (large) play queue on track change, control latency dropped from multi-second spikes to ~200 ms.
- **Full-bleed cover art** — direct from Spotify's CDN (no `wsrv.nl` proxy hop). Fills the screen, center-cropped, when controls are hidden; shrinks to a bordered view when controls are shown. Track name and artist are always displayed, and a "No song is playing" caption shows when nothing is active.
- **Tactile feedback** — an optional brief piezo buzz on each control press (toggle in Settings).
- **Resilient WiFi** — connects via the raw STA interface with a bounded timeout instead of blocking forever; if it can't reconnect (e.g. router or password changed) you can hold the screen for 3s to wipe config and return to setup.
- **Edge-triggered touch** — one tap = one action (fixes erratic control toggling).

> Note: the Pimoroni Presto firmware reserves the second core for the display, so `_thread` is unavailable — concurrency here is single-core cooperative `asyncio`, not multicore.

## Hardware

- [Pimoroni Presto](https://collabs.shop/xbvgb2)
- (Optional) [Right Angle USB C Cable](https://amzn.to/4jUYJ9F) 

## Setup

Setup has two halves: a **one-time provisioning** by the owner (flash the code and
provision the shared Spotify app identity), and the **self-serve flow** each user
runs on the device itself (join WiFi, link Spotify) — no cable required.

> Prefer the old flow with a pre-generated credentials dict? See
> **[Legacy setup](docs/legacy-setup.md)**.

### Provisioning (owner, once)

**1. Clone the repository**

```bash
git clone https://github.com/<you>/PrestoDeck.git
```

**2. Create a Spotify app (PKCE)**

- Visit [Spotify for Developers](https://developer.spotify.com/dashboard/applications), sign in, and click **Create an App**.
- Tick the **Web API** box. No client secret is needed — the device uses PKCE.
- This fork ships a tiny static OAuth helper page under [`docs/cb/`](docs/cb/), served via **GitHub Pages**. Spotify only allows `http://` redirect URIs for the literal loopback `127.0.0.1`, so the device can't be the redirect target; the helper bounces the phone back to the device instead. Enable Pages for your fork and register that page as the **Redirect URI**, e.g. `https://<you>.github.io/PrestoDeck/cb/`.
- Update `HELPER_REDIRECT` in `src/applications/spotify/setup_server.py` to match your Pages URL.
- Spotify apps start in **Development Mode** (max 25 users) — add each user's Spotify email under the app's **User Management**.

**3. Provision the client id**

Copy `src/secrets.py.example` to `src/secrets.py` and set `SPOTIFY_CLIENT_ID`
to your app's Client ID. (Alternatively put it in a `secrets.json` on a microSD
card — see the template `src/secrets.json.example`; it's read as `/sd/secrets.json`.)
This is the only thing the owner provisions; WiFi and the Spotify refresh token
are entered on-device per user and saved to `/config.json` on flash.

**4. Upload to the Presto**

Connect the Presto over USB-C and copy the contents of `src/` to the device root.
With [Thonny](https://thonny.org/) (interpreter: **MicroPython (Raspberry Pi Pico)**),
upload the project, or with `mpremote`:

```bash
mpremote cp -r src/* :
```

### On-device setup (each user)

On first boot (no usable `/config.json`), the device runs the setup flow:

1. **WiFi** — pick your network from the scanned list and type the password on
   the on-screen keyboard (the field has a show/hide eye).
2. **Link Spotify** — the device shows a URL like `http://<device-ip>`. Open it
   on your phone (same network) and tap **Connect Spotify**, approve access, and
   the device finishes linking and restarts into the player.

That's it — the beats flow. 🎵

### Using it

- Tap anywhere to show/hide the playback controls over the cover art.
- Play/pause, next/previous, shuffle, and repeat are in the border when controls
  are shown.
- The **cog** (top-right) opens **Settings**: toggle the ambient backlight and
  control sounds, or **Reset setup** (tap, then tap again to confirm) to wipe
  WiFi + Spotify and return to the setup flow. If WiFi ever stops connecting,
  **hold the screen for 3 seconds** on the failure screen to reset.

## Additional Resources
- [Pimoroni Presto Github Repo](https://github.com/pimoroni/presto)
- [Getting Started with Pimoroni Presto](https://learn.pimoroni.com/article/getting-started-with-presto)
- [Micropython Spotify Web API](https://github.com/tltx/micropython-spotify-web-api)

## Sponsoring

PrestoDeck is maintained and developed with the help of sponsors. If you enjoy the project or find it useful, consider supporting its continued development.

<p align="center">
<a href="https://github.com/sponsors/fatihak" target="_blank"><img src="https://user-images.githubusercontent.com/345274/133218454-014a4101-b36a-48c6-a1f6-342881974938.png" alt="Become a Patreon" height="35" width="auto"></a>
<a href="https://www.patreon.com/akzdev" target="_blank"><img src="https://c5.patreon.com/external/logo/become_a_patron_button.png" alt="Become a Patreon" height="35" width="auto"></a>
<a href="https://www.buymeacoffee.com/akzdev" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="35" width="auto"></a>
</p>
