"""Thread-safe live framebuffer, decoupled from any particular renderer.

The DVC decoder runs on the console's own receiver thread and calls back
into this as blocks arrive. Consumers (a websocket broadcaster, a Tkinter
canvas, a test script) just take a JPEG snapshot whenever they want one --
no assumption about who's reading it or how often.
"""
import io
import threading

from PIL import Image


class FrameBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._image = None
        self._version = 0  # bumped on every change, lets consumers skip unchanged snapshots

    def resize(self, w, h):
        with self._lock:
            self._image = Image.new("RGB", (w, h), "black")
            self._version += 1

    def paste_block(self, x, y, pixels):
        with self._lock:
            if self._image is None:
                return
            w, h = self._image.size
            if x >= w or y >= h:
                return
            tile = Image.new("RGB", (16, 16))
            px = tile.load()
            for i, val in enumerate(pixels):
                px[i % 16, i // 16] = ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF)
            self._image.paste(tile, (x, y))
            self._version += 1

    @property
    def version(self):
        with self._lock:
            return self._version

    def size(self):
        with self._lock:
            return self._image.size if self._image else (0, 0)

    def jpeg_snapshot(self, quality=80) -> bytes:
        with self._lock:
            if self._image is None:
                return b""
            im = self._image.copy()
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
