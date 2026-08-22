# Architecture

## Directory layout

```
ilo2/
  legacy_tls.py    HTTP(S) client that can actually talk to iLO2's TLS 1.0 web server
  session.py       Web UI login, Remote Console param fetch, RIBCL power control
  crypto.py        RC4 keystream (keyed via MD5), matching the applet's scheme
  dvc.py           The DVC video codec decoder (the big one -- see PROTOCOL.md)
  console.py       Owns the port-23 KVM socket: auth handshake, encrypt/decrypt,
                   wires decoded video + keyboard/mouse into/out of dvc.py
  framebuffer.py   Thread-safe live screen image, decoupled from any renderer
  gui.py           Tkinter client (native window)
  webserver.py     WebSocket + HTTP server (browser client)
web/
  index.html       Browser frontend: canvas + keyboard/mouse capture + log panel
main.py            Entry point for the Tkinter client
webmain.py         Entry point for the web client
docs/              This documentation
```

## Two clients, one backend

`ilo2/` has no UI code in it except `gui.py` (which is itself just one
consumer). Everything else -- login, crypto, video decode, KVM socket
handling -- is UI-agnostic. `main.py` (Tkinter) and `webmain.py`
(WebSocket) are two different front ends bolted onto the same backend;
adding a third (say, a proper HLS transcoder, or a CLI status tool) means
writing a new thin consumer, not touching the protocol code.

The shape both clients follow:

```
IloSession.login()                      -- web UI cookie auth
IloSession.fetch_console_params()       -- arms the port-23 listener, returns
                                           the RC4 keys + login ticket for it
IloConsole(host, params).connect()      -- opens the KVM socket, does the
                                           encrypted handshake, spins up a
                                           receiver thread
console.on_frame_block = ...            -- callback: (x, y, 16x16 pixel block)
console.on_video_size = ...             -- callback: (width, height) once known
console.send_key_bytes(...)             -- keyboard in
console.send_mouse_move/press/release() -- mouse in
```

`IloSession` also exposes `power_on()`, `power_off()`, `hold_power_button()`,
`warm_boot()`, `cold_boot()`, `get_power_status()` and
`reset_management_processor()` -- all plain RIBCL calls, independent of
whether a console session exists.

## `ilo2/legacy_tls.py`

A minimal hand-rolled HTTP/1.1 client, because `requests`/`urllib`'s
underlying `ssl` module can't be coaxed into iLO2's exact TLS requirements
easily enough for this to be worth fighting. See PROTOCOL.md for *why*
iLO2 needs special handling (spoiler: it's not just "TLS 1.0").

Two entry points:
- `raw_request(...)` / `request(...)`: normal HTTP GET, used for the web UI
  (login page, Remote Console frame page, downloading the applet jar during
  the original protocol reverse-engineering).
- `ribcl_raw(...)`: RIBCL's own framing (not HTTP at all -- see PROTOCOL.md),
  used for power control.

It also implements early-completion detection for chunked/Content-Length
responses (`_response_complete` / `_chunked_body_complete`), because iLO2
holds the TCP connection open for several seconds after finishing a
response instead of closing promptly -- without this every request would
otherwise idle out the full socket timeout.

## `ilo2/session.py`

`IloSession`:
- `login()`: reproduces the web UI's JS login flow (fetch a one-time
  `sessionkey`, build a cookie token, trade it for a real session cookie).
- `fetch_console_params()`: loads the Remote Console frame page and scrapes
  the `INFO*` applet `<PARAM>` values out of it (login ticket, RC4 keys,
  KVM port). This is also what tells iLO2 to open the port-23 listener --
  see the "arming" note in DEVELOPMENT.md.
- RIBCL power methods: each just builds a small RIBCL XML fragment and
  calls `legacy_tls.ribcl_raw`.

## `ilo2/crypto.py`

One class, `RC4`, implementing the exact (non-standard) key schedule the
applet uses: the real RC4 key is `MD5(seed || previous_key)`, not the seed
directly, and the console can ask both ends to rotate keys mid-session
(`update_key()`). See PROTOCOL.md for where this fits into the handshake.

## `ilo2/dvc.py`

The video codec decoder. This is a close, almost line-by-line port of the
decompiled `com.hp.ilo2.remcons.cim` state machine -- see PROTOCOL.md for
why it's written this way instead of "cleaned up", and for the state
machine's actual shape. `DvcDecoder.feed(byte)` is the only thing callers
need; everything else is callbacks (`on_block`, `on_resize`, `on_status`,
`on_refresh_request`, `on_seize`, `on_change_key`).

## `ilo2/console.py`

`IloConsole` owns the TCP socket to port 23 (or whatever `INFO6` says).
`connect()` sends the auth handshake (`build_login_string` + the RC4-framed
header, see PROTOCOL.md) and starts a receiver thread. That thread scans
incoming bytes for the `ESC [ R`/`ESC [ r` trigger that switches the stream
into DVC mode, RC4-decrypts each subsequent byte if encryption is on, and
feeds it to a `DvcDecoder` instance. Outgoing keyboard/mouse/control bytes
go through `transmit()`, which XORs them with the *same, continuously
advancing* RC4 keystream used for the login -- there is no per-message
re-keying.

## `ilo2/framebuffer.py`

`FrameBuffer` is a thread-safe `PIL.Image` with a version counter and a
`jpeg_snapshot()` method. It exists so `console.py` doesn't need to know or
care who's watching -- the Tkinter client polls its own copy of the pasted
blocks, the web server polls this shared one and re-encodes to JPEG on a
timer. Nothing about it is web- or Tkinter-specific.

## `ilo2/gui.py` (Tkinter client)

Straightforward, with one non-obvious bit: **all cross-thread UI updates go
through a `queue.Queue` drained by `root.after()` on the main thread**,
never `root.after()` called directly from a background thread. On macOS's
Aqua Tk build, `.after()` scheduled from a non-main thread can silently
no-op (this was found the hard way -- see DEVELOPMENT.md). Login, the
console's receiver thread, and RIBCL power calls all run off the main
thread, so this matters everywhere.

## `ilo2/webserver.py` + `web/index.html` (web client)

`WebServer`:
- runs a plain `http.server.ThreadingHTTPServer` serving `web/` as static
  files (just the one page)
- runs a `websockets` server; each connected browser gets pushed JPEG
  frame snapshots (only when the frame buffer's version has changed) and
  queued log lines, and can send back JSON control messages
- the actual iLO2 connection (`IloSession` + `IloConsole`) runs in a plain
  background thread, same shape as the Tkinter client's worker, bridged to
  the asyncio side only via the `FrameBuffer` and a thread-safe log queue
  -- no asyncio-specific code leaks into the protocol layer
- `_connecting` is a `threading.Lock` used as a non-blocking guard so a
  stray double "start console" (e.g. auto-start-on-boot racing a manual
  button click) can't fire two logins at once and stomp each other's
  one-time `sessionkey` (see DEVELOPMENT.md)

`web/index.html` is intentionally a single dependency-free file: a
`<canvas>` drawn from incoming JPEG blobs via `createImageBitmap`, keyboard
capture that maps `KeyboardEvent.code` (physical key, layout-independent)
through an explicit US-QWERTY table rather than trusting
`KeyboardEvent.key` (which is localized -- see DEVELOPMENT.md for why that
matters), mouse capture sending relative deltas, and a scrolling log panel
fed by the same WebSocket.

Message shapes, client → server (JSON text frames):

```
{"type": "start_console"}
{"type": "key", "data": [<bytes>]}
{"type": "mousemove", "dx": <int>, "dy": <int>}
{"type": "mousedown"|"mouseup", "button": "left"|"middle"|"right"}
{"type": "refresh"}
{"type": "cad"}
{"type": "power", "action": "on"|"off"|"off_hard"|"reset"}
```

Server → client: binary frames are raw JPEG bytes; text frames are
`{"type": "log", "text": "..."}`.
