"""Minimal session-cookie auth for the web client.

Deliberately simple (this is a single-user home tool, not a multi-tenant
service): one shared username/password from the environment
(WEBAPP_USER/WEBAPP_PASSWORD -- separate from ILO_USER/ILO_PASSWORD, which
are the iLO2 hardware's own credentials, so a leaked dashboard session
never exposes those), an in-memory random-token session store, and a
per-IP lockout on repeated failed logins. Assumes TLS termination happens
in front of this (reverse proxy) -- see README for why that's required,
not optional, once this is reachable from the internet.
"""
import hmac
import os
import secrets
import time

SESSION_COOKIE = "ilo2_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days

_MAX_FAILURES = 5
_LOCKOUT_WINDOW = 5 * 60  # seconds


class AuthConfig:
    def __init__(self):
        self.user = os.environ.get("WEBAPP_USER")
        self.password = os.environ.get("WEBAPP_PASSWORD")

    @property
    def enabled(self):
        return bool(self.user and self.password)

    def check(self, user, password):
        if not self.enabled:
            return False
        # compare_digest on both fields (not just the password) so a wrong
        # username doesn't short-circuit faster than a wrong password would
        # -- avoids leaking which one was wrong via response timing.
        ok_user = hmac.compare_digest(user or "", self.user)
        ok_pass = hmac.compare_digest(password or "", self.password)
        return ok_user and ok_pass


class SessionStore:
    """Random-token sessions, kept in memory -- restarting the server logs
    everyone out, which is an acceptable trade for not needing any
    persistence/storage just for this."""

    def __init__(self):
        self._sessions = {}  # token -> expiry (monotonic-ish unix time)

    def create(self):
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + SESSION_TTL
        return token

    def validate(self, token):
        if not token:
            return False
        expiry = self._sessions.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token):
        self._sessions.pop(token, None)


class LoginRateLimiter:
    """Per-IP lockout after repeated failed logins. In-memory, single
    process -- fine at this scale, and resets on restart like sessions do."""

    def __init__(self):
        self._failures = {}  # ip -> list[timestamp]

    def is_locked_out(self, ip):
        self._prune(ip)
        return len(self._failures.get(ip, [])) >= _MAX_FAILURES

    def record_failure(self, ip):
        self._prune(ip)
        self._failures.setdefault(ip, []).append(time.time())

    def record_success(self, ip):
        self._failures.pop(ip, None)

    def _prune(self, ip):
        cutoff = time.time() - _LOCKOUT_WINDOW
        attempts = self._failures.get(ip)
        if not attempts:
            return
        pruned = [t for t in attempts if t > cutoff]
        if pruned:
            self._failures[ip] = pruned
        else:
            self._failures.pop(ip, None)


def parse_cookies(header_value):
    """Tiny Cookie-header parser -- stdlib's http.cookies.SimpleCookie
    would also work, but this is a two-line job and avoids pulling in its
    (much larger) surface for a single name=value pair."""
    cookies = {}
    if not header_value:
        return cookies
    for part in header_value.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.strip().partition("=")
        cookies[k] = v
    return cookies


def session_cookie_header(token, secure=True):
    attrs = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
             f"Max-Age={SESSION_TTL}"]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def clear_cookie_header(secure=True):
    attrs = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)
