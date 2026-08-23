# Development notes

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # see the note below before skipping this
python3 -m pip install -r requirements.txt
cp .env.example .env
$EDITOR .env   # set ILO_HOST / ILO_USER / ILO_PASSWORD
```

**On macOS, use a real Python, not the system one.** `/usr/bin/python3`
(and Xcode's bundled Python) links against an ancient LibreSSL
(2.8.3 at last check) whose cipher-string parser doesn't understand the
`@SECLEVEL=0` suffix `legacy_tls.py` needs for the TLS handshake (see
below) -- `ssl.SSLContext.set_ciphers()` raises `SSLError: No cipher can
be selected`. The code falls back gracefully when that happens, but the
LibreSSL build this was tested against is old enough it may have other
undiscovered quirks too, so prefer a Homebrew (or pyenv, or python.org)
Python for `.venv` -- `python3 -m ssl` (or `python3 -c "import ssl;
print(ssl.OPENSSL_VERSION)"`) should say `OpenSSL 3.x`, not `LibreSSL`.

```bash
python3 webmain.py            # http://localhost:8080/
```

Reads `.env` automatically, or takes `--host`/`--user`/`--password`/etc. on
the command line, which override `.env`.

## Known iLO2-side quirks

These aren't bugs in this codebase -- they're real behaviors of the actual
hardware/firmware that cost real debugging time, documented here so they
don't get re-discovered from scratch.

### `sessionkey="NONEAVAILABLE"` / login suddenly starts failing

iLO2 has a **very small pool of concurrent web-UI sessions** (observed:
looked like roughly 2-4). Each successful `IloSession.login()` holds a slot
until it times out server-side (the applet's own default inactivity
timeout was 900s / 15 min, `remcons.SESSION_TIMEOUT_DEFAULT`), or until
`IloSession.logout()` (`GET /logout.htm` with the session cookie -- found
by grepping the authenticated frameset for a "Log out" link, there's no
mention of it anywhere in iLO2's client-visible pages before you're logged
in) releases it early. `WebServer._connect_worker` calls `logout()` before
every `login()` (initial connect, manual reconnect, and each KVM-arming
retry) and `webserver.main()` calls it once more on shutdown, so a running
ILO2Remastered process only ever holds one slot at a time. Scripting
`IloSession` directly (e.g. from a REPL) doesn't get this for free --
**call `logout()` yourself when you're done, or repeated testing without
it still leaks sessions and eventually exhausts the pool**. Symptom: `GET /`
starts returning `sessionkey="NONEAVAILABLE"` and `sessionindex="ffffffff"`
instead of real values, and `IloSession.login()` raises `LoginError`.

Fix: `IloSession.reset_management_processor()` (RIBCL `RESET_RIB`) reboots
*just the iLO2 controller* (not the host server), clearing all its
sessions in ~30-60s. **RIBCL keeps working during this exhaustion** (it's
a separate, per-request auth path, not tied to the web session pool), so
this recovery is always available even when the web UI itself is locked
out. Poll `nc -z host 443` in a loop until it comes back up.

### Console connect gets `ConnectionRefusedError` on port 23

`fetch_console_params()` only arms the port-23 listener for a short window
(single-digit seconds, empirically). If a connect attempt lands outside
that window, it's refused. This isn't a real error -- just redo
`login()` → `fetch_console_params()` → `IloConsole.connect()` fresh; don't
reuse old `params`.

### `rcseize_rcinuse=1` / `INFOA`/`B`/`C`/`D` come back empty

The Remote Console only allows one active KVM session at a time. If
`fetch_console_params()`'s HTML shows `rcseize_rcinuse=1`, someone (or a
leftover connection of your own from earlier testing that never called
`disconnect()`) already has it, and you won't get real encryption keys.
`reset_management_processor()` also clears this, same as above.

### `IloConsole.on_status_text("text", "No Video")`

This means iLO2's video-capture hardware genuinely isn't detecting a
signal right now -- it's the correct, honest report, not a decode failure
(the decoder still runs cleanly through the whole state machine; it just
resolves `screen_x`/`screen_y` to 0). Real-world causes we hit while
building this:

- The host's *primary display* is set to a discrete PCIe GPU in BIOS
  RBSU (`Advanced Options → Advanced Video Options → Video Boot
  Priority`/`Embedded Video`) instead of the onboard chip iLO2 actually
  taps. Symptom persists across OS reboots since it's set at POST, before
  any OS loads. Fix is physical: `Embedded Video = Primary`, `Optional
  Video = Secondary` in RBSU (press F9 at the HP splash screen).
- Separately, an NVIDIA card running its proprietary driver **explicitly
  disables legacy VGA decode on itself** once `nvidia-drm`/`nvidia-modeset`
  load (`dmesg` shows `vgaarb: VGA decodes changed: ... decodes=none`).
  If that GPU is also the BIOS-primary display, you'll see BIOS/GRUB/early
  kernel messages over iLO (before the driver loads) and then it'll go
  dark again once the OS finishes booting -- expected, not a regression.
  There's no supported way to keep the proprietary driver loaded *and*
  keep that card's legacy VGA output alive; if you need persistent OS
  console video over iLO with a discrete GPU installed, the onboard chip
  needs to be the BIOS-primary display, not the discrete card.

### iLO2 holds connections open for ~9s after finishing a response

Confirmed by testing: even though the HTTP response body is fully sent,
iLO2 doesn't close (or send more data on) the TCP connection for several
seconds afterward. A naive "read until the socket times out or closes"
client (the first version of `legacy_tls.raw_request`) pays that full
delay on *every single request*, which added up to real, user-visible
slowness (a full login + console-params fetch took ~30s). Fixed by
detecting response completion from the HTTP framing itself
(`Content-Length` or the chunked terminator) and returning immediately --
see `_response_complete`/`_chunked_body_complete` in `legacy_tls.py`. Not
using a substring search for the chunked terminator (`b"0\r\n\r\n"`)
matters: that exact byte sequence can legitimately appear inside chunk
*data*, which caused real, silent response truncation before it was fixed
to actually walk the chunk framing.

### The web server sometimes fully hangs (ports 80/443 stop responding, ICMP still fine)

Seen once after a long day of repeated automated testing: `ping` kept
working but `nc -z host 443` (and 80) just refused/timed out, RIBCL
included, while the box's own SSH stayed reachable. This looks like the
embedded HTTP server thread pool wedging under sustained load rather than
anything specific to this client. No clean recovery found other than
waiting it out or a full power cycle of the *host* (which also resets
iLO2 since it's on the same board, though iLO2 itself is normally on
standby power and shouldn't need the host to be power-cycled for a
`RESET_RIB`-style recovery -- this was a harder hang than the
session-exhaustion case above, where RIBCL still worked). If you're
scripting bulk testing against real hardware, add real backoff/pacing;
this box does not appreciate being hammered.

## Codebase-side gotchas

### Why there's no native (Tkinter) client

An early version of this project had a Tkinter desktop client alongside
the web one, sharing the same `ilo2/` backend. It's gone now (the web
client covers the same ground and is what's actually used), but the
reasons it was dropped are worth knowing if a native client ever comes
back into scope: two separate macOS/Aqua-Tk-specific bugs cost real
debugging time before switching primary effort to the browser client --
(1) `root.after(0, fn)` scheduled from a background thread (login, the
console's receiver thread, and RIBCL calls all ran off the main thread)
silently no-opped instead of raising or running, so UI updates pushed from
those threads just never appeared, with no exception to point at the
problem; (2) separately, a `Canvas` inside stretching/`sticky="nsew"`
parent frames did not reliably display `create_image`/`itemconfig`
updates on that machine, even though the underlying `PIL.Image` was
independently verified pixel-correct (dumped to PNG and inspected). Root
cause of the second one was never fully pinned down. The web client (a
plain `<canvas>` + JPEG blobs pushed over a WebSocket) doesn't have either
problem.

### Keyboard layout (web client)

This protocol sends interpreted bytes/characters over the wire, not
physical scancodes (unlike a real hardware KVM) -- so the correct thing to
send is whatever character the user's own keyboard layout actually
produces, i.e. `KeyboardEvent.key`, not the physical key position.

The first version of this got that backwards: it read
`KeyboardEvent.code` (the physical key, layout-independent) through a
hardcoded `US_LAYOUT` table, on the theory that BIOS/console input
"expects US-QWERTY" the way a real scancode-based KVM would. That's true
for letters (QWERTY layouts agree on those) but wrong for punctuation --
`.code` is purely positional, so on a non-US layout it does not report
what the key actually produces. On an Italian keyboard this sent
confidently *wrong* symbols, occasionally swapping two outright (the key
an Italian user reaches for "?" sits at the same physical position as US
`Minus`, whose table entry is `_`, so "?" arrived as "_" and vice versa).

Current behavior in `web/index.html::keyToBytes()`: try `e.key` first
(already resolved against the user's real active layout); fall back to
the `US_LAYOUT`/`e.code` table only when `e.key` isn't a usable single
character (some browsers/keys). `Ctrl`+letter is special-cased to send the
corresponding ASCII control code (`0x01`-`0x1A`) instead of either path.
Named/navigation keys (arrows, F-keys, Enter, etc.) come from `e.key` too,
which is fine for those -- they're not affected by layout. The mobile
on-screen-keyboard path is separate again (see ARCHITECTURE.md) since
touch IMEs often don't fire useful `KeyboardEvent.code`/`.key` at all.

### Concurrent console-connect races

`ilo2/webserver.py::WebServer` auto-starts the console on boot; a user
clicking "Avvia Console" in the browser at the same moment used to fire a
*second*, concurrent `_connect_worker()`. Since `sessionkey` is single-use,
whichever request fetched its key second would invalidate the first's,
and one of the two logins would fail with a confusing
`LoginError('login rejected...')` that had nothing to do with credentials.
Fixed with `WebServer._connecting`, a non-blocking `threading.Lock` used
purely as a guard (`acquire(blocking=False)` / `release()` in `finally`)
-- a second concurrent start request just logs and no-ops instead of
racing.

## What's not implemented

- **Absolute/high-performance mouse mode.** Only the simple relative-move
  protocol (`mouse_protocol == 0`) is implemented. The original applet's
  `MouseSync` class does server-side cursor position reconciliation for a
  second, higher-fidelity mode that wasn't ported.
- **Full special-key table.** Shift/Ctrl/Alt-modified variants of
  Home/End/PgUp/PgDn/Insert/F-keys beyond plain presses aren't mapped (see
  `cim.translate_special_key()` in the decompiled applet source if you
  need a specific one -- PROTOCOL.md §7 explains how to get back to that
  source).
- **Locale-aware keyboard translation matching the original
  `LocaleTranslator`.** The "treat every physical key as US-QWERTY"
  approach (PROTOCOL.md/this file, keyboard layout section) is a simpler,
  different fix for the same underlying problem and works fine for
  BIOS/console use, but isn't a port of the original class.
- **Real video encoding (H.264/HLS).** The web client gets dirty-rect JPEG
  tiles over the WebSocket (see ARCHITECTURE.md's `FrameBuffer` section),
  not a proper video stream. Good enough for a live console view; if you
  need this behind a real media pipeline, `ilo2/framebuffer.py::FrameBuffer`
  is already the right seam to hang an encoder off (it's just a thread-safe
  `PIL.Image` plus dirty-rect tracking -- swap or wrap
  `take_update()`/`full_snapshot()`, or consume the `paste_block()` calls
  directly).
