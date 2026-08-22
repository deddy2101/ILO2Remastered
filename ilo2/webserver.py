"""Serves the iLO2 remote console over the web instead of a native window:
a WebSocket pushes JPEG frame snapshots + log lines to any number of
browser clients and accepts keyboard/mouse/power commands back, and a
plain HTTP server serves the static frontend page. Nothing here is tied to
Tkinter or to any single "renderer" -- point any client (a browser tab, a
future HLS transcoder, a diagnostics dashboard) at the same WebSocket.
"""
import asyncio
import functools
import http.server
import json
import queue
import threading
from pathlib import Path

import websockets

from .console import IloConsole
from .framebuffer import FrameBuffer
from .session import IloSession

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_MOUSE_LEFT = 4
_MOUSE_CENTER = 2
_MOUSE_RIGHT = 1


class WebServer:
    def __init__(self, host, username, password, http_port=8080, ws_port=8765):
        self.host = host
        self.username = username
        self.password = password
        self.http_port = http_port
        self.ws_port = ws_port

        self.fb = FrameBuffer()
        self.session = IloSession(host, username, password)
        self.console = None
        self.clients = set()
        self._log_queue = queue.Queue()
        self._last_sent_version = -1
        self._connecting = threading.Lock()

    # ---- logging (thread-safe: just queues, the asyncio side drains it) --
    def log(self, msg):
        print(msg, flush=True)
        self._log_queue.put(str(msg))

    # ---- console connection (blocking, runs in its own thread) ----------
    def start_console_thread(self):
        if not self._connecting.acquire(blocking=False):
            self.log("Connessione console già in corso, ignoro la richiesta duplicata.")
            return
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            self.log("Login sulla pagina web dell'iLO2...")
            self.session.login()
            self.log(f"Login ok, sessione: {self.session.session_cookie}")

            self.log("Richiesta pagina Remote Console (arma la porta KVM)...")
            params = self.session.fetch_console_params()
            safe = {k: v for k, v in params.items() if k not in ("infob", "infoc")}
            self.log(f"Parametri console: {safe}")

            self.console = IloConsole(self.host, params, debug=True, log_fn=self.log)
            self.console.on_frame_block = self.fb.paste_block
            self.console.on_video_size = lambda w, h: (self.fb.resize(w, h), self.log(f"Dimensione video: {w}x{h}"))
            self.console.on_status_text = lambda field, text: self.log(f"stato[{field}]: {text}")
            self.console.on_disconnected = lambda reason: self.log(f"Disconnesso: {reason}")

            self.log(f"Apertura socket KVM sulla porta {self.console.port}...")
            self.console.connect()
            self.log("Connesso, in attesa del flusso video...")
        except Exception as e:
            self.log(f"ERRORE connessione console: {e!r}")
        finally:
            self._connecting.release()

    # ---- power controls (blocking RIBCL calls, run off the event loop) --
    async def _run_power(self, loop, fn, label):
        self.log(f"{label}: invio comando RIBCL...")
        try:
            await loop.run_in_executor(None, fn)
            self.log(f"{label}: ok")
        except Exception as e:
            self.log(f"{label}: FALLITO: {e!r}")

    # ---- websocket handling ----------------------------------------
    async def _ws_handler(self, websocket):
        self.clients.add(websocket)
        self.log(f"Client web connesso ({len(self.clients)} totali)")
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
        elif mtype == "cad" and c:
            c.send_ctrl_alt_del()
        elif mtype == "start_console":
            self.start_console_thread()
        elif mtype == "power":
            action = msg.get("action")
            fn, label = {
                "on": (self.session.power_on, "Power On"),
                "off": (self.session.power_off, "Power Off"),
                "off_hard": (self.session.hold_power_button, "Power Off (hold)"),
                "reset": (self.session.warm_boot, "Reset"),
            }.get(action, (None, None))
            if fn:
                asyncio.create_task(self._run_power(loop, fn, label))

    @staticmethod
    def _button_code(name):
        return {"left": _MOUSE_LEFT, "middle": _MOUSE_CENTER, "right": _MOUSE_RIGHT}.get(name, _MOUSE_LEFT)

    async def _broadcast_loop(self):
        while True:
            await asyncio.sleep(0.1)
            if not self.clients:
                continue
            v = self.fb.version
            if v != self._last_sent_version:
                self._last_sent_version = v
                data = self.fb.jpeg_snapshot()
                if data:
                    await self._broadcast(data)
            # drain queued log lines regardless of frame changes
            lines = []
            while True:
                try:
                    lines.append(self._log_queue.get_nowait())
                except queue.Empty:
                    break
            for line in lines:
                await self._broadcast(json.dumps({"type": "log", "text": line}))

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
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", self.http_port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.log(f"Frontend web su http://localhost:{self.http_port}/")

    async def run(self):
        self._start_http_server()
        self.log(f"WebSocket su ws://localhost:{self.ws_port}/")
        async with websockets.serve(self._ws_handler, "0.0.0.0", self.ws_port):
            self.start_console_thread()
            await self._broadcast_loop()


def main(host, username, password, http_port=8080, ws_port=8765):
    server = WebServer(host, username, password, http_port, ws_port)
    asyncio.run(server.run())
