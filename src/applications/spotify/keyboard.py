"""A touch on-screen keyboard for the 480x480 Presto display.

Used by the self-serve setup flow to enter a WiFi password (and, for hidden
networks, an SSID). Reuses the same primitives as the Spotify controls: the
firmware `touch.Button` for hit-testing and the "sans" vector font measured with
`display.measure_text` for centering labels. Runs its own cooperative asyncio
loop and returns the entered string (or None if cancelled).
"""

import uasyncio as asyncio
from touch import Button

# Letter and symbol pages. Plain strings are inserted verbatim; UPPERCASE tokens
# are special keys handled in _on_key.
_LOWER = [
    list("qwertyuiop"),
    list("asdfghjkl"),
    ["SHIFT", "z", "x", "c", "v", "b", "n", "m", "DEL"],
]
_UPPER = [
    list("QWERTYUIOP"),
    list("ASDFGHJKL"),
    ["SHIFT", "Z", "X", "C", "V", "B", "N", "M", "DEL"],
]
_SYM1 = [
    list("1234567890"),
    ["!", "@", "#", "$", "%", "&", "*", "(", ")"],
    ["MORE", "-", "_", "=", "+", "/", ":", ";", "DEL"],
]
_SYM2 = [
    ["[", "]", "{", "}", ";", ":", "'", '"', "\\"],
    ["<", ">", ",", ".", "?", "|", "~", "`", "^"],
    ["MORE", "DEL"],
]


class Keyboard:
    """Modal on-screen keyboard. Call `await kb.run()` to get the entered text."""

    MARGIN = 6
    ROW_TOP = 250
    ROW_H = 52
    ROW_GAP = 6

    def __init__(self, app, title="Enter password", mask=True, initial=""):
        self.app = app
        self.display = app.display
        self.touch = app.touch
        self.presto = app.presto
        self.width = app.width
        self.height = app.height
        self.title = title
        self.mask = mask
        self.show = True    # masked fields start visible; the eye toggle hides them
        self.text = initial

        self.page = "abc"   # "abc" | "sym1" | "sym2"
        self.caps = False
        self.keys = []
        self.dirty_keys = True
        self.result = None
        self.done = False

        # Pens for the keyboard chrome.
        cp = self.display.create_pen
        self.bg = cp(0, 0, 0)
        self.key_bg = cp(45, 45, 45)
        self.special_bg = cp(70, 70, 70)
        self.ok_bg = cp(28, 140, 64)
        self.field_bg = cp(22, 22, 22)
        self.text_pen = cp(255, 250, 240)
        self.hint_pen = cp(150, 150, 150)

    # --- layout -----------------------------------------------------------
    def _rows(self):
        if self.page == "abc":
            return _UPPER if self.caps else _LOWER
        return _SYM1 if self.page == "sym1" else _SYM2

    def _build_keys(self):
        """Rebuilds Button hit-regions for the current page."""
        self.keys = []
        m = self.MARGIN
        gap = 4
        rows = self._rows()
        for r, row in enumerate(rows):
            top = self.ROW_TOP + r * (self.ROW_H + self.ROW_GAP)
            n = len(row)
            usable = self.width - 2 * m
            kw = (usable - (n - 1) * gap) // n
            for i, token in enumerate(row):
                x = m + i * (kw + gap)
                self.keys.append(self._make_key(token, x, top, kw, self.ROW_H))

        # Bottom row: page-toggle, space, OK.
        top = self.ROW_TOP + 3 * (self.ROW_H + self.ROW_GAP)
        mode_w = 96
        ok_w = 96
        ok_x = self.width - m - ok_w
        space_x = m + mode_w + gap
        space_w = ok_x - gap - space_x
        toggle = "SYM" if self.page == "abc" else "ABC"
        self.keys.append(self._make_key(toggle, m, top, mode_w, self.ROW_H))
        self.keys.append(self._make_key("SPACE", space_x, top, space_w, self.ROW_H))
        self.keys.append(self._make_key("OK", ok_x, top, ok_w, self.ROW_H))

        # Cancel button in the title bar.
        self.cancel = self._make_key("CANCEL", m, 8, 96, 44)
        # Show/Hide toggle at the right edge of the input box (masked fields only).
        if self.mask:
            self.eye = self._make_key("EYE", self.width - m - 64, 70, 64, 56)

    def _make_key(self, token, x, y, w, h):
        return {"token": token, "x": x, "y": y, "w": w, "h": h, "button": Button(x, y, w, h)}

    # --- drawing ----------------------------------------------------------
    _LABELS = {"DEL": "del", "SHIFT": "shift", "MORE": "=\\<", "SYM": "?123",
               "ABC": "abc", "SPACE": "space", "OK": "OK", "CANCEL": "cancel"}

    def _draw_key(self, key):
        token = key["token"]
        if token == "OK":
            bg = self.ok_bg
        elif token in ("DEL", "SHIFT", "MORE", "SYM", "ABC", "SPACE", "CANCEL"):
            bg = self.special_bg
        else:
            bg = self.key_bg
        self.display.set_pen(bg)
        self.display.rectangle(key["x"], key["y"], key["w"], key["h"])

        label = self._LABELS.get(token, token)
        scale = 0.7 if len(label) > 1 else 1.0
        tw = self.display.measure_text(label, scale=scale)
        tx = key["x"] + (key["w"] - tw) // 2
        ty = key["y"] + (key["h"] + int(14 * scale)) // 2
        self.display.set_pen(self.text_pen)
        self.display.set_thickness(2)
        self.display.text(label, tx, ty, scale=scale)

    def _draw_field(self):
        # Title + cancel.
        self.display.set_pen(self.bg)
        self.display.rectangle(0, 0, self.width, self.ROW_TOP - 4)
        self._draw_key(self.cancel)
        self.display.set_pen(self.text_pen)
        self.display.set_thickness(2)
        self.display.text(self.title, 120, 38, scale=0.9)

        # Input box (leaves room for the Show/Hide toggle when masking).
        bx, by, bh = self.MARGIN, 70, 56
        eye_w = 64 if self.mask else 0
        gap = 6 if self.mask else 0
        bw = self.width - 2 * self.MARGIN - eye_w - gap
        self.display.set_pen(self.field_bg)
        self.display.rectangle(bx, by, bw, bh)

        shown = ("*" * len(self.text)) if (self.mask and not self.show) else self.text
        if not shown:
            self.display.set_pen(self.hint_pen)
            self.display.text("...", bx + 12, by + 38, scale=0.9)
        else:
            # Keep the tail visible for long entries.
            while shown and self.display.measure_text(shown + "_", scale=0.9) > bw - 24:
                shown = shown[1:]
            self.display.set_pen(self.text_pen)
            self.display.set_thickness(2)
            self.display.text(shown + "_", bx + 12, by + 38, scale=0.9)

        if self.mask:
            e = self.eye
            self.display.set_pen(self.special_bg)
            self.display.rectangle(e["x"], e["y"], e["w"], e["h"])
            label = "Hide" if self.show else "Show"
            tw = self.display.measure_text(label, scale=0.6)
            self.display.set_pen(self.text_pen)
            self.display.set_thickness(2)
            self.display.text(label, e["x"] + (e["w"] - tw) // 2, e["y"] + 35, scale=0.6)

    def _render(self):
        if self.dirty_keys:
            self.display.set_pen(self.bg)
            self.display.clear()
            self._build_keys()
            for key in self.keys:
                self._draw_key(key)
            self.dirty_keys = False
        self._draw_field()
        self.presto.update()

    # --- input ------------------------------------------------------------
    def _on_key(self, token):
        if token == "OK":
            self.result = self.text
            self.done = True
        elif token == "CANCEL":
            self.result = None
            self.done = True
        elif token == "DEL":
            self.text = self.text[:-1]
        elif token == "SPACE":
            self.text += " "
        elif token == "SHIFT":
            self.caps = not self.caps
            self.dirty_keys = True
        elif token == "SYM":
            self.page = "sym1"
            self.dirty_keys = True
        elif token == "ABC":
            self.page = "abc"
            self.dirty_keys = True
        elif token == "MORE":
            self.page = "sym2" if self.page == "sym1" else "sym1"
            self.dirty_keys = True
        else:
            self.text += token

    async def run(self):
        """Draws the keyboard and processes taps until OK/Cancel. Returns the
        string (OK) or None (Cancel). Edge-triggered: one tap = one key."""
        self._render()
        was_touching = False
        while not self.done:
            self.touch.poll()
            touching = self.touch.state
            if touching and not was_touching:
                changed = True
                if self.cancel["button"].is_pressed():
                    self._on_key("CANCEL")
                elif self.mask and self.eye["button"].is_pressed():
                    self.show = not self.show
                else:
                    changed = False
                    for key in self.keys:
                        if key["button"].is_pressed():
                            self._on_key(key["token"])
                            changed = True
                            break
                if changed and not self.done:
                    self._render()
            was_touching = touching
            await asyncio.sleep_ms(10)
        return self.result
