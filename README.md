# ILO2Remastered

A from-scratch client for HP Integrated Lights-Out 2 (iLO2), built because
modern browsers can no longer talk to iLO2's embedded web server at all
(`SSL_ERROR_UNSUPPORTED_VERSION`) and its Remote Console is a Java applet
that no browser has been able to run since NPAPI died around 2015.

Instead of patching around the browser, this reimplements the pieces of
iLO2's client-side stack that are actually needed, in Python, from the
protocol up:

- talks iLO2's TLS 1.0-only HTTPS (with a cipher workaround for a DHE
  handshake bug — see [docs/PROTOCOL.md](docs/PROTOCOL.md))
- logs into the web UI and drives the classic RIBCL XML API (power on/off/
  reset/status)
- reverse-engineered and reimplemented the Remote Console applet's KVM
  protocol: RC4/MD5 session crypto and a proprietary bit-oriented video
  codec ("DVC"), from the decompiled `rc175p11.jar`
- serves the result as **video + keyboard + mouse over a plain WebSocket**,
  with a small browser frontend, so it works from any modern browser and
  isn't tied to any particular UI toolkit

Full protocol writeup: [docs/PROTOCOL.md](docs/PROTOCOL.md).
Code tour: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Setup / dev notes / known quirks: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Quickstart

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env   # fill in ILO_HOST / ILO_USER / ILO_PASSWORD
python3 webmain.py
```

Then open **http://localhost:8080/** in any browser. Video, keyboard,
mouse, and power controls (on/off/reset) all work from that one page.

There's also a native Tkinter client (`python3 main.py`) with the same
feature set; see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for why the web
client is the recommended path.

## Status

- Power control (on/off/reset/status via RIBCL): solid, exercised a lot.
- Web login + Remote Console session setup: working, with some documented
  iLO2-side quirks (see docs/DEVELOPMENT.md) around session limits and
  timing.
- Video/keyboard/mouse over the DVC protocol: working end-to-end, verified
  against real hardware.

## Legal / scope note

This talks to hardware you own or administer, using credentials you
already have, over protocols the vendor's own client used — it's a
client-side reimplementation for interoperability with your own device,
not an attack against anything. Don't point it at hardware you don't have
authorization to manage.
