import gc
import time
import network
import jpegdec
import pngdec
import uasyncio as asyncio

from touch import Button
from presto import Buzzer

from applications.spotify.spotify_client import Session, SpotifyWebApiClient, fetch_url
from applications.spotify.secrets_loader import load_secrets
from applications.spotify.config_store import load_user_config, save_user_config, clear_user_config, is_configured
from base import BaseApp

DEBUG = False  # set True to print the [net]/[skip] timing logs over serial

class State:
    """Tracks the current state of the Spotify app including playback and UI controls."""
    def __init__(self):
        self.toggle_leds = False  # ambient LEDs (backlight); persisted, off by default
        self.control_sounds = False  # piezo press-buzz; persisted, off by default
        self.is_playing = False
        self.repeat = False
        self.shuffle = False
        self.track = None
        self.show_controls = False
        self.show_settings = False  # settings screen open instead of the player
        self.reset_pending = False  # reset button tapped once, awaiting confirm
        self.exit = False

    def copy(self):
        state = State()
        state.toggle_leds = self.toggle_leds
        state.control_sounds = self.control_sounds
        state.is_playing = self.is_playing
        state.repeat = self.repeat
        state.shuffle = self.shuffle
        state.show_controls = self.show_controls
        state.show_settings = self.show_settings
        state.reset_pending = self.reset_pending
        state.exit = self.exit
        state.track = {'id': self.track['id']} if self.track else None # only care about track id
        return state

    def __eq__(self, other):
        if not isinstance(other, State) or other is None:
            return False
        return (
            self.toggle_leds == other.toggle_leds and
            self.control_sounds == other.control_sounds and
            self.is_playing == other.is_playing and
            self.repeat == other.repeat and
            self.shuffle == other.shuffle and
            self.show_controls == other.show_controls and
            self.show_settings == other.show_settings and
            self.reset_pending == other.reset_pending and
            self.exit == other.exit and
            (self.track or {}).get('id') == (other.track or {}).get('id')
        )

class Shared:
    """Handoff between the UI tasks and the network task.

    All run cooperatively on a single asyncio loop (Pimoroni's Presto firmware
    reserves core 1 for the display, so _thread is unavailable). No locks are
    needed: each mutation is a single statement with no intervening await.
    """
    def __init__(self):
        self.commands = []         # UI -> network: queue of action tuples
        self.playback = None       # network -> UI: (track, is_playing, shuffle, repeat)
        self.playback_seq = 0      # bumped on each fetch so the UI knows it's new
        self.cover = None          # network -> UI: (track_id, jpeg_bytes)
        self.force_fetch = True    # UI -> network: poll now (e.g. after next/prev)
        self.command_until = 0     # trust optimistic is_playing/shuffle/repeat until this time
        self.next_track = None     # network -> UI: upcoming track (from the queue)
        self.prev_track = None     # network -> UI: previously played track (one-slot history)
        self.next_cover = None     # network -> UI: (track_id, jpeg) prefetched for next_track
        self.prev_cover = None     # network -> UI: (track_id, jpeg) of the previous track
        self.suppress_track_id = None  # ignore this stale outgoing id until it propagates
        self.suppress_until = 0        # safety timeout for the suppression above

class ControlButton():
    """Represents a control button with an icon and touch area."""
    def __init__(self, display, name, icons, bounds, on_press=None, update=None):
        self.name = name
        self.enabled = False
        self.icon = icons[0] if icons else None
        self.pngs = {}
        if icons:
            for icon in icons:
                png = pngdec.PNG(display)
                png.open_file("applications/spotify/icons/" + icon)
                self.pngs[icon] = png

        self.button = Button(*bounds)
        self.on_press = on_press
        self.update = update

    def is_pressed(self, state):
        """Checks if the button is enabled and currently pressed."""
        return self.enabled and self.button.is_pressed()

    def draw(self, state):
        """Draws the button icon if enabled."""
        if self.enabled and self.icon:
            self.draw_icon()

    def draw_icon(self):
        """Renders the button's icon centered inside its bounds."""
        png = self.pngs[self.icon]
        x, y, width, height = self.button.bounds
        png_width, png_height = png.get_width(), png.get_height()
        x_offset = (width-png_width)//2
        y_offset = (height-png_height)//2

        png.decode(x+x_offset, y+y_offset)

class SettingsButton():
    """A settings-screen widget: a touch area plus a custom vector draw callback.

    Shares ControlButton's touch interface (name/button/update/is_pressed/
    on_press/draw) so one dispatch loop drives both the player and the settings
    screen. `render(app, state, bounds)` draws the widget on the active layer."""
    def __init__(self, app, name, bounds, on_press, render):
        self.app = app
        self.name = name
        self.enabled = True
        self.button = Button(*bounds)
        self.on_press = on_press
        self._render = render

    def update(self, state, button):
        pass  # always enabled while the settings screen is shown

    def is_pressed(self, state):
        return self.enabled and self.button.is_pressed()

    def draw(self, state):
        self._render(self.app, state, self.button.bounds)

class Spotify(BaseApp):
    """Main Spotify app managing playback controls, track display, and UI interactions."""
    def __init__(self, wifi_ssid, wifi_password, credentials, persist=None,
                 backlight=False, control_sounds=False):
        super().__init__(ambient_light=True, full_res=True, layers=2)
        self.toggle_leds(False)  # ambient LEDs off at boot (no glow during connect)
        self.credentials = credentials
        self._persist = persist  # called when the refresh token rotates
        self._backlight = backlight  # persisted prefs, applied to state after connect
        self._control_sounds = control_sounds
        self._reset_until = 0  # deadline for the reset button's tap-again confirm

        self.display.set_layer(0)
        icon = pngdec.PNG(self.display)
        icon.open_file("applications/spotify/icon.png")
        icon.decode(self.center_x - icon.get_width()//2, self.center_y - icon.get_height()//2 - 20)
        self.presto.update()

        self.display.set_font("sans")
        self.display.set_layer(1)
        self.display_text("Connecting to WIFI", (90, self.height - 80), thickness=2)
        self.presto.update()

        # WiFi creds come from /config.json (set during setup) or legacy secrets.
        if wifi_ssid:
            # The settings-screen reset is unreachable until the app loop starts,
            # so a device with stale creds would otherwise be stuck here forever.
            # Connect via the raw STA interface (bounded timeout) and offer a
            # hold-to-reset escape hatch on each failed attempt.
            self._connect_wifi(wifi_ssid, wifi_password)
        else:
            # Legacy path: no /config.json to reset, so EzWiFi (reads secrets.py)
            # with the simple retry is fine — a reset here would do nothing.
            self.presto.connect()
            while not self.presto.wifi.isconnected():
                self.clear(1)
                self.display_text("Failed to connect to WIFI", (40, self.height - 80), thickness=2)
                time.sleep(2)

        # Disable WiFi power-save: the radio otherwise parks between beacons and adds
        # latency to requests. We're mains-powered, so keep it awake. Go through the
        # raw STA interface — presto.wifi is an EzWiFi wrapper with no config().
        try:
            wlan = network.WLAN(network.STA_IF)
            wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xa11140))
        except Exception as e:
            print("Could not disable WiFi power-save:", e)

        self.spotify_client = self.get_spotify_client()
        self.clear(1)
        self.presto.update()

        # JPEG decoder
        self.j = jpegdec.JPEG(self.display)

        # Piezo buzzer (Presto's is on GPIO 43) for tactile press feedback.
        self.buzzer = Buzzer(43)

        self.state = State()
        self.state.control_sounds = self._control_sounds
        self.shared = Shared()
        self.setup_buttons()

        # Apply the persisted backlight preference now that the connect-time
        # "LEDs off" phase is over.
        self.state.toggle_leds = self._backlight
        self.toggle_leds(self._backlight)

    def display_text(self, text, position, color=65535, scale=1, thickness=None):
        if thickness:
            self.display.set_thickness(2)
        x,y = position
        self.display.set_pen(color)
        self.display.text(text, x, y, scale=scale)
        self.presto.update()

    def get_spotify_client(self):
        return SpotifyWebApiClient(Session(self.credentials, on_credentials_changed=self._persist))

    def setup_buttons(self):
        """Initializes control buttons and their behavior."""
        # --- Shared update functions ---
        def update_show_controls(state, button):
            button.enabled = state.show_controls

        def update_always_enabled(state, button):
            button.enabled = True

        def update_play_pause(state, button):
            button.enabled = state.show_controls
            button.icon = "pause.png" if state.is_playing else "play.png"

        def update_shuffle(state, button):
            button.enabled = state.show_controls
            button.icon = "shuffle_on.png" if state.shuffle else "shuffle_off.png"

        def update_repeat(state, button):
            button.enabled = state.show_controls
            button.icon = "repeat_on.png" if state.repeat else "repeat_off.png"

        # --- On-press handlers ---
        # Network actions flip local state immediately (instant visual feedback) and
        # enqueue the API call for the worker core; the next poll reconciles real state.
        def toggle_controls(self):
            self.state.show_controls = not self.state.show_controls

        def play_pause(self):
            self.state.is_playing = not self.state.is_playing
            self.shared.command_until = time.time() + 4
            self._enqueue(("play_pause", self.state.is_playing))

        def skip_to(self, predicted, predicted_cover, command):
            # Show the predicted track + cover instantly and suppress the outgoing id so a
            # not-yet-propagated poll can't revert it; a real change still applies.
            current = self.state.track
            if predicted:
                self.state.track = predicted
                if predicted_cover and predicted_cover[0] == predicted.get("id"):
                    self.shared.cover = predicted_cover
            if current:
                self.shared.suppress_track_id = current.get("id")
                self.shared.suppress_until = time.time() + 8
            self.shared.command_until = time.time() + 4
            self._enqueue((command,))

        def next_track(self):
            skip_to(self, self.shared.next_track, self.shared.next_cover, "next")

        def previous_track(self):
            skip_to(self, self.shared.prev_track, self.shared.prev_cover, "previous")

        def toggle_shuffle(self):
            self.state.shuffle = not self.state.shuffle
            self.shared.command_until = time.time() + 4
            self._enqueue(("shuffle", self.state.shuffle))

        def toggle_repeat(self):
            self.state.repeat = not self.state.repeat
            self.shared.command_until = time.time() + 4
            self._enqueue(("repeat", self.state.repeat))

        def open_settings(self):
            self.state.show_settings = True
            self.state.reset_pending = False

        # --- Settings-screen handlers ---
        def close_settings(self):
            self.state.show_settings = False
            self.state.reset_pending = False

        def toggle_backlight(self):
            self.state.toggle_leds = not self.state.toggle_leds
            self.toggle_leds(self.state.toggle_leds)
            self.state.reset_pending = False
            self._save_settings()

        def toggle_control_sounds(self):
            self.state.control_sounds = not self.state.control_sounds
            self.state.reset_pending = False
            self._save_settings()
            if self.state.control_sounds:
                asyncio.create_task(self._buzz())  # confirm the now-on sound

        def reset_setup(self):
            # Tap once to arm, tap again within the window to actually wipe.
            if self.state.reset_pending and time.time() < self._reset_until:
                self._do_reset()
            else:
                self.state.reset_pending = True
                self._reset_until = time.time() + 4

        # --- Button configurations ---
        buttons_config = [
            # Transport: play/pause centered, previous/next on the side borders.
            ("Play", ["play.png", "pause.png"], (self.center_x - 40, self.center_y - 40, 80, 80), play_pause, update_play_pause),
            ("Previous", ["previous.png"], (0, self.center_y - 40, 80, 80), previous_track, update_show_controls),
            ("Next", ["next.png"], (self.width - 80, self.center_y - 40, 80, 80), next_track, update_show_controls),
            # Shuffle/repeat clustered in the top-left corner.
            ("Toggle Shuffle", ["shuffle_on.png", "shuffle_off.png"], (0, 0, 80, 80), toggle_shuffle, update_shuffle),
            ("Toggle Repeat", ["repeat_on.png", "repeat_off.png"], (80, 0, 80, 80), toggle_repeat, update_repeat),
            # Settings cog in the top-right corner (opens the settings screen).
            # 80-wide bounds match Next/Back so the centered glyph gets the same
            # ~20px right padding instead of sitting too far from the edge.
            ("Settings", ["settings.png"], (self.width - 80, 0, 80, 80), open_settings, update_show_controls),
            ("Toggle Controls", None, (0, 0, self.width, self.height), toggle_controls, update_always_enabled),
        ]

        # --- Create ControlButton instances ---
        self.buttons = [
            ControlButton(self.display, name, icons, bounds, on_press, update)
            for name, icons, bounds, on_press, update in buttons_config
        ]

        # --- Settings screen ---
        cp = self.display.create_pen
        self.accent = cp(28, 140, 64)       # "on" track (matches setup.py)
        self.toggle_off = cp(60, 60, 60)    # "off" track
        self.reset_red = cp(170, 45, 45)    # reset button, idle
        self.reset_red_hot = cp(220, 60, 60)  # reset button, awaiting confirm

        self.settings_buttons = [
            SettingsButton(self, "Back", (self.width - 80, 0, 80, 80), close_settings,
                           lambda app, s, b: app._draw_back_arrow(b)),
            SettingsButton(self, "Backlight", (40, 150, self.width - 80, 64), toggle_backlight,
                           lambda app, s, b: app._draw_toggle("Backlight", s.toggle_leds, b)),
            SettingsButton(self, "Control sounds", (40, 230, self.width - 80, 64), toggle_control_sounds,
                           lambda app, s, b: app._draw_toggle("Control sounds", s.control_sounds, b)),
            SettingsButton(self, "Reset setup", (90, 384, 300, 64), reset_setup,
                           lambda app, s, b: app._draw_reset(s.reset_pending, b)),
        ]

    def _save_settings(self):
        """Persists the backlight + control-sounds prefs without clobbering the
        WiFi creds or rotating refresh token (load-modify-save the whole dict)."""
        config = load_user_config() or {}
        config["backlight"] = self.state.toggle_leds
        config["control_sounds"] = self.state.control_sounds
        save_user_config(config)

    # --- Settings-screen drawing (vector; matches setup.py's aesthetic) ---
    def _draw_back_arrow(self, bounds):
        """A white left-chevron centered in its touch area."""
        x, y, w, h = bounds
        cx, cy = x + w // 2, y + h // 2
        self.display.set_pen(self.colors.WHITE)
        self.display.line(cx + 9, cy - 14, cx - 9, cy, 4)
        self.display.line(cx - 9, cy, cx + 9, cy + 14, 4)

    def _draw_toggle(self, label, on, bounds):
        """A label on the left and a pill switch (green=on) on the right."""
        x, y, w, h = bounds
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)
        self.display.text(label, x, y + (h + 16) // 2, scale=0.9)

        # Pill track: a rectangle capped by two circles, right-aligned in bounds.
        track_w, track_h = 74, 36
        r = track_h // 2
        tx = x + w - track_w
        ty = y + (h - track_h) // 2
        self.display.set_pen(self.accent if on else self.toggle_off)
        self.display.rectangle(tx + r, ty, track_w - track_h, track_h)
        self.display.circle(tx + r, ty + r, r)
        self.display.circle(tx + track_w - r, ty + r, r)

        # Knob: white circle parked right (on) or left (off).
        knob = r - 4
        kx = tx + track_w - r if on else tx + r
        self.display.set_pen(self.colors.WHITE)
        self.display.circle(kx, ty + r, knob)

    def _draw_reset(self, pending, bounds):
        """A red button; relabels + brightens while awaiting the confirm tap."""
        x, y, w, h = bounds
        self.display.set_pen(self.reset_red_hot if pending else self.reset_red)
        self.display.rectangle(x, y, w, h)
        label = "Tap again to reset" if pending else "Reset setup"
        self.display.set_pen(self.colors.WHITE)
        self.display.set_thickness(2)
        tw = self.display.measure_text(label, scale=0.8)
        self.display.text(label, x + (w - tw) // 2, y + (h + 13) // 2, scale=0.8)

    def draw_settings(self):
        """Renders the full settings screen (title + widgets) and flushes."""
        self.clear(0)
        self.clear(1)  # leaves the active layer at 1
        self.display.set_font("sans")
        self._write_centered("Settings", 90, 1.3, 3)
        for widget in self.settings_buttons:
            widget.draw(self.state)
        self.presto.update()

    def run(self):
        """Starts the UI and network event loops (cooperative, single core)."""
        loop = asyncio.get_event_loop()
        loop.create_task(self.touch_handler_loop())
        loop.create_task(self.display_loop())
        loop.create_task(self.network_loop())
        loop.run_forever()

    def _enqueue(self, command):
        """Queues a network command for the network task."""
        self.shared.commands.append(command)

    async def _buzz(self, freq=1000, ms=30, duty=0.5):
        """Plays a brief tone for tactile press feedback, then silences the piezo.

        Spawned as a fire-and-forget task so the touch handler never blocks; if taps
        overlap, the last buzz to finish silences (set_tone(-1) zeroes the duty)."""
        self.buzzer.set_tone(freq, duty)
        await asyncio.sleep_ms(ms)
        self.buzzer.set_tone(-1)

    async def _execute_command(self, command):
        """Runs a queued network command (async, non-blocking HTTP)."""
        action = command[0]
        if action == "play_pause":
            await (self.spotify_client.play() if command[1] else self.spotify_client.pause())
        elif action == "next":
            await self.spotify_client.next()
        elif action == "previous":
            await self.spotify_client.previous()
        elif action == "shuffle":
            await self.spotify_client.toggle_shuffle(command[1])
        elif action == "repeat":
            await self.spotify_client.toggle_repeat(command[1])

    async def _publish_playback(self, result):
        """Publishes a fetched playback state to the UI and downloads the cover
        on track change. Returns True if the track changed (so the caller can
        refresh the queue only then, instead of on every poll)."""
        device_id, track, is_playing, shuffle, repeat = result
        if device_id:
            self.spotify_client.session.device_id = device_id

        track_id = track.get("id")
        changed = (self._published_track or {}).get("id") != track_id
        # Remember the outgoing track + its cover so "previous" can show both instantly.
        if self._published_track and changed:
            self.shared.prev_track = self._published_track
            self.shared.prev_cover = self._published_cover
        self._published_track = track

        self.shared.playback = (track, is_playing, shuffle, repeat)
        self.shared.playback_seq += 1

        await asyncio.sleep(0)

        # Reuse an already-shown or prefetched cover; only download as a last resort.
        if self.shared.cover and self.shared.cover[0] == track_id:
            cover = self.shared.cover
        elif self.shared.next_cover and self.shared.next_cover[0] == track_id:
            cover = self.shared.next_cover
            self.shared.cover = cover
        else:
            img = await get_album_cover(track)
            cover = (track_id, img) if img else None
            if cover:
                self.shared.cover = cover
        if cover:
            self._published_cover = cover
        return changed

    async def _refresh_queue(self):
        """Caches the upcoming track and its cover so 'next' shows both instantly."""
        try:
            resp = await self.spotify_client.queue()
            upcoming = resp.get("queue") if resp else None
            nxt = upcoming[0] if upcoming else None
            self.shared.next_track = nxt
            if nxt:
                nxt_id = nxt.get("id")
                if not self.shared.next_cover or self.shared.next_cover[0] != nxt_id:
                    img = await get_album_cover(nxt)
                    if img:
                        self.shared.next_cover = (nxt_id, img)
        except Exception as e:
            print("Queue fetch failed:", e)

    async def network_loop(self):
        """Owns all network I/O: executes queued commands, polls playback, and
        downloads cover art. Runs as its own asyncio task so command/cover work is
        decoupled from the render cadence and button presses stay optimistic."""
        INTERVAL = 3  # seconds between steady-state polls; button presses stay optimistic
        last_fetch = 0
        self._published_track = None
        self._published_cover = None

        while not self.state.exit:
            # Drain queued button commands and run them.
            commands, self.shared.commands = self.shared.commands, []
            did_work = bool(commands)
            skipped = False
            for command in commands:
                try:
                    await self._execute_command(command)
                except Exception as e:
                    print("Command failed:", command, e)
                if command[0] in ("next", "previous"):
                    skipped = True
                await asyncio.sleep(0)

            now = time.time()
            if skipped:
                # The title already shows the predicted track optimistically. Poll until
                # Spotify reflects the change to confirm it and fetch the right cover; only
                # publish on a real change so a slow transition can't revert the optimism.
                prev_id = self.shared.playback[0].get("id") if self.shared.playback else None
                t0 = time.ticks_ms()
                confirmed = False
                for i in range(12):  # up to ~3.6s
                    result = await fetch_state(self.spotify_client)
                    if result and result[1].get("id") != prev_id:
                        await self._publish_playback(result)
                        confirmed = True
                        break
                    await asyncio.sleep_ms(300)
                if DEBUG:
                    elapsed = time.ticks_diff(time.ticks_ms(), t0)
                    print("[skip] {} after {} poll(s), {}ms".format(
                        "confirmed" if confirmed else "TIMED OUT", i + 1, elapsed))
                await self._refresh_queue()
                last_fetch = time.time()
            elif self.shared.force_fetch or now - last_fetch >= INTERVAL:
                self.shared.force_fetch = False
                last_fetch = now
                did_work = True
                result = await fetch_state(self.spotify_client)
                # Only refetch the queue (a ~47KB payload) when the track actually
                # changed, not on every poll — the upcoming track is otherwise stable.
                if result and await self._publish_playback(result):
                    await self._refresh_queue()

            # Collect only after real work. A full gc.collect() scans the entire 8MB
            # heap (~100ms) regardless of garbage, so running it every idle iteration
            # would peg the single core and stall in-flight network reads.
            if did_work:
                self.buzzer.set_tone(-1)  # don't let a press beep tail span the collect
                gc.collect()
            await asyncio.sleep_ms(50)

    async def touch_handler_loop(self):
        """Handles touch input, firing each button once per touch-down.

        Edge-triggered: act only on the untouched->touched transition so a single
        tap produces exactly one action (the firmware's is_pressed() is level-based)."""
        was_touching = False
        while not self.state.exit:
            self.touch.poll()
            touching = self.touch.state

            if touching and not was_touching:  # rising edge only
                # The settings screen and the player each have their own widgets.
                active = self.settings_buttons if self.state.show_settings else self.buttons
                for button in active:
                    button.update(self.state, button)
                    if button.is_pressed(self.state):
                        print(f"{button.name} pressed")
                        if self.state.control_sounds:
                            asyncio.create_task(self._buzz())  # brief tactile feedback
                        try:
                            button.on_press(self)
                        except Exception as e:
                            print(f"Failed to execute on_press: {e}")
                        break

            was_touching = touching

            await asyncio.sleep_ms(1)

    def _connect_wifi(self, ssid, password, timeout=20):
        """Associates via the raw STA interface and blocks until connected.

        presto.connect()/EzWiFi blocks internally with no timeout, which would
        freeze the boot here with no way out. We instead retry with a bounded
        timeout and, on each failure, show an inline 'hold to reset' escape hatch
        (the settings-screen reset can't be reached until the app loop runs)."""
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        while not wlan.isconnected():
            try:
                try:
                    wlan.disconnect()  # clear any half-open association before retrying
                except Exception:
                    pass
                wlan.connect(ssid, password)
            except Exception as e:
                print("WiFi connect failed:", e)

            t0 = time.time()
            while not wlan.isconnected() and time.time() - t0 < timeout:
                time.sleep_ms(300)
            if wlan.isconnected():
                return

            self.clear(1)
            self._write_centered("Can't connect to Wi-Fi", self.center_y - 16, 0.9, 2)
            self._write_centered("Hold screen 3s to reset setup", self.center_y + 34, 0.8, 2)
            self.presto.update()
            if self._held_for_reset(3000):
                self._do_reset()

    def _held_for_reset(self, hold_ms, window_ms=6000):
        """Polls touch for up to window_ms; returns True once held continuously
        for hold_ms, ramping the LEDs red as a 'keep holding' cue. Synchronous
        (runs during boot before the asyncio loop exists)."""
        start = time.ticks_ms()
        hold_start = 0
        while time.ticks_diff(time.ticks_ms(), start) < window_ms:
            self.touch.poll()
            if self.touch.state:
                if hold_start == 0:
                    hold_start = time.ticks_ms()
                held = time.ticks_diff(time.ticks_ms(), hold_start)
                lit = min(7, 1 + held * 7 // hold_ms)
                for i in range(7):
                    self.presto.set_led_rgb(i, 120 if i < lit else 0, 0, 0)
                if held >= hold_ms:
                    return True
            else:
                hold_start = 0
                for i in range(7):
                    self.presto.set_led_rgb(i, 0, 0, 0)
            time.sleep_ms(20)
        for i in range(7):
            self.presto.set_led_rgb(i, 0, 0, 0)
        return False

    def _do_reset(self):
        """Wipes the user config and reboots into the setup flow."""
        print("Config reset requested: clearing /config.json and rebooting")
        clear_user_config()
        self.clear(1)
        self._write_centered("Resetting...", self.center_y, 1.0, 2)
        self.presto.update()
        time.sleep(1)
        import machine
        machine.reset()

    def show_image(self, img, fullscreen=False):
        """Displays an album cover image.

        With controls hidden (fullscreen), decode Spotify's 640px art at full scale
        and center-crop it to fill the 480px screen (the overflow clips off-screen).
        With controls shown, decode at half (~320px) and center with a border so the
        controls living in that border stay visible. jpegdec only scales by powers of two."""
        try:
            self.j.open_RAM(memoryview(img))

            if fullscreen:
                scale = jpegdec.JPEG_SCALE_FULL
                img_width, img_height = self.j.get_width(), self.j.get_height()
            else:
                scale = jpegdec.JPEG_SCALE_HALF
                img_width, img_height = self.j.get_width() // 2, self.j.get_height() // 2
            img_x, img_y = (self.width - img_width) // 2, (self.height - img_height) // 2

            self.clear(0)
            # Silence any in-flight press beep before the decode: it blocks the single
            # core for hundreds of ms, which would otherwise stretch the tone (the buzz
            # task can't run its own set_tone(-1) until the core is free again).
            self.buzzer.set_tone(-1)
            self.j.decode(img_x, img_y, scale, dither=True)

        except OSError:
            print("Failed to load image.")

    def _write_centered(self, text, y, scale, thickness):
        """Draws white text with a shadow, horizontally centered."""
        self.display.set_thickness(thickness)
        x = (self.width - self.display.measure_text(text, scale=scale)) // 2
        self.display.set_pen(self.colors._BLACK)
        self.display.text(text, x + 2, y + 3, scale=scale)
        self.display.set_pen(self.colors.WHITE)
        self.display.text(text, x, y, scale=scale)

    def write_track(self):
        """Writes the track name and artists centered along the bottom border.

        Always shown (shadowed text stays legible over the full-screen cover too).
        With nothing playing (just the Spotify logo on screen), captions that."""
        if not self.state.track:
            self._write_centered("No song is playing", self.center_y + 90, 1.0, 3)
            return

        track_name = self.state.track.get("name") or ""
        track_name = ''.join(i if ord(i) < 128 else ' ' for i in track_name)
        if len(track_name) > 20:
            track_name = track_name[:20] + " ..."
        # "sans" is a vector font drawn from the baseline, so these y values are
        # where each line's baseline sits — chosen to center the block in the band.
        self._write_centered(track_name, self.height - 42, 1.1, 3)

        artists = ", ".join(a.get("name") for a in (self.state.track.get("artists") or []))
        artists = ''.join(i if ord(i) < 128 else ' ' for i in artists)
        if len(artists) > 35:
            artists = artists[:35] + " ..."
        self._write_centered(artists, self.height - 16, 0.7, 2)

    async def display_loop(self):
        """Renders the latest track info and controls (network-free).

        Reads playback and cover art produced by the network task; the only place
        JPEG decode and presto.update() run."""
        prev_state = None
        last_seq = -1
        last_cover_id = None
        last_fullscreen = None

        while not self.state.exit:
            # Pull the latest playback snapshot from the network task.
            if self.shared.playback_seq != last_seq:
                last_seq = self.shared.playback_seq
                playback = self.shared.playback
                if playback:
                    track, is_playing, shuffle, repeat = playback
                    track_id = track.get("id") if track else None
                    # Ignore the stale outgoing track a not-yet-propagated skip keeps
                    # returning; accept any other id (or give up after the timeout).
                    if not (track_id == self.shared.suppress_track_id
                            and time.time() < self.shared.suppress_until):
                        self.state.track = track
                        if track_id != self.shared.suppress_track_id:
                            self.shared.suppress_track_id = None
                    # Keep optimistic toggles right after a command, so a not-yet-applied
                    # poll result doesn't briefly revert the icon.
                    if time.time() >= self.shared.command_until:
                        self.state.is_playing = is_playing
                        self.state.shuffle = shuffle
                        self.state.repeat = repeat

            await asyncio.sleep(0)

            # Settings screen takes over the display entirely; skip the player render.
            if self.state.show_settings:
                # Let an unconfirmed reset expire so its label reverts on its own.
                if self.state.reset_pending and time.time() > self._reset_until:
                    self.state.reset_pending = False
                if prev_state != self.state:
                    self.draw_settings()
                    prev_state = self.state.copy()
                # Force the cover to redraw when we return to the player.
                last_cover_id = None
                last_fullscreen = None
                await asyncio.sleep_ms(120)
                continue

            # Decode the cover (layer 0) on a new download, or re-decode at the other
            # scale when controls toggle (full-screen crop hidden vs. bordered shown).
            cover_updated = False
            fullscreen = not self.state.show_controls
            cover = self.shared.cover
            if cover and (cover[0] != last_cover_id or fullscreen != last_fullscreen):
                last_cover_id, img = cover
                last_fullscreen = fullscreen
                self.show_image(img, fullscreen=fullscreen)
                cover_updated = True

            await asyncio.sleep(0)

            # Re-render when state changes (commit 768e05a) or a new cover was decoded
            # (otherwise the decoded cover would sit unflushed until the next change).
            if cover_updated or prev_state != self.state:
                self.clear(1)
                for button in self.buttons:
                    button.update(self.state, button)  # refresh icon/enabled at draw time
                    button.draw(self.state)
                self.write_track()

                self.presto.update()
                prev_state = self.state.copy()

            # Collect only after a cover decode (the one big transient allocation here);
            # the per-poll collect in network_loop reclaims everything else globally.
            if cover_updated:
                gc.collect()
            await asyncio.sleep_ms(120)

async def fetch_state(spotify_client):
    """Fetches the current playback state from Spotify."""

    current_track = None
    is_playing = False
    shuffle = False
    repeat = False
    device_id = None
    try:
        resp = await spotify_client.current_playing()
        if resp and resp.get("item"):
            current_track = resp["item"]
            is_playing = resp.get("is_playing")
            shuffle = resp.get("shuffle_state")
            repeat = resp.get("repeat_state", "off") != "off"
            device_id = resp["device"]["id"]
    except Exception as e:
        print("Failed to get current playing track:", e)

    if not current_track:
        try:
            resp = await spotify_client.recently_played()
            if resp and resp.get("items"):
                current_track = resp["items"][0]["track"]
        except Exception as e:
            print("Failed to get recently played track:", e)

    if not current_track:
        return None

    return device_id, current_track, is_playing, shuffle, repeat

async def get_album_cover(track):
    """Fetches the album cover straight from Spotify's CDN (largest available)."""
    try:
        img_url = track["album"]["images"][0]["url"]  # ~640px; decoded at half in show_image
        status, _, body = await fetch_url(img_url)
        if status == 200:
            return body
        print("Failed to fetch image:", status)
    except Exception as e:
        print("Fetch image error:", e)

    return None

def _resolve_boot():
    """Decides whether to run the app or the setup flow.

    Returns ("run", (wifi_ssid, wifi_password, credentials, persist, backlight,
    control_sounds)) or ("setup", (client_id, config)). The provisioned client_id
    lives in the gitignored secrets source; WiFi + refresh token + the user's
    backlight/control-sounds prefs live in /config.json. The persist callback
    writes rotated PKCE refresh tokens back to flash (None for the legacy path,
    which has no /config.json to update)."""
    secrets = load_secrets()
    client_id = getattr(secrets, "SPOTIFY_CLIENT_ID", None)
    legacy = getattr(secrets, "SPOTIFY_CREDENTIALS", None)
    config = load_user_config()

    settings = config or {}
    backlight = settings.get("backlight", False)
    control_sounds = settings.get("control_sounds", False)

    # Back-compat: a full pre-generated credentials dict boots directly.
    if legacy:
        return "run", (getattr(secrets, "WIFI_SSID", None),
                       getattr(secrets, "WIFI_PASSWORD", None), legacy, None,
                       backlight, control_sounds)

    if is_configured(config):
        credentials = {
            "client_id": client_id,
            "refresh_token": config["refresh_token"],
            "device_id": config.get("device_id"),
        }
        return "run", (config.get("wifi_ssid"), config.get("wifi_password"),
                       credentials, _persist_refresh_token, backlight, control_sounds)

    return "setup", (client_id, config)


def _persist_refresh_token(credentials):
    """Writes a rotated refresh token back to /config.json so it survives reboots."""
    config = load_user_config() or {}
    config["refresh_token"] = credentials.get("refresh_token")
    save_user_config(config)


def launch():
    """Runs the setup flow if unconfigured, otherwise the Spotify app."""
    mode, args = _resolve_boot()

    if mode == "setup":
        from applications.spotify.setup import SetupApp
        client_id, config = args
        app = SetupApp(client_id, config)
        asyncio.get_event_loop().run_until_complete(app.run())  # ends by rebooting
        return

    app = Spotify(*args)
    app.run()

    app.clear()
    del app
    gc.collect()
