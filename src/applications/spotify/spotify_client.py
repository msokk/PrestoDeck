import asyncio
import json
import tls


def _split_url(url):
    """Splits 'https://host/path?query' into ('host', '/path?query')."""
    rest = url.split("://", 1)[1]
    slash = rest.find("/")
    if slash == -1:
        return rest, "/"
    return rest[:slash], rest[slash:]


def _new_tls_context():
    ctx = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = tls.CERT_NONE  # no CA bundle / RTC on the device
    return ctx


class Connection:
    """A single HTTP/1.1-over-TLS connection that can be kept alive and reused.

    Network waits (TCP/TLS round-trips, body download) yield to the asyncio loop,
    so the UI stays responsive. Reused for the api.spotify.com hot path so the TLS
    handshake is paid once, not on every poll.
    """
    def __init__(self, host, port=443):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None

    async def _connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=_new_tls_context(), server_hostname=self.host
        )

    async def close(self):
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = self.writer = None

    async def request(self, method, path, headers=None, body=None):
        # Reconnect once if the kept-alive socket has gone stale.
        for attempt in range(2):
            try:
                if self.writer is None:
                    await self._connect()
                return await self._do_request(method, path, headers, body)
            except Exception:
                await self.close()
                if attempt == 1:
                    raise

    async def _do_request(self, method, path, headers, body):
        h = {"Host": self.host, "Connection": "keep-alive"}
        if headers:
            h.update(headers)
        if body is not None:
            h["Content-Length"] = str(len(body))
        elif method in ("POST", "PUT", "PATCH"):
            h["Content-Length"] = "0"  # Spotify returns 411 without it
        req = method + " " + path + " HTTP/1.1\r\n"
        for k, v in h.items():
            req += k + ": " + v + "\r\n"
        req += "\r\n"
        self.writer.write(req.encode())
        if body:
            self.writer.write(body)
        await self.writer.drain()
        return await self._read_response()

    async def _read_response(self):
        status_line = await self.reader.readline()
        if not status_line:
            raise OSError("connection closed")
        status = int(status_line.split(None, 2)[1])

        headers = {}
        while True:
            line = await self.reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()

        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = await self._read_chunked()
        elif "content-length" in headers:
            n = int(headers["content-length"])
            body = await self.reader.readexactly(n) if n else b""
        else:
            body = b""  # no body (e.g. 204); Spotify/wsrv always size real bodies

        if "close" in headers.get("connection", "").lower():
            await self.close()  # server won't keep this one alive
        return status, headers, body

    async def _read_chunked(self):
        chunks = []
        while True:
            size_line = await self.reader.readline()
            if not size_line:
                break
            size = int(size_line.split(b";")[0].strip(), 16)
            if size == 0:
                await self.reader.readline()  # trailing CRLF
                break
            chunks.append(await self.reader.readexactly(size))
            await self.reader.readline()  # CRLF after each chunk
        return b"".join(chunks)


async def fetch_url(url):
    """One-shot async GET. Returns (status, headers, body bytes)."""
    host, path = _split_url(url)
    conn = Connection(host)
    try:
        return await conn.request("GET", path)
    finally:
        await conn.close()


class SpotifyWebApiClient:
    def __init__(self, session):
        self.session = session

    async def play(self):
        await self.session.put("https://api.spotify.com/v1/me/player/play")

    async def pause(self):
        await self.session.put("https://api.spotify.com/v1/me/player/pause")

    async def toggle_shuffle(self, state):
        value = "true" if state else "false"
        await self.session.put("https://api.spotify.com/v1/me/player/shuffle?state=" + value)

    async def toggle_repeat(self, state):
        value = "track" if state else "off"
        await self.session.put("https://api.spotify.com/v1/me/player/repeat?state=" + value)

    async def next(self):
        await self.session.post("https://api.spotify.com/v1/me/player/next")

    async def previous(self):
        await self.session.post("https://api.spotify.com/v1/me/player/previous")

    async def current_playing(self):
        return await self.session.get("https://api.spotify.com/v1/me/player")

    async def recently_played(self):
        return await self.session.get("https://api.spotify.com/v1/me/player/recently-played?limit=1")

    async def queue(self):
        return await self.session.get("https://api.spotify.com/v1/me/player/queue")


class Session:
    def __init__(self, credentials):
        self.credentials = credentials
        self.device_id = credentials["device_id"]
        self.api = Connection("api.spotify.com")  # kept alive for the hot path

    async def get(self, url):
        return await self._request("GET", url, add_device=False)

    async def put(self, url, json_body=None):
        return await self._request("PUT", url, json_body=json_body, add_device=True)

    async def post(self, url, json_body=None):
        return await self._request("POST", url, json_body=json_body, add_device=True)

    async def _ensure_device(self):
        """Picks an available device when none is set, so commands have a target
        (and Spotify activates it). No-op once a device id is known."""
        if self.device_id:
            return
        try:
            resp = await self.get("https://api.spotify.com/v1/me/player/devices")
            devices = resp.get("devices") if resp else None
            if devices:
                active = next((d for d in devices if d.get("is_active")), None)
                self.device_id = (active or devices[0]).get("id")
                print("Using Spotify device:", self.device_id)
        except Exception as e:
            print("Device lookup failed:", e)

    async def _request(self, method, url, json_body=None, add_device=False):
        if "access_token" not in self.credentials:
            await self._refresh_access_token()

        host, path = _split_url(url)
        if add_device:
            if not self.device_id:
                await self._ensure_device()
            path = self._add_device_id(path)

        headers = self._headers()
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"

        conn = self.api if host == "api.spotify.com" else Connection(host)
        try:
            status, resp_headers, resp_body = await conn.request(method, path, headers, body)
            if status == 401:  # access token expired: refresh and retry once
                await self._refresh_access_token()
                headers = self._headers()
                if json_body is not None:
                    headers["Content-Type"] = "application/json"
                status, resp_headers, resp_body = await conn.request(method, path, headers, body)
        finally:
            if conn is not self.api:
                await conn.close()

        if status >= 400:
            reason = None
            try:
                error = json.loads(resp_body).get("error", {})
                reason = error.get("reason") or error.get("message")
            except Exception:
                pass
            raise SpotifyWebApiError(
                "HTTP {} {}".format(status, reason or "").strip(), status=status, reason=reason
            )

        if resp_body and resp_headers.get("content-type", "").startswith("application/json"):
            return json.loads(resp_body)
        return None

    def _headers(self):
        return {"Authorization": "Bearer " + self.credentials["access_token"]}

    def _add_device_id(self, path):
        if not self.device_id:
            return path
        join = "&" if "?" in path else "?"
        return "{path}{join}device_id={device_id}".format(
            path=path, join=join, device_id=self.device_id
        )

    async def _refresh_access_token(self):
        body = urlencode(dict(
            grant_type="refresh_token",
            refresh_token=self.credentials["refresh_token"],
            client_id=self.credentials["client_id"],
            client_secret=self.credentials["client_secret"],
        )).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        conn = Connection("accounts.spotify.com")
        try:
            for attempt in range(3):
                try:
                    status, _, resp_body = await conn.request("POST", "/api/token", headers, body)
                    if status < 400:
                        tokens = json.loads(resp_body)
                        self.credentials["access_token"] = tokens["access_token"]
                        if "refresh_token" in tokens:
                            self.credentials["refresh_token"] = tokens["refresh_token"]
                        return
                except Exception as e:
                    print("Failed to refresh access token, retrying:", e)
                await asyncio.sleep_ms(500)
            print("Failed to refresh access token after 3 tries, giving up")
        finally:
            await conn.close()


class SpotifyWebApiError(Exception):
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def quote(s):
    always_safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz' '0123456789' '_.-'
    res = []
    for c in s:
        if c in always_safe:
            res.append(c)
            continue
        res.append('%%%x' % ord(c))
    return ''.join(res)


def quote_plus(s):
    s = quote(s)
    if ' ' in s:
        s = s.replace(' ', '+')
    return s


def urlencode(query):
    if isinstance(query, dict):
        query = query.items()
    li = []
    for k, v in query:
        if not isinstance(v, list):
            v = [v]
        for value in v:
            k = quote_plus(str(k))
            v = quote_plus(str(value))
            li.append(k + '=' + v)
    return '&'.join(li)
