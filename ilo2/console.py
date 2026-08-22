"""HP iLO2 Remote Console (KVM) socket client.

Ported from com.hp.ilo2.remcons.{remcons,cim,telnet} in rc175p11.jar.

Wire protocol, in order:
  1. TCP connect to <host>:<INFO6> (usually 23; the iLO2 firmware only
     opens this listener for a short window after the web UI's Remote
     Console page has been loaded, i.e. after session.fetch_console_params()).
  2. If INFOA == "1": encryption is on. Send
         0xFF 0xC0 <key_index:4 bytes BE> <RC4(login_string)>
     where the RC4 keystream is seeded from INFOC (the *encrypt* key) via
     the key schedule in ilo2.crypto.RC4, and continues, uninterrupted,
     for every byte sent for the rest of the connection (it's one
     continuous keystream, not re-seeded per message).
     If INFOA != "1", the login_string is sent as plain bytes instead.
  3. Read the socket. Everything up to and including the 3-byte sequence
     ESC '[' 'R' (encrypted DVC stream) or ESC '[' 'r' (plaintext DVC
     stream) is scanned byte-by-byte and discarded (it's not video data).
     From the byte *after* that trigger, every byte belongs to the DVC
     video codec (ilo2.dvc.DvcDecoder) and, if the trigger was 'R', must
     first be XORed with the RC4 keystream seeded from INFOB (the
     *decrypt* key).
  4. Keyboard/mouse/control messages are sent the same way as step 2's
     login (continuing the same encrypt keystream): 0xFF followed by a
     command byte and parameters, e.g. 0xFF 0xD0 dx dy for a relative
     mouse move.
"""
import base64
import socket
import threading
import time

from .crypto import RC4
from .dvc import DvcDecoder


def _decode_login_ticket(info0: str) -> bytes:
    pad = (-len(info0)) % 4
    raw = base64.b64decode(info0 + "=" * pad)
    # The applet swaps decoded ':' bytes back to CR (server-side substitution
    # to keep the ticket safe inside an HTML attribute).
    fixed = bytes(0x0D if b == 0x3A else b for b in raw)
    return fixed


def build_login_string(params: dict) -> bytes:
    ticket = _decode_login_ticket(params["info0"])
    if ticket and not ticket.endswith(b"\r"):
        ticket += b"\r"
    login = ticket
    if params.get("info1") is not None:
        login = b"\x1b[4" + login
    login = b"\x1b[7\x1b[9" + login
    return login


def _hex16(s: str) -> bytes:
    b = bytes.fromhex(s)
    if len(b) != 16:
        raise ValueError(f"expected 16-byte hex key, got {len(b)} bytes")
    return b


class SessionSeized(Exception):
    """Another client took over the console session."""


class IloConsole:
    def __init__(self, host, params, port=None, debug=False, log_fn=print):
        self.host = host
        self.params = params
        self.port = port or int(params.get("info6") or 23)
        self.debug = debug
        self.log = log_fn

        self.encryption_enabled = params.get("infoa") == "1"
        self._rc4_encrypt = None
        self._rc4_decrypt = None
        if self.encryption_enabled:
            self._rc4_encrypt = RC4(_hex16(params["infoc"]))
            self._rc4_decrypt = RC4(_hex16(params["infob"]))
            self._key_index = int(params["infod"])
        self._sending_login = False

        self.sock = None
        self._recv_thread = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

        self.dvc_mode = False
        self.dvc_encryption = False
        self._esc_state = 0

        self.decoder = DvcDecoder(
            on_block=self._on_block,
            on_resize=self._on_resize,
            on_status=self._on_status,
            on_refresh_request=self.refresh_screen,
            on_seize=self._on_seize,
            on_change_key=self._on_change_key,
            debug=debug,
            log_fn=log_fn,
        )

        # UI-facing callbacks; gui.py overrides these.
        self.on_frame_block = lambda x, y, px: None
        self.on_video_size = lambda w, h: None
        self.on_status_text = lambda field, text: None
        self.on_disconnected = lambda reason: None

    # ---- connection lifecycle ------------------------------------------
    def connect(self, timeout=10):
        self.log(f"console: connecting to {self.host}:{self.port} ...")
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(1.0)
        self.log("console: TCP connected, sending login ticket"
                  + (" (encrypted)" if self.encryption_enabled else " (plaintext)"))
        login = build_login_string(self.params)
        self._send_login(login)
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        self.log("console: receiver thread started, waiting for DVC trigger...")

    def disconnect(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # ---- outgoing ------------------------------------------------------
    def _send_login(self, login: bytes):
        if not self.encryption_enabled:
            self._raw_send(login)
            return
        header = bytes([0xFF, 0xC0]) + self._key_index.to_bytes(4, "big", signed=False)
        encrypted = self._rc4_encrypt.xor(login)
        self._raw_send(header + encrypted)

    def transmit(self, data: bytes):
        if not self.encryption_enabled:
            self._raw_send(data)
            return
        self._raw_send(self._rc4_encrypt.xor(data))

    def _raw_send(self, data: bytes):
        if not self.sock or not data:
            return
        with self._send_lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.log(f"console: send error: {e!r}")

    def refresh_screen(self):
        self.transmit(b"\x1b[~")

    def send_keep_alive(self):
        self.transmit(b"\x1b[(")

    def send_auto_alive(self):
        self.transmit(b"\x1b[&")

    def send_ctrl_alt_del(self):
        self.transmit(b"\x1b[2\x1b[\x7f")

    def send_mouse_move(self, dx: int, dy: int):
        dx = max(-128, min(127, dx))
        dy = max(-128, min(127, dy))
        self.transmit(bytes([0xFF, 0xD0]) + dx.to_bytes(1, "big", signed=True) + dy.to_bytes(1, "big", signed=True))

    def send_mouse_press(self, button: int):
        self.transmit(bytes([0xFF, 0xD1, button & 0xFF]))

    def send_mouse_release(self, button: int):
        self.transmit(bytes([0xFF, 0xD2, button & 0xFF]))

    def send_mouse_click(self, button: int, count: int = 1):
        self.transmit(bytes([0xFF, 0xD3, button & 0xFF, count & 0xFF]))

    def send_key_bytes(self, data: bytes):
        self.transmit(data)

    # ---- incoming --------------------------------------------------
    def _recv_loop(self):
        buf = bytearray(4096)
        try:
            while not self._stop.is_set():
                try:
                    n = self.sock.recv_into(buf)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if n <= 0:
                    break
                self._process_incoming(buf[:n])
        finally:
            self.on_disconnected("socket closed")

    def _process_incoming(self, data: bytes):
        for b in data:
            if self.dvc_mode:
                if self.dvc_encryption:
                    b ^= self._rc4_decrypt.next_byte()
                try:
                    self.decoder.feed(b)
                except Exception as e:
                    self.log(f"console: dvc decode error: {e!r}")
                continue
            c = chr(b)
            if c == "\x1b":
                self._esc_state = 1
            elif self._esc_state == 1 and c == "[":
                self._esc_state = 2
            elif self._esc_state == 2 and c == "R":
                self.dvc_mode = True
                self.dvc_encryption = True
                self.log("console: DVC trigger seen (RC4-encrypted video stream starting)")
                self.on_status_text(1, "DVC Mode (RC4-128 bit)")
            elif self._esc_state == 2 and c == "r":
                self.dvc_mode = True
                self.dvc_encryption = False
                self.log("console: DVC trigger seen (plaintext video stream starting)")
                self.on_status_text(1, "DVC Mode (no encryption)")
            else:
                self._esc_state = 0

    # ---- decoder callbacks -----------------------------------------
    def _on_block(self, x, y, pixels):
        self.on_frame_block(x, y, pixels)

    def _on_resize(self, w, h):
        self.on_video_size(w, h)

    def _on_status(self, field, text):
        self.on_status_text(field, text)

    def _on_seize(self):
        self.on_disconnected("session seized by another client")
        self.disconnect()

    def _on_change_key(self):
        if self._rc4_encrypt:
            self._rc4_encrypt.update_key()
        if self._rc4_decrypt:
            self._rc4_decrypt.update_key()
