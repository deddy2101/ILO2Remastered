# iLO2 protocol notes

Everything here was reverse-engineered against a real iLO2 (firmware 2.22,
web UI copyright strings 2001-2005/2018) by inspecting live traffic and
decompiling the Remote Console applet (`rc175p11.jar`, class
`com.hp.ilo2.remcons.*`, banner string `Version 20050808154652` --
this codec dates to ~2005 RILOE-era firmware, carried forward unchanged).
None of this is documented anywhere official; treat firmware-version
differences as a real possibility if this stops matching what you see.

## 1. Why browsers can't connect at all

Two separate, stacking problems:

1. **iLO2's HTTPS only speaks TLS 1.0** (confirmed via `openssl s_client`).
   Every current browser refuses TLS below 1.2. This is the
   `SSL_ERROR_UNSUPPORTED_VERSION` Firefox shows.
2. Even forcing TLS 1.0, **the DHE key exchange iLO2 offers by default
   hangs modern TLS stacks outright** -- the client sends its Finished
   message and iLO2 never replies (confirmed with both LibreSSL and
   OpenSSL 3.x clients; verified at the record layer with `-msg -state`).
   Forcing a plain RSA-key-exchange cipher (`AES128-SHA`, no forward
   secrecy, no DHE) makes the handshake complete normally. This is the one
   setting that actually matters in `ilo2/legacy_tls.py::_connect()`:
   `ssl.TLSVersion.TLSv1` min/max plus `set_ciphers("AES128-SHA")`.

Separately, the Remote Console's "video" tab is a **Java NPAPI applet**.
No browser has supported NPAPI since ~2015-2017, so even a hypothetical
TLS-1.0-tolerant browser couldn't run it. This is *why* this project exists
as a reimplementation instead of "just fix the TLS".

There's also an "Integrated Remote Console" option in the UI: that's
ActiveX, IE-only, and was never in scope here.

## 2. Web UI login

`GET /` returns a login page containing two JS globals:

```js
var sessionkey="<40-char one-time nonce>";
var sessionindex="<8-hex-digit counter>";
```

The login button's JS builds a cookie value (function `MakeCookie` /
`createToken` in the page's inline `<script>`):

```
token = sessionindex + ":" + b64(username) + ":" + b64(password) + ":" + sessionkey
```

(standard base64, no LDAP `dn` component for local accounts) and sets it
as `hp-iLO-Login`, then navigates to `/index.htm`. The server validates
that cookie and responds with `Set-Cookie: hp-iLO-Session=<idx>:::<key>`
(note: **three colons**, format observed as
`sessionindex:::sessionkey`). That `hp-iLO-Session` cookie is what every
subsequent authenticated page load needs.

`sessionkey` appears to be single-use / short-lived: the fetch-key and
build-and-send-login steps need to happen back-to-back (seconds apart),
not with a long pause in between, or you'll get rejected. See
DEVELOPMENT.md for the "`sessionkey="NONEAVAILABLE"`" failure mode this is
related to.

Implementation: `ilo2/session.py::IloSession.login()`.

## 3. RIBCL (power control, and everything else the old iLO XML API does)

iLO2 predates the HTTP-wrapped RIBCL that iLO3+ use. On iLO2, you connect
to port 443 with TLS, and instead of an HTTP request you send **raw XML
directly on the socket**:

```
<?xml version="1.0"?>\r\n
<RIBCL VERSION="2.0">
<LOGIN USER_LOGIN="..." PASSWORD="...">
  <SERVER_INFO MODE="write"><SET_HOST_POWER HOST_POWER="Yes"/></SERVER_INFO>
</LOGIN>
</RIBCL>
```

Two details that matter and cost real debugging time:

- **The XML header and the RIBCL body must land in two separate TCP
  writes** (two `sendall()` calls), not one combined buffer. Python's
  `socket.sendall()` on a single concatenated buffer usually goes out as
  one TCP segment/TLS record and the firmware never replies to that. This
  is why `python-hpilo` (a prior Python client for HP iLO, which was
  useful for cross-checking this behavior) calls it "RAW" mode.
- Auth is per-request (`USER_LOGIN`/`PASSWORD` inside the XML), so this is
  entirely independent of the web UI's session-cookie login -- RIBCL calls
  keep working even when the web UI is refusing new logins.

Response framing: the read loop just reads until the server closes/times
out; several `<RESPONSE STATUS="0x0000".../>` blocks typically come back
even for a single command (informational chatter), with the real result
(if any) inside the last one.

Useful commands beyond power control:
`<RIB_INFO MODE="write"><RESET_RIB/></RIB_INFO>` reboots *just the iLO2
management processor* (not the host), which is the fix for the web-UI
session exhaustion issue -- see DEVELOPMENT.md.

Implementation: `ilo2/legacy_tls.py::ribcl_raw()`,
`ilo2/session.py`'s RIBCL methods.

## 4. Arming the Remote Console (port 23)

Loading `GET /drc2fram.htm?restart=1` (authenticated) does two things:

1. Returns the HTML that would embed the Java applet, with its
   `<PARAM>` values inlined as JS variables (see below).
2. **Tells the firmware to open the KVM TCP listener** (port given by
   `INFO6`, always `23` in what we've seen) for a short window. Before
   this request, port 23 refuses connections; a few seconds after, it
   accepts one connection and then appears to close the window again. If
   you see `ConnectionRefusedError` on port 23, this is almost always
   "fetch_console_params() wasn't recent enough" rather than a real
   problem -- just retry the whole login → fetch_params → connect
   sequence.

Also on this page: `rcseize_rcinuse=1` means another client already has
the console session and you won't get real encryption keys (`INFOA`/`B`/
`C`/`D` come back empty/`None`) until it's released -- see
DEVELOPMENT.md.

Relevant `PARAM`s (all scraped by
`IloSession.fetch_console_params()`):

| Param    | Meaning                                                        |
|----------|------------------------------------------------------------------|
| `INFO0`  | Base64 login ticket, decodes to `0x<sessionindex-hex>\r<32-hex-char token>` (see §5) |
| `INFO1`  | Present/absent flag only -- its *value* is unused, just whether the key exists (see §5) |
| `INFO6`  | KVM TCP port (observed: always `23`)                            |
| `INFOA`  | `"1"` if the DVC stream is RC4-encrypted, absent/`"0"` otherwise |
| `INFOB`  | 32 hex chars = 16-byte RC4 seed for the **decrypt** direction (server→client) |
| `INFOC`  | 32 hex chars = 16-byte RC4 seed for the **encrypt** direction (client→server) |
| `INFOD`  | Decimal key index, sent in the encrypted-login header            |

The applet's own base64 decode for `INFO0` uses the standard alphabet but
post-processes decoded byte `0x3A` (`:`) back into `0x0D` (CR) -- the
server substitutes CR with `:` before base64-encoding, presumably because
raw CR inside an HTML attribute is awkward. `ilo2/console.py::_decode_login_ticket()`
reverses this.

## 5. KVM socket handshake (port 23)

1. TCP connect to `host:INFO6`.
2. Build the login string (`ilo2/console.py::build_login_string()`):
   ```
   login = decode_login_ticket(INFO0)      # "0x<idx>\r<32-hex-token>\r"
   if INFO1 is present: login = "\x1b[4" + login
   login = "\x1b[7\x1b[9" + login
   ```
3. If `INFOA == "1"` (encryption on):
   ```
   key_index = int(INFOD)
   header = bytes([0xFF, 0xC0]) + key_index.to_bytes(4, "big")
   send(header + RC4(seed=INFOC).xor(login))
   ```
   The `0xFF 0xC0` + 4-byte key index go out **unencrypted**; the RC4
   keystream starts exactly at the login string's first byte. This is one
   continuous keystream for the rest of the connection -- every later
   keyboard/mouse/control message is XORed with the *next* bytes of the
   same stream, there's no per-message IV or re-sync.
   If encryption is off, the login string is sent as plain bytes with no
   header at all.
4. Read the socket, scanning byte-by-byte (not decrypting yet) for the
   3-byte sequence `ESC [ R` (switch to DVC mode, RC4-encrypted) or
   `ESC [ r` (DVC mode, plaintext). Before this trigger you'll typically
   see plain Telnet option negotiation (`IAC WILL/DO ...`, i.e.
   `0xFF 0xFB/0xFD ...`) -- this class inherits from
   `com.hp.ilo2.remcons.telnet`, hence the leftover Telnet framing.
5. From the byte *after* the trigger, every byte belongs to the DVC video
   stream (§6). If the trigger was `R`, XOR each byte with the next
   keystream byte from a **separate** `RC4(seed=INFOB)` instance first --
   this is the decrypt direction, independent from the encrypt one.

### RC4 key schedule (`ilo2/crypto.py`)

Not textbook RC4-with-seed-as-key. The applet's `RC4` class:

```
key = MD5(seed || key)     # key starts as 16 zero bytes
KSA using this 16-byte MD5 digest, cycling key[i % 16]
standard RC4 PRGA from there
```

`RC4.update_key()` re-derives `key = MD5(seed || current_key)` and re-runs
the KSA -- this is what a "change key" firmware command (§6, `CORP`
command 9) triggers on **both** directions' RC4 objects simultaneously
(`IloConsole._on_change_key()`).

### Outgoing control message formats

All sent via `IloConsole.transmit()` (i.e. RC4-XORed with the ongoing
encrypt keystream, same as the login):

| Action              | Bytes                                    |
|----------------------|-------------------------------------------|
| Refresh screen        | `ESC [ ~`                                 |
| Keepalive              | `ESC [ (`                                 |
| Auto-alive              | `ESC [ &`                                 |
| Ctrl+Alt+Del            | `ESC [ 2 ESC [ 0x7F`                      |
| Mouse relative move    | `0xFF 0xD0 <dx:i8> <dy:i8>`               |
| Mouse button press     | `0xFF 0xD1 <button>`                      |
| Mouse button release   | `0xFF 0xD2 <button>`                      |
| Mouse click            | `0xFF 0xD3 <button> <count>`              |

Mouse button codes: left=4, center=2, right=1 (yes, that ordering).
This project only implements the *relative* mouse protocol
(`mouse_protocol == 0` in the original applet); the applet also supports
an absolute/high-performance mode with server-side cursor sync
(`MouseSync.class`) that wasn't reimplemented.

Keyboard bytes for plain printable characters are just their US-ASCII
value. Special keys use `ESC [ <letter>` sequences (see the `SPECIAL`
table in `web/index.html` for the ones implemented: arrows,
Home/End/PgUp/PgDn/Insert, F1-F12,
Tab/Enter/Backspace/Delete/Escape). The original applet has a much larger
table covering Shift/Ctrl/Alt-modified variants of every special key
(`cim.translate_special_key()` in the decompiled source) that wasn't fully
ported -- see DEVELOPMENT.md's keyboard-layout note for why this mostly
doesn't matter for BIOS/OS console use, and grep the decompiled sources
(§7) if you need a specific missing combo.

## 6. The DVC video codec

"DVC" (the name comes from internal debug strings; likely "Digital Video
Compression" or similar) is a proprietary differential/run-length pixel
codec, implemented as a **48-state bit-oriented state machine**. Ported
almost line-by-line into `ilo2/dvc.py::DvcDecoder` from the decompiled
`cim.process_bits()` -- deliberately *not* cleaned up into something more
"idiomatic", because the state graph has zero documentation anywhere and
preserving the original shape is what makes it possible to diff against
the decompiled Java when something doesn't decode right.

### Bit-level framing

Bits are consumed **LSB-first** out of an accumulator fed one byte at a
time (`add_bits`), but multi-bit fields are **bit-reversed** on the way
out (`get_bits`, via a precomputed 256-entry reversal table) -- a classic
trick for making an LSB-first shift register emit fields as if it were
MSB-first. `add_bits` also tracks a running count of leading/trailing zero
bits per byte (via `dvc_left`/`dvc_right` lookup tables) to detect a long
run of zero bits, which is used as an explicit **resync/reset marker**
(`HUNT` state) -- every DVC stream we've seen starts with one, and it
recurs periodically (looked like roughly once per full frame in testing).
**Seeing "reset sequence detected" / "unexpected hit" messages in the log
during this resync is normal, not a decode error** -- it happens before
the state machine has re-synced, by design.

### State graph shape

Each of the 48 states has a fixed number of bits it consumes
(`BITS_TO_READ[state]`); the value of those bits picks the next state via
`NEXT_0[state]`/`NEXT_1[state]` (a literal 1-bit-of-context Huffman-ish
dispatch, not a real Huffman table). Broad groups of states:

- **Pixel color, LRU-cached** (`PIXLRU0/1`, `PIXCODE1-4`, `PIXGREY`,
  `PIXRGBR/G/B`): a 17-entry LRU cache of recently-used colors
  (`dvc_cc_*` arrays) lets a repeated color cost ~log2(17) bits instead of
  a full RGB444 (12-bit) literal. `PIXRGBR`→`PIXRGBG`→`PIXGREY`/`PIXRGBB`
  is the path for an actual new 12-bit color literal, expanded to 8-bit
  per channel via nibble replication (`n * 17`) in `color_remap_table`.
- **Run-length / repeat** (`PIXRPT`, `PIXRPTSTD1/2`, `PIXRPTNSTD`,
  `PIXDUP`): repeat the last-emitted color N times within the current
  16x16 block.
- **Block-level** (`BLKRPT`, `BLKRPTSTD`, `BLKRPTNSTD`, `BLKDUP`):
  repeat/duplicate a whole completed 16x16 block N times, advancing
  `dvc_lastx`/`dvc_lasty` -- this is how large solid-color regions get
  compressed cheaply.
- **Positioning** (`MOVEXY0/1`, `MOVESHORTX`, `MOVELONGX`): set the
  current block cursor (in 16px units).
- **Frame setup** (`MODE0/1/2`): `MODE1` sets `dvc_size_x`/`dvc_size_y`
  (in 16px block units), `MODE2` finalizes actual pixel dimensions
  (`screen_x = size_x*16`, `screen_y = size_y*16 + clip`) and is where
  `video_detected` gets computed -- **zero width/height here means the
  iLO2 hardware genuinely isn't capturing a video signal right now**, not
  a decode bug (see DEVELOPMENT.md, "No Video").
- **Firmware/control channel** (`FIRMWARE`, `CORP`): a side-channel
  command protocol. `CORP` dispatches on an accumulated command byte
  (`cmd_last`): `1`=graceful DVC exit, `2`=start of a print/status string
  (`PRINT0`/`PRINT1`, terminated by a `0` byte, routed to
  `on_status(field, text)` -- this is where things like refresh
  rate/resolution status text comes from), `3`=set framerate, `6`="video
  suspended", `7`/`8`=start/stop an RDP terminal-services launch (not
  applicable here, ignored), `9`=rotate both RC4 keys
  (`on_change_key`), `10`=**seize**: another client has taken over the
  console and this one should disconnect (`on_seize`).
- **Error/resync** (`LATCHED`, `HUNT`, `TIMEOUT`, `EXIT`): `LATCHED` is
  the generic "something didn't decode as expected" state; it has a
  self-healing counter that requests a fresh full-frame refresh
  (`refresh_screen()`) after enough bytes pass through it without
  recovering on its own.

### Output

`DvcDecoder.on_block(x, y, pixels)` fires once a 16x16 block is complete,
with `pixels` a flat list of up to 256 packed `0xRRGGBB` ints (row-major).
`on_resize(w, h)` fires when `MODE2` establishes real dimensions.
The consumer (`FrameBuffer`) just pastes each block into an image at
`(x, y)`.

## 7. Re-deriving any of this yourself

If a firmware update changes something and this stops matching:

1. Log into the iLO2 web UI, grab `/drc2fram.htm?restart=1` and
   `/<applet-archive>.jar` (the archive filename is in the `<APPLET
   ARCHIVE=...>` tag on that page -- was `rc175p11.jar` here) the same way
   `ilo2/session.py` does.
2. Decompile with [CFR](https://github.com/leibnitz27/cfr) (a single jar,
   `java -jar cfr.jar the.jar --outputdir out/`) -- worked cleanly on this
   firmware's class files with no manual fixups needed.
3. `com.hp.ilo2.remcons.remcons` is the applet entry point (param
   parsing, UI); `cim` is the protocol/codec (extends `telnet`, which owns
   the raw socket and the plaintext-preamble scanning); `RC4`/`VMD5` are
   the crypto (`VMD5` is plain unmodified MD5 -- verified against the
   RFC 1321 constants -- so `hashlib.md5` is a safe substitute).
