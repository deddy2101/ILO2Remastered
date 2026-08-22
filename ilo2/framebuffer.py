"""Thread-safe live framebuffer, decoupled from any particular renderer.

The DVC decoder runs on the console's own receiver thread and calls back
into this as blocks arrive. Consumers (the websocket broadcaster, a test
script) just take an update whenever they want one -- no assumption about
who's reading it or how often.

Change tracking is a single dirty *bounding box*, not a list of individual
dirty tiles: for a mostly-static text console (the common case) a handful
of scattered 16x16 blocks changing still usually clusters into one small
rectangle, and one rectangle keeps both the bookkeeping here and the
wire format in webserver.py trivial. A pathological frame where dirty
blocks are scattered corner-to-corner degrades to sending the whole
frame -- no worse than before this existed, just no better either.
"""
import io
import threading

from PIL import Image


class FrameBuffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._image = None
        self._version = 0  # bumped on every change, mostly informational now
        self._dirty = None  # [x0, y0, x1, y1] (x1/y1 exclusive), or None
        self._needs_full = False  # set on resize(): dirty-rect tracking is moot,
        # every consumer's prior state is now wrong regardless of what changed

    def resize(self, w, h):
        with self._lock:
            # A same-size resize is a no-op for us but not necessarily rare
            # upstream (iLO2 can re-announce the mode, and a DVC decoder
            # desync/resync can also misfire this): recreating the image
            # would wipe every block painted so far for no reason, which
            # looks exactly like "the video blanks out and only the parts
            # that get redrawn afterward reappear."
            if self._image is not None and self._image.size == (w, h):
                return
            self._image = Image.new("RGB", (w, h), "black")
            self._version += 1
            self._dirty = None
            self._needs_full = True

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
            x1, y1 = min(x + 16, w), min(y + 16, h)
            if self._dirty is None:
                self._dirty = [x, y, x1, y1]
            else:
                d = self._dirty
                d[0] = min(d[0], x); d[1] = min(d[1], y)
                d[2] = max(d[2], x1); d[3] = max(d[3], y1)

    @property
    def version(self):
        with self._lock:
            return self._version

    def size(self):
        with self._lock:
            return self._image.size if self._image else (0, 0)

    @staticmethod
    def _encode(im, quality):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def take_update(self, quality=92):
        """For the periodic broadcast loop: one consumer, consumes (clears)
        whatever changed since the last call. Returns (x, y, w, h, jpeg
        bytes) -- a full frame (x=y=0) right after a resize, otherwise a
        crop of just the accumulated dirty rectangle -- or None if nothing
        changed since the last call."""
        with self._lock:
            if self._image is None:
                return None
            if self._needs_full:
                self._needs_full = False
                self._dirty = None
                im, x, y = self._image.copy(), 0, 0
            elif self._dirty is not None:
                x0, y0, x1, y1 = self._dirty
                self._dirty = None
                im, x, y = self._image.crop((x0, y0, x1, y1)), x0, y0
            else:
                return None
            w, h = im.size
        return x, y, w, h, self._encode(im, quality)

    def full_snapshot(self, quality=92):
        """For a newly-joined client, independent of take_update()'s dirty
        tracking: always the whole current frame, as (x=0, y=0, w, h, jpeg
        bytes), or None if there's no video yet."""
        with self._lock:
            if self._image is None:
                return None
            im = self._image.copy()
        w, h = im.size
        return 0, 0, w, h, self._encode(im, quality)
