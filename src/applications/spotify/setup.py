"""Self-serve, on-device setup flow.

Two steps: (1) pick a WiFi network and type its password on the on-screen
keyboard, (2) link Spotify by opening the device's setup page on a phone and
approving access. The result (WiFi creds + Spotify refresh token) is saved to
/config.json on flash, then the device reboots into the normal Spotify app.
"""

import time
import network
import uasyncio as asyncio

from touch import Button
from base import BaseApp
from applications.spotify.keyboard import Keyboard
from applications.spotify.setup_server import SetupServer
from applications.spotify.config_store import save_user_config

PORT = 80  # no privilege model on-device, so use the default http port (no :port to type)
MANUAL = "__manual__"
RESCAN = "__rescan__"


class SetupApp(BaseApp):
    """Runs the setup screens. `await app.run()` ends by rebooting the device."""

    def __init__(self, client_id, existing_config=None):
        super().__init__(full_res=True, layers=1)
        self.toggle_leds(False)
        self.client_id = client_id
        self.config = dict(existing_config or {})
        self.display.set_font("sans")

        cp = self.display.create_pen
        self.btn_bg = cp(45, 45, 45)
        self.accent = cp(28, 140, 64)
        self.url_pen = cp(80, 200, 255)

    # --- shared UI helpers ------------------------------------------------
    def _status(self, *lines):
        self.clear()
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)
        y = self.center_y - (len(lines) - 1) * 26
        for line in lines:
            tw = self.display.measure_text(line, scale=0.9)
            self.display.text(line, (self.width - tw) // 2, y, scale=0.9)
            y += 52
        self.presto.update()

    def _draw_button(self, x, y, w, h, label, bg, scale=0.8):
        self.display.set_pen(bg)
        self.display.rectangle(x, y, w, h)
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)
        tw = self.display.measure_text(label, scale=scale)
        self.display.text(label, x + (w - tw) // 2, y + (h + int(16 * scale)) // 2, scale=scale)

    async def _pick(self, buttons):
        """Edge-triggered: waits for one tap, returns the tapped button's key."""
        was_touching = False
        while True:
            self.touch.poll()
            touching = self.touch.state
            if touching and not was_touching:
                for key, btn in buttons:
                    if btn.is_pressed():
                        return key
            was_touching = touching
            await asyncio.sleep_ms(10)

    # --- WiFi step --------------------------------------------------------
    def _scan(self):
        """Returns SSIDs seen, strongest first (deduped, named only)."""
        try:
            wlan = network.WLAN(network.STA_IF)
            wlan.active(True)
            seen = {}
            for net in wlan.scan():
                ssid = net[0].decode().strip() if isinstance(net[0], (bytes, bytearray)) else str(net[0])
                rssi = net[3]
                if ssid and (ssid not in seen or rssi > seen[ssid]):
                    seen[ssid] = rssi
            return sorted(seen, key=lambda s: seen[s], reverse=True)
        except Exception as e:
            print("WiFi scan failed:", e)
            return []

    async def _choose_ssid(self):
        self._status("Scanning for WiFi...")
        ssids = self._scan()[:5]

        self.clear()
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)
        self.display.text("Choose your WiFi", 90, 44, scale=0.95)

        buttons = []
        y = 90
        for ssid in ssids:
            label = ssid if len(ssid) <= 24 else ssid[:24] + "..."
            self._draw_button(12, y, self.width - 24, 54, label, self.btn_bg, scale=0.85)
            buttons.append((ssid, Button(12, y, self.width - 24, 54)))
            y += 60

        half = (self.width - 30) // 2
        self._draw_button(12, 404, half, 60, "Enter manually", self.accent, scale=0.75)
        self._draw_button(self.width - 12 - half, 404, half, 60, "Rescan", self.btn_bg, scale=0.8)
        buttons.append((MANUAL, Button(12, 404, half, 60)))
        buttons.append((RESCAN, Button(self.width - 12 - half, 404, half, 60)))
        self.presto.update()

        return await self._pick(buttons)

    async def _wifi_step(self):
        """Loops until a WiFi connection succeeds; returns (ssid, password)."""
        while True:
            choice = await self._choose_ssid()
            if choice == RESCAN:
                continue
            if choice == MANUAL:
                ssid = await Keyboard(self, title="Network name (SSID)", mask=False).run()
                if not ssid:
                    continue
            else:
                ssid = choice
            password = await Keyboard(self, title="Password for " + ssid, mask=True).run()
            if password is None:
                continue
            if await self._connect(ssid, password):
                return ssid, password
            self._status("Couldn't connect to", ssid, "Tap to try again")
            await self._wait_tap()

    async def _connect(self, ssid, password, timeout=20):
        # Use the raw STA interface (already active from scanning) rather than
        # presto.connect(): EzWiFi blocks internally, which would freeze this
        # cooperative loop on the "Connecting..." screen with no timeout.
        self._status("Connecting to", ssid + "...")
        wlan = network.WLAN(network.STA_IF)
        try:
            wlan.active(True)
            try:
                wlan.disconnect()  # clear any half-open association before retrying
            except Exception:
                pass
            wlan.connect(ssid, password)
        except Exception as e:
            print("connect() error:", e)
            return False
        t0 = time.time()
        while not wlan.isconnected():
            if time.time() - t0 > timeout:
                return False
            await asyncio.sleep_ms(300)
        return True

    async def _wait_tap(self):
        was_touching = False
        while True:
            self.touch.poll()
            touching = self.touch.state
            if touching and not was_touching:
                return
            was_touching = touching
            await asyncio.sleep_ms(10)

    # --- Spotify OAuth step ----------------------------------------------
    def _show_url(self, ip):
        self.clear()
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)

        def centered(text, y, scale, pen):
            self.display.set_pen(pen)
            tw = self.display.measure_text(text, scale=scale)
            self.display.text(text, (self.width - tw) // 2, y, scale=scale)

        url = "http://" + ip + ("" if PORT == 80 else ":" + str(PORT))
        centered("Almost done!", 110, 1.0, self.colors.WHITE)
        centered("On your phone, open:", 180, 0.8, self.colors.GRAY)
        centered(url, 244, 1.1, self.url_pen)
        centered("then tap", 322, 0.8, self.colors.GRAY)
        centered("Connect Spotify", 364, 0.9, self.colors.WHITE)
        self.presto.update()

    async def _oauth_step(self):
        ip = network.WLAN(network.STA_IF).ifconfig()[0]
        server = SetupServer(self.client_id, ip, PORT)
        await server.start()
        self._show_url(ip)

        last_error = None
        while not server.done:
            if server.error and server.error != last_error:
                last_error = server.error
                print("OAuth error:", server.error)
            await asyncio.sleep_ms(200)

        self.config["refresh_token"] = server.tokens["refresh_token"]
        if "device_id" not in self.config:
            self.config["device_id"] = None
        save_user_config(self.config)
        await server.stop()

    # --- orchestration ----------------------------------------------------
    async def run(self):
        if not self.client_id:
            while True:  # owner forgot to provision the app identity
                self._status("Setup not provisioned", "Missing SPOTIFY_CLIENT_ID")
                await asyncio.sleep(5)

        ssid = self.config.get("wifi_ssid")
        password = self.config.get("wifi_password")
        if not (ssid and await self._connect(ssid, password)):
            ssid, password = await self._wifi_step()
            self.config["wifi_ssid"] = ssid
            self.config["wifi_password"] = password
            save_user_config(self.config)

        await self._oauth_step()

        self._status("All set!", "Restarting...")
        await asyncio.sleep(2)
        import machine
        machine.reset()
