"""Minimal HTTP/1.1 client for talking to old HP iLO2 firmware.

iLO2's embedded web server only speaks TLS 1.0, and its DHE key exchange
hangs modern OpenSSL/LibreSSL clients outright (confirmed against a live
iLO2: the client Finished message is sent but the server never replies).
Forcing a plain RSA cipher (no DHE) avoids that; this is the one setting
that actually matters here, everything else about the TLS handshake is
otherwise unremarkable.
"""
import socket
import ssl
import time

_CIPHER = "AES128-SHA"  # RSA key exchange only -- avoids the DHE hang


def _connect(host, port, timeout):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1
    # OpenSSL 3.x's default @SECLEVEL (>=1) refuses TLS1.0 entirely
    # ("no protocols available"), and separately refuses the handshake
    # even at SECLEVEL=0 because iLO2 doesn't support RFC5746 secure
    # renegotiation ("unsafe legacy renegotiation disabled"). Both have to
    # be relaxed explicitly on OpenSSL 3.x clients; older OpenSSL 1.1
    # builds allowed this by default. The "@SECLEVEL=0" cipher-string
    # suffix is an OpenSSL-ism, though: LibreSSL's cipher-string parser
    # (macOS's system Python, at least through 3.9) doesn't understand it
    # and rejects the whole string ("No cipher can be selected"), so fall
    # back to the plain cipher name there -- LibreSSL never needed the
    # security-level relaxation in the first place.
    try:
        ctx.set_ciphers(f"{_CIPHER}@SECLEVEL=0")
    except ssl.SSLError:
        ctx.set_ciphers(_CIPHER)
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    raw = socket.create_connection((host, port), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname=host)


def _chunked_body_complete(body: bytes) -> bool:
    """Walks a (possibly still-arriving) chunked body chunk by chunk. Only
    returns True once an actual zero-length terminator chunk has been fully
    parsed -- checking for the b"0\\r\\n\\r\\n" byte pattern anywhere in the
    buffer is not safe, since normal chunk *data* can legitimately contain
    that exact byte sequence and would trigger a false positive, truncating
    the response before the real end."""
    buf = body
    while True:
        nl = buf.find(b"\r\n")
        if nl == -1:
            return False  # size line not fully arrived yet
        size_line = buf[:nl].split(b";")[0]
        try:
            size = int(size_line, 16)
        except ValueError:
            return False
        chunk_end = nl + 2 + size
        if len(buf) < chunk_end + 2:
            return False  # chunk data/trailing CRLF not fully arrived yet
        if size == 0:
            return True
        buf = buf[chunk_end + 2:]


def _response_complete(data: bytes):
    """Returns True once `data` holds a full HTTP response, so raw_request
    can stop reading instead of idling out the full socket timeout waiting
    for iLO2 to get around to closing the connection (it takes its time --
    up to ~9s per request otherwise, even though the body finished arriving
    almost immediately)."""
    head_end = data.find(b"\r\n\r\n")
    if head_end == -1:
        return False
    header_block = data[:head_end].lower()
    body = data[head_end + 4:]
    if b"transfer-encoding: chunked" in header_block:
        return _chunked_body_complete(body)
    cl_pos = header_block.find(b"content-length:")
    if cl_pos != -1:
        line_end = header_block.find(b"\r\n", cl_pos)
        try:
            length = int(header_block[cl_pos + len(b"content-length:"):line_end].strip())
        except ValueError:
            return False
        return len(body) >= length
    return False  # no framing info -- have to wait for EOF/timeout


def raw_request(host, method, path, headers=None, body=None, port=443, timeout=10):
    s = _connect(host, port, timeout)
    hdrs = {"Host": host, "Connection": "close", "User-Agent": "Mozilla/5.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        hdrs["Content-Length"] = str(len(body))
    lines = [f"{method} {path} HTTP/1.1"]
    for k, v in hdrs.items():
        lines.append(f"{k}: {v}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode()
    if body:
        req += body if isinstance(body, bytes) else body.encode()
    s.sendall(req)
    s.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if _response_complete(data):
                break
    except socket.timeout:
        pass
    s.close()
    return data


def ribcl_raw(host, xml_body: bytes, port=443, timeout=15, quiet_period=0.75):
    """Send a RIBCL XML document the old way: raw bytes over the TLS socket,
    no HTTP framing (iLO2 predates the HTTP-wrapped RIBCL that iLO3+ added).
    The '<?xml version="1.0"?>' header must land in its own TCP write,
    separate from the RIBCL body, or the firmware never replies.

    RIBCL's raw framing has no Content-Length/chunked terminator to detect
    completion from, and iLO2 holds the connection open well after it's
    done answering (same behavior as raw_request, see PROTOCOL.md) --
    confirmed by measurement, every single call was blocking for the full
    `timeout` even though the actual reply arrives almost immediately.
    So: once the first byte has arrived, treat `quiet_period` with no new
    data as "response finished" and stop, instead of always waiting out
    the whole `timeout`. Before any data arrives, still wait the full
    remaining `timeout` (that part -- connect + iLO2 processing the
    request -- can legitimately take a while).
    """
    s = _connect(host, port, timeout)
    s.sendall(b'<?xml version="1.0"?>\r\n')
    s.sendall(xml_body)
    data = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        s.settimeout(min(quiet_period, remaining) if data else remaining)
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            if data:
                break
            continue
        if not chunk:
            break
        data += chunk
    s.close()
    return data.decode(errors="replace")


def _dechunk(body):
    out = b""
    buf = body
    while True:
        nl = buf.find(b"\r\n")
        if nl == -1:
            break
        size_line = buf[:nl].split(b";")[0]
        try:
            size = int(size_line, 16)
        except ValueError:
            break
        if size == 0:
            break
        out += buf[nl + 2:nl + 2 + size]
        buf = buf[nl + 2 + size + 2:]
    return out


def request(host, method, path, headers=None, body=None, port=443, timeout=10):
    """Returns (status_line, headers_dict, body_bytes, raw_response_bytes)."""
    raw = raw_request(host, method, path, headers=headers, body=body, port=port, timeout=timeout)
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode(errors="replace")
    hdrs = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            hdrs[k.strip().decode().lower()] = v.strip().decode()
    if hdrs.get("transfer-encoding", "").lower() == "chunked":
        return status_line, hdrs, _dechunk(rest), raw
    return status_line, hdrs, rest, raw
