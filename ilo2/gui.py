"""Tkinter front-end: shows the remote video, forwards keyboard/mouse input,
and exposes power controls (via RIBCL, independent of the KVM socket).

Everything that happens is written to the on-screen log panel (not just the
terminal) since the console session is armed only briefly and it's the
easiest way to see what's actually going on without a separate window.
"""
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime

from PIL import Image, ImageTk

from .console import IloConsole
from .session import IloSession

# Keysym -> DVC escape sequence, ported from cim.translate_special_key().
_SPECIAL_KEYS = {
    "Escape": b"\x1b",
    "Tab": b"\t",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "Prior": b"\x1b[I",   # Page Up
    "Next": b"\x1b[G",    # Page Down
    "Insert": b"\x1b[L",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Left": b"\x1b[D",
    "Right": b"\x1b[C",
    "F1": b"\x1b[M", "F2": b"\x1b[N", "F3": b"\x1b[O", "F4": b"\x1b[P",
    "F5": b"\x1b[Q", "F6": b"\x1b[R", "F7": b"\x1b[S", "F8": b"\x1b[T",
    "F9": b"\x1b[U", "F10": b"\x1b[V", "F11": b"\x1b[W", "F12": b"\x1b[X",
}

_MOUSE_LEFT = 4
_MOUSE_CENTER = 2
_MOUSE_RIGHT = 1


class IloApp:
    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password

        self.root = tk.Tk()
        self.root.title(f"iLO2 Remote Console - {host}")
        self.root.geometry("1050x950")

        # Grid layout instead of pack for the top-level rows: pack's
        # "expand=True fill=both" on the canvas together with a dynamically
        # resized canvas (once the real video resolution is known) could
        # make it overrun the log panel's slice of the window. Grid with
        # explicit row weights keeps the canvas as the only row that grows.
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=0)  # toolbar
        self.root.rowconfigure(1, weight=1)  # video canvas (grows)
        self.root.rowconfigure(2, weight=0)  # log panel (fixed)

        toolbar = tk.Frame(self.root)
        toolbar.grid(row=0, column=0, sticky="ew")
        self.start_btn = tk.Button(toolbar, text="Avvia Console", command=self._start_console)
        self.start_btn.pack(side="left")
        tk.Button(toolbar, text="Refresh", command=self._refresh).pack(side="left")
        tk.Button(toolbar, text="Ctrl+Alt+Del", command=self._cad).pack(side="left")
        tk.Button(toolbar, text="Power On", command=self._power_on).pack(side="left", padx=(20, 0))
        tk.Button(toolbar, text="Reset", command=self._reset).pack(side="left")
        tk.Button(toolbar, text="Power Off (graceful)", command=self._power_off).pack(side="left")
        tk.Button(toolbar, text="Power Off (hold)", command=self._power_off_hard).pack(side="left")

        # Video canvas sits inside its own frame with a scrollbar in case
        # the real remote resolution is bigger than the window.
        canvas_frame = tk.Frame(self.root)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, width=800, height=600, bg="black")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self._image = None
        self._photo = None
        self._tk_image_id = None

        log_frame = tk.Frame(self.root)
        log_frame.grid(row=2, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)
        tk.Label(log_frame, text="Log:", anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew")
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                                 bg="black", fg="#33ff33", font=("Menlo", 11))
        self.log_text.grid(row=1, column=0, sticky="ew")
        log_scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.config(yscrollcommand=log_scroll.set)

        self.canvas.bind("<Key>", self._on_key)
        self.canvas.bind("<Button-1>", lambda e: self._mouse_button(_MOUSE_LEFT, True))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._mouse_button(_MOUSE_LEFT, False))
        self.canvas.bind("<Button-2>", lambda e: self._mouse_button(_MOUSE_CENTER, True))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._mouse_button(_MOUSE_CENTER, False))
        self.canvas.bind("<Button-3>", lambda e: self._mouse_button(_MOUSE_RIGHT, True))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._mouse_button(_MOUSE_RIGHT, False))
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.focus_set()

        self._last_mouse = None
        self.console = None
        self.session = IloSession(host, username, password)
        self._pending_blocks = []
        self._block_total = 0
        self._blocks_lock = threading.Lock()
        self._last_debug_dump = 0.0

        # Background threads (login, socket I/O, RIBCL calls) never touch
        # Tk widgets directly -- Tkinter on macOS's Aqua backend does not
        # reliably handle cross-thread calls, including .after() scheduled
        # from a non-main thread (it can silently no-op). Everything that
        # needs to update the UI is queued here and drained on the main
        # thread's own event loop tick instead.
        self._ui_queue = queue.Queue()
        self.root.after(50, self._pump_ui_queue)
        self.root.after(50, self._flush_blocks)

        self._log(f"Pronto. Host={host} user={username}. Premi 'Avvia Console' per connetterti al video.")

    # ---- thread-safe UI updates -------------------------------------
    def _ui_call(self, fn):
        self._ui_queue.put(fn)

    def _pump_ui_queue(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()
        self.root.after(50, self._pump_ui_queue)

    def _log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        print(line, end="")

        def append():
            self.log_text.config(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self._ui_call(append)

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        self.root.mainloop()

    def _start_console(self):
        self.start_btn.config(state="disabled", text="Connessione...")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            self._log("Login sulla pagina web dell'iLO2...")
            self.session.login()
            self._log(f"Login ok, sessione: {self.session.session_cookie}")

            self._log("Richiesta pagina Remote Console (arma la porta KVM)...")
            params = self.session.fetch_console_params()
            safe_params = {k: v for k, v in params.items() if k not in ("infob", "infoc")}
            self._log(f"Parametri console: {safe_params}, cifratura={'on' if params.get('infoa')=='1' else 'off'}")

            self.console = IloConsole(self.host, params, debug=True, log_fn=self._log)
            self.console.on_frame_block = self._queue_block
            self.console.on_video_size = self._on_video_size
            self.console.on_status_text = self._on_status_text
            self.console.on_disconnected = self._on_disconnected

            self._log(f"Apertura socket KVM sulla porta {self.console.port}...")
            self.console.connect()
            self._log("Connesso. In attesa del flusso video (premi Refresh se non arriva nulla)...")
            self._ui_call(lambda: self.start_btn.config(text="Connesso"))
        except Exception as e:
            self._log(f"ERRORE connessione: {e!r}")
            self._log(traceback.format_exc())
            self._ui_call(lambda: self.start_btn.config(state="normal", text="Riprova Console"))

    def _on_disconnected(self, reason):
        self._log(f"Disconnesso: {reason}")
        self._ui_call(lambda: self.start_btn.config(state="normal", text="Riavvia Console"))

    def _on_status_text(self, field, text):
        self._log(f"stato[{field}]: {text}")

    # ---- video rendering -------------------------------------------
    def _on_video_size(self, w, h):
        self._log(f"Dimensione video rilevata: {w}x{h}")

        def apply():
            self._image = Image.new("RGB", (w, h), "black")
            self.canvas.config(width=min(w, 1600), height=min(h, 1000))
            self._redraw_full()
        self._ui_call(apply)

    def _queue_block(self, x, y, pixels):
        with self._blocks_lock:
            self._pending_blocks.append((x, y, pixels))
            self._block_total += 1

    def _flush_blocks(self):
        with self._blocks_lock:
            blocks = self._pending_blocks
            self._pending_blocks = []
            total = self._block_total
        if blocks:
            if self._image is None:
                if total <= len(blocks):
                    self._log(f"attenzione: {len(blocks)} blocchi video ricevuti ma nessuna dimensione video nota ancora (manca on_resize)")
            else:
                for x, y, pixels in blocks:
                    self._paste_block(x, y, pixels)
                self._redraw_full()
                self._maybe_dump_debug_png()
            if total <= len(blocks) or total % 500 < len(blocks):
                self._log(f"blocchi video ricevuti finora: {total}")
        self.root.after(50, self._flush_blocks)

    def _paste_block(self, x, y, pixels):
        w, h = self._image.size
        tile = Image.new("RGB", (16, 16))
        px = tile.load()
        for i, val in enumerate(pixels):
            r = (val >> 16) & 0xFF
            g = (val >> 8) & 0xFF
            b = val & 0xFF
            px[i % 16, i // 16] = (r, g, b)
        if x < w and y < h:
            self._image.paste(tile, (x, y))

    def _maybe_dump_debug_png(self):
        now = time.time()
        if now - self._last_debug_dump < 2.0:
            return
        self._last_debug_dump = now
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_frame.png")
        try:
            self._image.save(path)
            extrema = self._image.convert("L").getextrema()
            self._log(f"debug: salvato {path} ({self._image.size[0]}x{self._image.size[1]}, min/max luminanza={extrema})")
        except Exception as e:
            self._log(f"debug: impossibile salvare screenshot: {e!r}")

    def _redraw_full(self):
        if self._image is None:
            return
        self._photo = ImageTk.PhotoImage(self._image)
        if self._tk_image_id is None:
            self._tk_image_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        else:
            self.canvas.itemconfig(self._tk_image_id, image=self._photo)

    # ---- input forwarding -----------------------------------------
    def _on_key(self, event):
        if not self.console:
            return
        keysym = event.keysym
        if keysym in _SPECIAL_KEYS:
            self.console.send_key_bytes(_SPECIAL_KEYS[keysym])
            return
        if keysym == "Return":
            self.console.send_key_bytes(b"\r")
            return
        if keysym == "BackSpace":
            self.console.send_key_bytes(b"\x08")
            return
        if keysym == "Delete":
            self.console.send_key_bytes(b"\x7f")
            return
        ch = event.char
        if ch:
            try:
                self.console.send_key_bytes(ch.encode("latin-1"))
            except UnicodeEncodeError:
                pass

    def _mouse_button(self, button, pressed):
        if not self.console:
            return
        if pressed:
            self.console.send_mouse_press(button)
        else:
            self.console.send_mouse_release(button)

    def _on_motion(self, event):
        if not self.console:
            return
        pos = (event.x, event.y)
        if self._last_mouse is not None:
            dx = pos[0] - self._last_mouse[0]
            dy = pos[1] - self._last_mouse[1]
            if dx or dy:
                self.console.send_mouse_move(dx, dy)
        self._last_mouse = pos

    # ---- toolbar actions -------------------------------------------
    def _refresh(self):
        if self.console:
            self._log("Invio richiesta di refresh schermo...")
            self.console.refresh_screen()
        else:
            self._log("Console non connessa: premi prima 'Avvia Console'.")

    def _cad(self):
        if self.console:
            self.console.send_ctrl_alt_del()

    def _run_power(self, fn, label):
        def worker():
            self._log(f"{label}: invio comando RIBCL...")
            try:
                resp = fn()
                self._log(f"{label}: ok")
            except Exception as e:
                self._log(f"{label}: FALLITO: {e!r}")
        threading.Thread(target=worker, daemon=True).start()

    def _power_on(self):
        self._run_power(self.session.power_on, "Power On")

    def _power_off(self):
        self._run_power(self.session.power_off, "Power Off")

    def _power_off_hard(self):
        self._run_power(self.session.hold_power_button, "Power Off (hold)")

    def _reset(self):
        self._run_power(self.session.warm_boot, "Reset")
