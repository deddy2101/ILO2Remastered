<img src="assets/wordmark.png" alt="ILO2Remastered" width="560">

A from-scratch client for HP Integrated Lights-Out 2 (iLO2), built because
modern browsers can no longer talk to iLO2's embedded web server at all
(`SSL_ERROR_UNSUPPORTED_VERSION`) and its Remote Console is a Java applet
that no browser has been able to run since NPAPI died around 2015. If you've
got an old ProLiant sitting in a closet and a browser from this decade, iLO2
just... stops working. This fixes that.

Instead of patching around the browser, this reimplements the pieces of
iLO2's client-side stack that are actually needed, in Python, from the
protocol up — and wraps the result in a proper web dashboard, not just a
bare video feed.

## ✨ What you actually get

- 🖱️ **Full remote console** — video, keyboard, and mouse, streamed over a
  plain WebSocket to any modern browser. No Java, no plugins, no dead
  browser APIs.
- 🔌 **Power control** — on / off (graceful) / off (forced) / reset, straight
  from the dashboard.
- 🌡️ **Live health sensors** — CPU/ambient/memory temperatures, fan speeds,
  power supply status, all pulled from the same RIBCL API iLO2's own web UI
  uses, refreshed every few seconds.
- 💡 **UID (blue "locate") LED** — toggle it on/off to find the box in a
  rack; the dashboard also explains when iLO2 is flashing it itself (it does
  that automatically while a console session is open).
- 📱 **Actually usable on a phone** — installable as a PWA, pinch-to-zoom on
  the console without hijacking the remote mouse, an on-screen keyboard
  fallback, touch-sized buttons. This was designed mobile-first, not
  ported after the fact.
- 🔁 **Shared, multi-client** — the backend holds one session to the iLO2 and
  broadcasts video/sensors/log to every connected browser tab, so several
  people (or several devices) can watch the same console at once.
- 🩹 **Self-healing where it's safe to be** — auto-retries a console connect
  that missed iLO2's narrow "arm the KVM port" window; for anything more
  disruptive (resetting the iLO2 management processor because its tiny
  session pool filled up), it asks you first instead of just doing it.
- 🔐 **Optional login** — a username/password gate with signed session
  cookies, rate-limited login attempts, and WebSocket-level enforcement (not
  just the page), for when this needs to leave your LAN.
- 🐳 **Docker-ready** — a `Dockerfile` and `docker-compose.yml` are included;
  config comes in entirely through environment variables, nothing sensitive
  ever gets baked into the image.

Under the hood, it:

- talks iLO2's TLS 1.0-only HTTPS (with workarounds for a DHE handshake bug
  *and* for OpenSSL 3.x refusing TLS 1.0 and legacy renegotiation outright —
  see [docs/PROTOCOL.md](docs/PROTOCOL.md))
- logs into the web UI and drives the classic RIBCL XML API (power, health,
  UID, firmware/network info)
- reverse-engineered and reimplemented the Remote Console applet's KVM
  protocol: RC4/MD5 session crypto and a proprietary bit-oriented video
  codec ("DVC"), from the decompiled `rc175p11.jar`

Full protocol writeup: [docs/PROTOCOL.md](docs/PROTOCOL.md).
Code tour: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Setup / dev notes / known quirks: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## 🚀 Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ILO_HOST / ILO_USER / ILO_PASSWORD
python3 webmain.py
```

Then open **http://localhost:8080/** in any browser (or "Add to Home
Screen" on a phone — it's installable). Video, keyboard, mouse, power
controls, and live sensors all work from that one page.

### 🐳 Or with Docker

```bash
cp .env.example .env   # fill in ILO_HOST / ILO_USER / ILO_PASSWORD
docker compose up -d
```

Same page, same port, nothing installed on the host beyond Docker itself.
The bundled `nginx.conf` also does the "just one exposed port" part below
for you: `docker-compose.yml`'s `ilo2remastered` service isn't published
to the host at all (`expose`, not `ports`) — only the `nginx` service is,
proxying both the page and the WebSocket (path `/ws`) through the single
port it publishes (`8180` by default).

Running without Docker (`python3 webmain.py` directly)? The page and the
WebSocket are two separate ports there (`--http-port`/`--ws-port`, 8080/
8765 by default) unless you put your own reverse proxy in front the same
way `nginx.conf` does.

## 🌍 Exposing this beyond your LAN

This app can view the server's console, send it keyboard/mouse input, and
power-cycle it — treat it like physical access to the machine. If it's
reachable from anywhere you don't fully trust:

1. **Terminate TLS somewhere in front of it.** `nginx.conf` unifies the
   two ports but still speaks plain `http://`/`ws://` on its own listening
   side — add a cert there (or put another TLS-terminating proxy, or your
   router/firewall's own reverse proxy, in front of *that*). Without it,
   the login password and every key pressed in the remote console
   (including whatever gets typed into the server's own OS login) cross
   the network in the clear.
2. **Set `WEBAPP_USER`/`WEBAPP_PASSWORD` in `.env`** (see
   `.env.example`) to require a login before the page or the WebSocket
   will do anything. Both are unset by default, so it's open to anyone who
   can reach the port — fine on a trusted LAN, not fine otherwise.
3. Make sure whatever terminates TLS passes through `X-Forwarded-Proto:
   https` (`nginx.conf` already does) — the session cookie only gets
   marked `Secure` when it sees that header, so it knows it's actually
   safe to send over HTTPS instead of assuming and breaking plain-`http://`
   local testing.
4. If you're reaching iLO2 itself through a NAT/port-forward that maps a
   different external port to its real 443 (say, as a recovery path that
   deliberately doesn't depend on the same VPN/router this app usually
   goes through), set `ILO_PORT` in `.env` to that mapped port.

## ✅ Status

- Power control (on/off/reset/status via RIBCL): solid, exercised a lot.
- Health sensors (temperatures/fans/PSUs), UID LED: working, verified
  against real hardware.
- Web login + Remote Console session setup: working, with some documented
  iLO2-side quirks (see docs/DEVELOPMENT.md) around session limits and
  timing — the web client now retries through the common ones on its own.
- Video/keyboard/mouse over the DVC protocol: working end-to-end, verified
  against real hardware, including from mobile.

## ⚖️ Legal / scope note

This talks to hardware you own or administer, using credentials you
already have, over protocols the vendor's own client used — it's a
client-side reimplementation for interoperability with your own device,
not an attack against anything. Don't point it at hardware you don't have
authorization to manage.

## 📄 License

[MIT](LICENSE).
