"""Minimal on-device HTTP server that drives the Spotify OAuth (PKCE) hand-off.

Spotify only allows http redirect URIs for the literal loopback 127.0.0.1, so
the device can't be the registered redirect. Instead a static GitHub Pages helper
(HELPER_REDIRECT, https) is registered, and it bounces the phone back to this
server via a top-level navigation, carrying the device's ip:port in the OAuth
`state`. Flow:

  phone -> http://<ip>:<port>/        (this server: "Connect Spotify")
        -> accounts.spotify.com/authorize?...&redirect_uri=<helper>&state=<ip:port>|<nonce>
        -> <helper>?code=...&state=<ip:port>|<nonce>   (helper splits state)
        -> http://<ip>:<port>/callback?code=...&state=<nonce>
        -> this server exchanges the code (PKCE) and stores the refresh token.
"""

import hashlib
import binascii
import os
import time
import uasyncio as asyncio

from applications.spotify.spotify_client import exchange_code, urlencode

# Static helper page registered as the Spotify Redirect URI (https, no secrets).
# Must match the page committed under docs/cb/ and the app's dashboard setting.
HELPER_REDIRECT = "https://msokk.github.io/PrestoDeck/cb/"

AUTHORIZE = "https://accounts.spotify.com/authorize"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-recently-played"

_PKCE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def _random_bytes(n):
    try:
        return os.urandom(n)
    except Exception:
        import random
        try:
            random.seed(time.ticks_us())
        except Exception:
            random.seed(int(time.time()))
        return bytes(random.getrandbits(8) for _ in range(n))


def _random_token(length):
    return "".join(_PKCE_CHARS[b % len(_PKCE_CHARS)] for b in _random_bytes(length))


def _b64url(data):
    s = binascii.b2a_base64(data).decode().strip()
    return s.replace("+", "-").replace("/", "_").rstrip("=")


def _challenge_for(verifier):
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _esc(s):
    """HTML-escapes a value before it's reflected into a response body."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _unquote(s):
    s = s.replace("+", " ")
    if "%" not in s:
        return s
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s):
            try:
                out.append(chr(int(s[i + 1:i + 3], 16)))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return "".join(out)


def _parse_query(query):
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        params[_unquote(k)] = _unquote(v)
    return params


_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrestoDeck setup</title>
<style>
body{{margin:0;background:#121212;color:#f5f5f5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.card{{text-align:center;padding:32px;max-width:340px}}
h1{{font-size:22px;margin:0 0 8px}}p{{color:#b3b3b3;line-height:1.5}}
a.btn,div.msg{{display:block;margin-top:24px;padding:16px;border-radius:32px;
font-size:18px;font-weight:600;text-decoration:none}}
a.btn{{background:#1db954;color:#fff}}
</style></head><body><div class="card">{body}</div></body></html>"""


class SetupServer:
    """Serves the setup page and completes the OAuth exchange. Poll `.done`."""

    def __init__(self, client_id, ip, port=8080):
        self.client_id = client_id
        self.ip = ip
        self.port = port
        self.redirect_uri = HELPER_REDIRECT
        self.verifier = _random_token(64)
        self.challenge = _challenge_for(self.verifier)
        self.nonce = _random_token(16)
        self.state = "{}:{}|{}".format(ip, port, self.nonce)
        self.tokens = None
        self.error = None
        self.done = False
        self._exchanging = False
        self._server = None

    def authorize_url(self):
        return AUTHORIZE + "?" + urlencode(dict(
            client_id=self.client_id,
            response_type="code",
            redirect_uri=self.redirect_uri,
            scope=SCOPES,
            code_challenge_method="S256",
            code_challenge=self.challenge,
            state=self.state,
        ))

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)

    async def stop(self):
        if self._server is not None:
            try:
                self._server.close()
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            parts = line.split(b" ")
            target = parts[1].decode() if len(parts) > 1 else "/"
            while True:  # drain request headers
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break

            path, _, query = target.partition("?")
            if path == "/":
                body = _PAGE.format(body=(
                    '<h1>PrestoDeck</h1>'
                    '<p>Link your Spotify account to finish setup. '
                    'You\'ll approve access, then this device takes over.</p>'
                    '<a class="btn" href="{}">Connect Spotify</a>'
                ).format(self.authorize_url()))
                await self._respond(writer, 200, body)
            elif path == "/callback":
                await self._callback(writer, _parse_query(query))
            else:
                await self._respond(writer, 404, _PAGE.format(body="<h1>Not found</h1>"))
        except Exception as e:
            print("Setup request failed:", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _callback(self, writer, params):
        # An authorization code is single-use: replaying it makes Spotify revoke
        # the tokens from the first exchange. Browsers reload /callback (mobile
        # refresh, prefetch), so never exchange the same code twice.
        if self.done or self.tokens is not None:
            await self._message(writer, 200, "All set!",
                                "Already linked — you can close this tab.")
            return
        if self._exchanging:
            await self._message(writer, 200, "Finishing up",
                                "Almost done — please wait a moment.")
            return

        code = params.get("code")
        state = params.get("state")
        if params.get("error"):
            await self._message(writer, 400, "Authorization denied",
                                 "Spotify reported: " + params["error"])
            return
        if not code or state != self.nonce:
            await self._message(writer, 400, "Setup error",
                                "Invalid or expired request. Reopen the setup page on the device.")
            return

        self._exchanging = True
        try:
            self.tokens = await exchange_code(
                self.client_id, code, self.verifier, self.redirect_uri)
            await self._message(writer, 200, "All set!",
                                "PrestoDeck is connected. You can close this tab — the device will restart.")
            self.done = True
        except Exception as e:
            self.error = str(e)
            await self._message(writer, 500, "Couldn't finish",
                                "Token exchange failed: " + str(e))
        finally:
            self._exchanging = False

    async def _message(self, writer, status, title, text):
        body = _PAGE.format(body='<h1>{}</h1><p>{}</p>'.format(_esc(title), _esc(text)))
        await self._respond(writer, status, body)

    async def _respond(self, writer, status, body, content_type="text/html"):
        if isinstance(body, str):
            body = body.encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  500: "Internal Server Error"}.get(status, "OK")
        head = ("HTTP/1.1 {} {}\r\nContent-Type: {}\r\n"
                "Content-Length: {}\r\nConnection: close\r\n\r\n").format(
                    status, reason, content_type, len(body))
        writer.write(head.encode())
        writer.write(body)
        await writer.drain()
