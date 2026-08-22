# 🖥️ ILO2Remastered

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

There's also a native Tkinter client (`python3 main.py`) with the core
console feature set; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for why
the web client is the recommended path.

### 🐳 Or with Docker

```bash
cp .env.example .env   # fill in ILO_HOST / ILO_USER / ILO_PASSWORD
docker compose up -d
```

Same page, same port, nothing installed on the host beyond Docker itself.

## 🌍 Exposing this beyond your LAN

This app can view the server's console, send it keyboard/mouse input, and
power-cycle it — treat it like physical access to the machine. If it's
reachable from anywhere you don't fully trust:

1. **Put a TLS-terminating reverse proxy in front of it** (nginx, Caddy,
   Traefik, ...) forwarding to `http_port` (8080) and `ws_port` (8765).
   Everything here is plain `http://`/`ws://` — with a login, that means
   sending the password (and every key pressed in the remote console,
   including whatever you type into the server's own OS login) in the
   clear otherwise.
2. **Set `WEBAPP_USER`/`WEBAPP_PASSWORD` in `.env`** (see
   `.env.example`) to require a login before the page or the WebSocket
   will do anything. Both are unset by default, so it's open to anyone who
   can reach the port — fine on a trusted LAN, not fine otherwise.
3. Make sure your proxy passes through `X-Forwarded-Proto: https` (most do
   by default) — the session cookie only gets marked `Secure` when it sees
   that header, so it knows it's actually safe to send over HTTPS instead
   of assuming and breaking plain-`http://` local testing.

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
