# Architecture

## Directory layout

```
ilo2/
  legacy_tls.py    HTTP(S) client that can actually talk to iLO2's TLS 1.0 web server
  session.py       Web UI login, Remote Console param fetch, RIBCL power/health/UID
  crypto.py        RC4 keystream (keyed via MD5), matching the applet's scheme
  dvc.py           The DVC video codec decoder (the big one -- see PROTOCOL.md)
  console.py       Owns the port-23 KVM socket: auth handshake, encrypt/decrypt,
                   wires decoded video + keyboard/mouse into/out of dvc.py
  framebuffer.py   Thread-safe live screen image, decoupled from any renderer
  auth.py          Session-cookie login, rate limiting -- used by webserver.py only
  dotenv.py        Tiny .env loader shared by main.py and webmain.py
  gui.py           Tkinter client (native window)
  webserver.py     WebSocket + HTTP server (browser client), status polling,
                   console connection lifecycle, auth wiring
web/
  index.html       Browser frontend: dashboard (power/UID/sensors), canvas console,
                   PWA shell
  login.html       Login page (only reachable/relevant when auth is enabled)
  manifest.json    PWA manifest (installable "Add to Home Screen")
  sw.js            Service worker: network-first, offline app-shell fallback only
  icons/           PWA icons
main.py            Entry point for the Tkinter client
webmain.py         Entry point for the web client
Dockerfile,
docker-compose.yml Container packaging for the web client; config comes in via
                   environment variables (see .env.example), nothing baked into
                   the image
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
`warm_boot()`, `cold_boot()`, `get_power_status()`,
`reset_management_processor()`, `get_uid_status()`/`uid_on()`/`uid_off()`,
`get_embedded_health()` (fans/temperatures/power supplies), and
`get_fw_version()`/`get_server_name()` -- all plain RIBCL calls, independent
of whether a console session exists. `webserver.py` polls the health/power/
UID ones on a timer, RIBCL round-trip time permitting (see the `ribcl_raw`
note below).

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
responses (`_response_complete` / `_chunked_body_complete`), and separately
for `ribcl_raw` (a `quiet_period` after the first byte, since RIBCL's raw
framing has no Content-Length to key off) -- because iLO2 holds the TCP
connection open for several seconds after finishing a response instead of
closing promptly, and without this every request (HTTP *or* RIBCL) would
otherwise idle out the full socket timeout on every single call.

On the TLS handshake itself: modern OpenSSL (3.x) refuses iLO2's TLS 1.0
by default, and separately refuses its "unsafe legacy renegotiation"
(iLO2 predates RFC 5746). Both have to be re-enabled explicitly
(`@SECLEVEL=0` on the cipher string, `SSL_OP_LEGACY_SERVER_CONNECT`) or
the connection never gets established at all on a recent OpenSSL client.

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

## `ilo2/dotenv.py`

Just the `.env` loader (`KEY=VALUE` lines into `os.environ`, never
overriding what's already set). Split out of `main.py` specifically so
`webmain.py` doesn't have to import `main.py` -- which pulls in `gui.py`,
which imports Tkinter -- just to read a config file. That import chain used
to break the web-only entry point on any headless machine without Tkinter
installed, which defeats the point of it being the headless-friendly path.

## `ilo2/auth.py`

Only used by `webserver.py`, and only when `WEBAPP_USER`/`WEBAPP_PASSWORD`
are both set (unset by default -- LAN-only use needs none of this):

- `AuthConfig`: reads the two env vars, `check(user, password)` via
  `hmac.compare_digest` on *both* fields (not just the password) so a wrong
  username doesn't resolve faster than a wrong password would.
- `SessionStore`: random-token sessions (`secrets.token_urlsafe`) held in
  memory -- restarting the process logs everyone out, an acceptable trade
  for needing no persistence.
- `LoginRateLimiter`: per-IP lockout after repeated failed logins, also
  in-memory.
- Cookie helpers (`parse_cookies`, `session_cookie_header`,
  `clear_cookie_header`): the session cookie is `HttpOnly` + `SameSite=Strict`
  always, and `Secure` only when the request carried
  `X-Forwarded-Proto: https` -- see the TLS-reverse-proxy note in the
  top-level README before exposing this beyond a LAN.

## `ilo2/gui.py` (Tkinter client)

Straightforward, with one non-obvious bit: **all cross-thread UI updates go
through a `queue.Queue` drained by `root.after()` on the main thread**,
never `root.after()` called directly from a background thread. On macOS's
Aqua Tk build, `.after()` scheduled from a non-main thread can silently
no-op (this was found the hard way -- see DEVELOPMENT.md). Login, the
console's receiver thread, and RIBCL power calls all run off the main
thread, so this matters everywhere.

## `ilo2/webserver.py` + `web/` (web client)

`WebServer`:
- runs a `_AuthenticatedHandler` (subclasses `SimpleHTTPRequestHandler`)
  serving `web/` as static files, gating `/` and `/index.html` behind the
  session cookie when auth is enabled (see `auth.py`), plus `/api/login`
  and `/api/logout`
- runs a `websockets` server; each connected browser gets pushed JPEG
  frame snapshots (only when the frame buffer's version has changed) and
  queued log/status/state events, and can send back JSON control messages.
  When auth is enabled, `process_request` (`_check_ws_auth`) rejects the
  handshake outright (HTTP 401, never upgrades) for a missing/invalid
  session cookie -- the WebSocket is a separate port from the HTML page,
  so it needs its own enforcement, not just a login screen in front of the
  page
- the actual iLO2 connection (`IloSession` + `IloConsole`) runs in a plain
  background thread, same shape as the Tkinter client's worker, bridged to
  the asyncio side only via the `FrameBuffer` and a thread-safe event queue
  -- no asyncio-specific code leaks into the protocol layer
- `_connecting` is a `threading.Lock` used as a non-blocking guard so a
  stray double "start console" (e.g. auto-start-on-boot racing a manual
  button click) can't fire two logins at once and stomp each other's
  one-time `sessionkey` (see DEVELOPMENT.md)
- a separate `_status_poll_loop` task polls power/UID/health on a timer,
  independent of console state, and broadcasts the result -- works even
  before "Avvia console" is ever clicked
- the console connection is a small explicit state machine
  (`idle`/`connecting`/`connected`/`disconnected`/`error`/
  `session_exhausted`), broadcast as `console_state` events so the UI can
  show *why* the video is blank instead of just... being blank. A
  `ConnectionRefusedError` from missing iLO2's narrow KVM-port arming
  window is retried automatically (redo login + params, up to 3 times --
  see DEVELOPMENT.md); `SessionExhaustedError` (the web-UI session pool is
  full) is *not* auto-recovered -- it puts the state machine into
  `session_exhausted` and waits for an explicit `confirm_reset` message
  from the client, because the fix (`RESET_RIB`) reboots the iLO2
  controller and kills any other active session too

`web/index.html`: a dashboard (power controls, UID toggle, health sensor
cards) around the same console `<canvas>` as before, installable as a PWA
(`manifest.json` + `sw.js`, the latter network-first so it doesn't end up
serving a stale cached copy of an actively-changing page) and built
mobile-first:
- pointer handling is unified (mouse vs. touch) via the Pointer Events API;
  one finger/mouse drags the remote cursor, a second finger switches to a
  local (never sent to the remote end) pinch-zoom/pan on the canvas via
  CSS transform
- an on-screen-keyboard fallback (`#mobileKbInput`, a 1x1 offscreen input)
  for typing on touch devices, driven by `beforeinput`/`compositionend`
  rather than `keydown` (mobile IMEs often don't fire useful
  `KeyboardEvent.code`), with the field force-cleared after every event so
  a mobile IME's own autocorrect/prediction can't quietly mutate what gets
  sent
- keyboard mapping prefers `KeyboardEvent.key` (already resolved against
  the user's actual layout) over a hardcoded US-QWERTY position table
  keyed by `KeyboardEvent.code` -- the table is a fallback now, not the
  primary path; on a non-US layout it used to send confidently wrong
  (sometimes swapped) punctuation, since `.code` is purely positional and
  layouts disagree heavily on symbol placement

Message shapes, client → server (JSON text frames):

```
{"type": "start_console"}
{"type": "confirm_reset"}
{"type": "key", "data": [<bytes>]}
{"type": "mousemove", "dx": <int>, "dy": <int>}
{"type": "mousedown"|"mouseup", "button": "left"|"middle"|"right"}
{"type": "refresh"}
{"type": "cad"}
{"type": "power", "action": "on"|"off"|"off_hard"|"reset"}
{"type": "uid", "action": "on"|"off"}
```

Server → client: binary frames are raw JPEG bytes; text frames are JSON
with a `type` of `log`, `info` (server name/firmware, sent once), `status`
(power/UID/health, polled), or `console_state`
(`idle`/`connecting`/`connected`/`disconnected`/`error`/
`session_exhausted`, plus an optional `detail`).
