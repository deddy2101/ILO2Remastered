"""Serves the iLO2 remote console over the web: a WebSocket pushes JPEG
frame snapshots + log lines to any number of browser clients and accepts
keyboard/mouse/power commands back, and a plain HTTP server serves the
static frontend page. Nothing here is tied to any single "renderer" --
point any client (a browser tab, a future HLS transcoder, a diagnostics
dashboard) at the same WebSocket.
"""
import asyncio
import functools
import http.server
import json
import queue
import signal
import socket
import struct
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import websockets

from .auth import (
    SESSION_COOKIE, AuthConfig, LoginRateLimiter, SessionStore,
    clear_cookie_header, parse_cookies, session_cookie_header,
)
from .console import IloConsole
from .framebuffer import FrameBuffer
from .session import IloSession, SessionExhaustedError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_FRAME_HEADER = struct.Struct("!HHHH")  # x, y, w, h -- a rect, not a bitmask


def _pack_frame(x, y, w, h, jpeg_bytes):
    """Wire format for a video update, full-frame or partial alike: an
    8-byte (x, y, w, h) header followed by that rect's JPEG bytes. The
    client always just draws the JPEG at (x, y) -- a full frame is simply
    the special case x == y == 0 and (w, h) covering the whole canvas, no
    separate "is this a keyframe" flag needed on either side."""
    return _FRAME_HEADER.pack(x, y, w, h) + jpeg_bytes


class _AuthenticatedHandler(http.server.SimpleHTTPRequestHandler):
    """Static file server that gates the app page behind a session cookie
    (when WEBAPP_USER/WEBAPP_PASSWORD are set) and handles login/logout.
    Everything else (manifest, icons, sw.js, the login page itself) stays
    reachable unauthenticated -- none of it is sensitive on its own, and
    the login page obviously has to load before anyone can log in."""

    def __init__(self, *args, auth, sessions, rate_limiter, **kwargs):
        self.auth = auth
        self.sessions = sessions
        self.rate_limiter = rate_limiter
        super().__init__(*args, **kwargs)

    def _is_authenticated(self):
        if not self.auth.enabled:
            return True
        cookies = parse_cookies(self.headers.get("Cookie"))
        return self.sessions.validate(cookies.get(SESSION_COOKIE))

    def _is_https(self):
        # TLS terminates at the reverse proxy in front of this process (see
        # README) -- trust its X-Forwarded-Proto to decide whether the
        # session cookie can carry Secure. It can't over the plain
        # http://localhost used for LAN/dev testing, or the browser would
        # silently refuse to store it and login would never "stick".
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/logout":
            self._handle_logout()
            return
        if self.auth.enabled and path in ("/", "/index.html") and not self._is_authenticated():
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            self._handle_login()
        elif path == "/api/logout":
            self._handle_logout()
        else:
            self.send_error(404)

    def _handle_login(self):
        ip = self.client_address[0]
        if self.rate_limiter.is_locked_out(ip):
            self.send_response(429)
            self.send_header("Retry-After", "300")
            self.end_headers()
            self.wfile.write(b"Troppi tentativi falliti, riprova tra qualche minuto.\n")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(body)
        user = fields.get("user", [""])[0]
        password = fields.get("password", [""])[0]
        if self.auth.check(user, password):
            self.rate_limiter.record_success(ip)
            token = self.sessions.create()
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", session_cookie_header(token, secure=self._is_https()))
            self.end_headers()
        else:
            self.rate_limiter.record_failure(ip)
            self.send_response(302)
            self.send_header("Location", "/login.html?error=1")
            self.end_headers()

    def _handle_logout(self):
        cookies = parse_cookies(self.headers.get("Cookie"))
        self.sessions.revoke(cookies.get(SESSION_COOKIE))
        self.send_response(302)
        self.send_header("Location", "/login.html")
        self.send_header("Set-Cookie", clear_cookie_header(secure=self._is_https()))
        self.end_headers()

_MOUSE_LEFT = 4
_MOUSE_CENTER = 2
_MOUSE_RIGHT = 1

STATUS_POLL_INTERVAL = 15  # seconds; power/UID/fans/temps don't change fast


class WebServer:
    def __init__(self, host, username, password, http_port=8080, ws_port=8765, ilo_port=443):
        self.host = host
        self.username = username
        self.password = password
        self.http_port = http_port
        self.ws_port = ws_port

        self.fb = FrameBuffer()
        # ilo_port: iLO2's HTTPS/RIBCL port as *this process* reaches it --
        # 443 on the LAN, but e.g. a router's WAN-side NAT port when this is
        # reached through a port-forward (iLO2 itself still only listens on
        # 443; the number here is whatever gets you there). The KVM
        # console's own port doesn't need this: IloConsole connects to
        # whatever iLO2's own drc2fram.htm page reports (INFO6, always
        # "23" in practice) on `host`, so a 1:1 (unmodified) port forward
        # for that one is assumed.
        self.session = IloSession(host, username, password, port=ilo_port)
        self.console = None
        self.clients = set()
        self._event_queue = queue.Queue()
        self._connecting = threading.Lock()
        self._last_status = None
        self._server_info = None
        self.console_state = {"type": "console_state", "state": "idle", "detail": None}

        self.auth = AuthConfig()
        self.sessions = SessionStore()
        self.rate_limiter = LoginRateLimiter()

    # ---- logging / state events (thread-safe: just queues, the asyncio
    # side drains them) ---------------------------------------------------
    def log(self, msg):
        print(msg, flush=True)
        self._event_queue.put({"type": "log", "text": str(msg)})

    def set_console_state(self, state, detail=None):
        self.console_state = {"type": "console_state", "state": state, "detail": detail}
        self._event_queue.put(self.console_state)

    # ---- console connection (blocking, runs in its own thread) ----------
    def start_console_thread(self):
        if not self._connecting.acquire(blocking=False):
            self.log("Connessione console già in corso, ignoro la richiesta duplicata.")
            return
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _wait_for_ilo(self, timeout=90):
        deadline = time.time() + timeout
        time.sleep(5)  # iLO2 drops connections immediately after RESET_RIB
        while time.time() < deadline:
            try:
                socket.create_connection((self.host, 443), timeout=3).close()
                return
            except OSError:
                time.sleep(3)
        raise TimeoutError("iLO2 non ha ripreso a rispondere dopo il reset")

    def _on_console_disconnected(self, reason):
        self.log(f"Disconnesso: {reason}")
        self.set_console_state("disconnected", reason)

    def _connect_worker(self):
        self.set_console_state("connecting")
        try:
            # Release whatever session this object was already holding first
            # -- start_console_thread() can run again (manual reconnect, a
            # retry after an error) while an old cookie is still sitting in
            # self.session, and iLO2's web-UI session pool is tiny (~2-4
            # slots, see SessionExhaustedError) so leaking one every time
            # this runs empties it fast. Best-effort: logout() never raises.
            self.session.logout()
            self.log("Login sulla pagina web dell'iLO2...")
            try:
                self.session.login()
            except SessionExhaustedError:
                # Don't auto-reset iLO2's management processor: that's a
                # disruptive action (it reboots the iLO2 controller, ~30-60s
                # unreachable, kills any other active session too) and
                # should only happen with the user's explicit OK -- see
                # "confirm_reset" below.
                self.log("Pool sessioni iLO2 esaurito (NONEAVAILABLE).")
                self.set_console_state("session_exhausted")
                return
            self.log(f"Login ok, sessione: {self.session.session_cookie}")

            # fetch_console_params() only arms the KVM port for a short,
            # single-digit-second window -- a connect landing just outside
            # it gets ConnectionRefusedError, which isn't a real failure,
            # just redo login+params fresh (see DEVELOPMENT.md).
            last_err = None
            for attempt in range(1, 4):
                try:
                    self.log("Richiesta pagina Remote Console (arma la porta KVM)...")
                    params = self.session.fetch_console_params()
                    safe = {k: v for k, v in params.items() if k not in ("infob", "infoc")}
                    self.log(f"Parametri console: {safe}")

                    self.console = IloConsole(self.host, params, debug=True, log_fn=self.log)
                    self.console.on_frame_block = self.fb.paste_block
                    self.console.on_video_size = lambda w, h: (
                        self.fb.resize(w, h), self.log(f"Dimensione video: {w}x{h}"))
                    self.console.on_status_text = lambda field, text: self.log(f"stato[{field}]: {text}")
                    self.console.on_disconnected = self._on_console_disconnected

                    self.log(f"Apertura socket KVM sulla porta {self.console.port}...")
                    self.console.connect()
                    self.log("Connesso, in attesa del flusso video...")
                    self.set_console_state("connected")
                    return
                except ConnectionRefusedError as e:
                    last_err = e
                    self.log(f"Porta KVM non armata (tentativo {attempt}/3), rifaccio login e riprovo...")
                    if attempt < 3:
                        time.sleep(1.5)
                        self.session.logout()
                        self.session.login()
            raise last_err
        except Exception as e:
            self.log(f"ERRORE connessione console: {e!r}")
            self.set_console_state("error", str(e))
        finally:
            self._connecting.release()

    async def _confirmed_reset(self, loop):
        """Only reachable via an explicit "confirm_reset" message from a
        client, after the user has been shown what it does (see
        web/index.html's session_exhausted handling)."""
        self.log("Reset del management processor confermato dall'utente, invio RESET_RIB...")
        try:
            await loop.run_in_executor(None, self.session.reset_management_processor)
            self.log("Reset inviato, attendo che iLO2 torni raggiungibile...")
            await loop.run_in_executor(None, self._wait_for_ilo)
            self.log("iLO2 di nuovo raggiungibile.")
        except Exception as e:
            self.log(f"Reset FALLITO: {e!r}")
            self.set_console_state("error", str(e))
            return
        self.start_console_thread()

    # ---- power/UID controls (blocking RIBCL calls, run off the event loop) --
    async def _run_action(self, loop, fn, label):
        self.log(f"{label}: invio comando RIBCL...")
        try:
            await loop.run_in_executor(None, fn)
            self.log(f"{label}: ok")
            # refresh status right away instead of waiting up to
            # STATUS_POLL_INTERVAL for the UI to catch up
            status = await loop.run_in_executor(None, self._poll_status)
            if status is not None:
                self._last_status = status
                await self._broadcast(json.dumps(status))
        except Exception as e:
            self.log(f"{label}: FALLITO: {e!r}")

    # ---- status polling (power/UID/health -- RIBCL, independent of the
    # console/web-session state, so it works even before "Avvia Console") --
    def _poll_status(self):
        """Runs in a worker thread. Returns None (and logs) on failure so a
        transient RIBCL hiccup doesn't kill the polling loop."""
        try:
            health = self.session.get_embedded_health()
            return {
                "type": "status",
                "power": self.session.get_power_status(),
                "uid": self.session.get_uid_status(),
                **health,
            }
        except Exception as e:
            self.log(f"Poll stato iLO2 fallito: {e!r}")
            return None

    async def _status_poll_loop(self):
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(None, self.session.get_fw_version)
            name = await loop.run_in_executor(None, self.session.get_server_name)
            self._server_info = {"type": "info", "server_name": name,
                                  "auth_enabled": self.auth.enabled, **info}
            await self._broadcast(json.dumps(self._server_info))
        except Exception as e:
            self.log(f"Recupero info server fallito: {e!r}")
        while True:
            status = await loop.run_in_executor(None, self._poll_status)
            if status is not None:
                self._last_status = status
                await self._broadcast(json.dumps(status))
            await asyncio.sleep(STATUS_POLL_INTERVAL)

    # ---- websocket handling ----------------------------------------
    def _check_ws_auth(self, connection, request):
        """`process_request` hook: rejects the handshake outright (HTTP 401,
        connection never upgrades) for anyone without a valid session
        cookie. Login gates the HTML page too, but that's a separate port
        -- without this, the WS data/control channel would stay wide open
        even with a login screen in front of the page."""
        if not self.auth.enabled:
            return None
        cookies = parse_cookies(request.headers.get("Cookie"))
        if self.sessions.validate(cookies.get(SESSION_COOKIE)):
            return None
        return connection.respond(401, "Unauthorized\n")

    async def _ws_handler(self, websocket):
        # Send this client its keyframe *before* it's in self.clients, so
        # the broadcast loop can never interleave a dirty-diff (computed
        # against state this client hasn't seen yet) ahead of it.
        full = self.fb.full_snapshot()
        if full is not None:
            await websocket.send(_pack_frame(*full))
        self.clients.add(websocket)
        self.log(f"Client web connesso ({len(self.clients)} totali)")
        if self._server_info is not None:
            await websocket.send(json.dumps(self._server_info))
        if self._last_status is not None:
            await websocket.send(json.dumps(self._last_status))
        await websocket.send(json.dumps(self.console_state))
        loop = asyncio.get_running_loop()
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                await self._handle_message(msg, loop)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            self.log(f"Client web disconnesso ({len(self.clients)} totali)")

    async def _handle_message(self, msg, loop):
        mtype = msg.get("type")
        c = self.console
        if mtype == "key" and c:
            data = msg.get("data")
            if isinstance(data, list):
                c.send_key_bytes(bytes(data))
        elif mtype == "mousemove" and c:
            c.send_mouse_move(int(msg.get("dx", 0)), int(msg.get("dy", 0)))
        elif mtype == "mousedown" and c:
            c.send_mouse_press(self._button_code(msg.get("button")))
        elif mtype == "mouseup" and c:
            c.send_mouse_release(self._button_code(msg.get("button")))
        elif mtype == "refresh" and c:
            c.refresh_screen()
        elif mtype == "force_full_frame":
            # Manual escape hatch: dirty-rect diffing means a client that
            # somehow missed/misapplied one update (a decode race, a dropped
            # binary frame) just keeps compounding it forever, since every
            # later diff is only computed against server-side state, not
            # against what that client actually has on screen. This resends
            # the whole current frame, independent of the diff stream, to
            # every connected client (matches the shared-viewing model --
            # anyone's screen can get stuck, not just the one who clicks).
            snap = self.fb.full_snapshot()
            if snap is not None:
                await self._broadcast(_pack_frame(*snap))
        elif mtype == "cad" and c:
            c.send_ctrl_alt_del()
        elif mtype == "start_console":
            self.start_console_thread()
        elif mtype == "confirm_reset":
            asyncio.create_task(self._confirmed_reset(loop))
        elif mtype == "power":
            action = msg.get("action")
            fn, label = {
                "on": (self.session.power_on, "Power On"),
                "off": (self.session.power_off, "Power Off"),
                "off_hard": (self.session.hold_power_button, "Power Off (hold)"),
                "reset": (self.session.warm_boot, "Reset"),
            }.get(action, (None, None))
            if fn:
                asyncio.create_task(self._run_action(loop, fn, label))
        elif mtype == "uid":
            action = msg.get("action")
            fn, label = {
                "on": (self.session.uid_on, "UID On"),
                "off": (self.session.uid_off, "UID Off"),
            }.get(action, (None, None))
            if fn:
                asyncio.create_task(self._run_action(loop, fn, label))

    @staticmethod
    def _button_code(name):
        return {"left": _MOUSE_LEFT, "middle": _MOUSE_CENTER, "right": _MOUSE_RIGHT}.get(name, _MOUSE_LEFT)

    async def _broadcast_loop(self):
        while True:
            await asyncio.sleep(0.1)
            if self.clients:
                update = self.fb.take_update()
                if update is not None:
                    await self._broadcast(_pack_frame(*update))
            # drain queued log/state events regardless of frame changes
            events = []
            while True:
                try:
                    events.append(self._event_queue.get_nowait())
                except queue.Empty:
                    break
            for event in events:
                await self._broadcast(json.dumps(event))

    async def _broadcast(self, payload):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(payload)
            except websockets.ConnectionClosed:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ---- HTTP static file server (plain thread, not asyncio) -----------
    def _start_http_server(self):
        handler = functools.partial(
            _AuthenticatedHandler, directory=str(WEB_DIR),
            auth=self.auth, sessions=self.sessions, rate_limiter=self.rate_limiter)
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.log(f"Frontend web su http://localhost:{self.http_port}/")
        if self.auth.enabled:
            self.log("Autenticazione attiva (WEBAPP_USER/WEBAPP_PASSWORD impostati).")
        else:
            self.log("ATTENZIONE: nessuna autenticazione attiva (WEBAPP_USER/"
                      "WEBAPP_PASSWORD non impostati nell'.env) -- non esporre "
                      "questa porta su internet così com'è.")

    async def run(self):
        self._start_http_server()
        self.log(f"WebSocket su ws://localhost:{self.ws_port}/")
        async with websockets.serve(self._ws_handler, "0.0.0.0", self.ws_port,
                                     process_request=self._check_ws_auth):
            self.start_console_thread()
            asyncio.create_task(self._status_poll_loop())
            await self._broadcast_loop()


def main(host, username, password, http_port=8080, ws_port=8765, ilo_port=443):
    server = WebServer(host, username, password, http_port, ws_port, ilo_port)
    # `docker stop`/a container restart sends SIGTERM, not SIGINT -- without
    # this it kills the process outright and skips the finally below (only
    # an interactive Ctrl-C would trigger it), leaking a session slot on
    # every deploy/restart. default_int_handler raises KeyboardInterrupt,
    # same as SIGINT, so it goes through the same shutdown path.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(server.run())
    finally:
        # Release our web-UI session slot on shutdown too (Ctrl-C, container
        # restart/redeploy) -- otherwise every restart leaks one until iLO2
        # times it out on its own, same problem _connect_worker's logout()
        # avoids on reconnect.
        server.session.logout()
