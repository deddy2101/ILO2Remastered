"""HP iLO2 web login + RIBCL power control + Remote Console parameter fetch."""
import base64
import re

from . import legacy_tls


class LoginError(Exception):
    pass


class IloSession:
    def __init__(self, host, username, password, port=443):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.session_cookie = None

    def _b64(self, s):
        return base64.b64encode(s.encode()).decode()

    def login(self):
        """Web UI login: fetch a one-time sessionkey, then trade
        user/pass/sessionkey for a session cookie. This must happen in two
        requests in quick succession -- the sessionkey appears to be a
        short-lived nonce.
        """
        raw = legacy_tls.raw_request(self.host, "GET", "/", port=self.port)
        text = raw.decode(errors="replace")
        m_key = re.search(r'sessionkey="([^"]+)"', text)
        m_idx = re.search(r'sessionindex="([^"]+)"', text)
        if not m_key or not m_idx:
            raise LoginError("could not find sessionkey on login page")
        key, idx = m_key.group(1), m_idx.group(1)

        token = f"{idx}:{self._b64(self.username)}:{self._b64(self.password)}:{key}"
        raw2 = legacy_tls.raw_request(
            self.host, "GET", "/index.htm",
            headers={"Cookie": f"hp-iLO-Login={token}"}, port=self.port)
        m = re.search(rb"Set-Cookie: hp-iLO-Session=([^;]+)", raw2)
        if not m:
            raise LoginError("login rejected (bad credentials, or sessionkey expired)")
        self.session_cookie = m.group(1).decode()
        return self.session_cookie

    def _get(self, path):
        if not self.session_cookie:
            self.login()
        status, hdrs, body, raw = legacy_tls.request(
            self.host, "GET", path,
            headers={"Cookie": f"hp-iLO-Session={self.session_cookie}"},
            port=self.port)
        return body.decode(errors="replace")

    def fetch_console_params(self):
        """Loads the Remote Console frame page and extracts the applet
        PARAM values needed to open the KVM socket on port 23: the login
        ticket (INFO0/1), whether the DVC stream is RC4-encrypted (INFOA)
        and the two 16-byte RC4 seed keys (INFOB = decrypt, INFOC = encrypt)
        plus the key index (INFOD).
        """
        text = self._get("/drc2fram.htm?restart=1")

        def val(name):
            m = re.search(name + r'\s*=\s*"?([^";\n]+)"?\s*;', text)
            return m.group(1) if m else None

        info0 = val("info0")
        if info0 is None:
            raise LoginError("drc2fram.htm did not contain INFO0 -- not logged in?")
        params = {
            "info0": info0.strip(),
            "info1": val("info1"),
            "info6": val("info6"),
            "infoa": val("infoa"),
            "infob": val("infob"),
            "infoc": val("infoc"),
            "infod": val("infod"),
        }
        return params

    # ---- RIBCL power control -------------------------------------------
    def _ribcl(self, fragment):
        xml = (f'<RIBCL VERSION="2.0">'
               f'<LOGIN USER_LOGIN="{self.username}" PASSWORD="{self.password}">'
               f'{fragment}'
               f'</LOGIN></RIBCL>\r\n').encode()
        return legacy_tls.ribcl_raw(self.host, xml, port=self.port)

    def get_power_status(self):
        resp = self._ribcl('<SERVER_INFO MODE="read"><GET_HOST_POWER_STATUS/></SERVER_INFO>')
        m = re.search(r'HOST_POWER="(\w+)"', resp)
        return m.group(1) if m else None

    def power_on(self):
        return self._ribcl('<SERVER_INFO MODE="write"><SET_HOST_POWER HOST_POWER="Yes"/></SERVER_INFO>')

    def power_off(self):
        """Graceful OS shutdown (press-and-release power button)."""
        return self._ribcl('<SERVER_INFO MODE="write"><SET_HOST_POWER HOST_POWER="No"/></SERVER_INFO>')

    def press_power_button(self):
        return self._ribcl('<SERVER_INFO MODE="write"><PRESS_PWR_BTN/></SERVER_INFO>')

    def hold_power_button(self):
        """Hard power-off (press-and-hold)."""
        return self._ribcl('<SERVER_INFO MODE="write"><HOLD_PWR_BTN/></SERVER_INFO>')

    def cold_boot(self):
        return self._ribcl('<SERVER_INFO MODE="write"><COLD_BOOT_SERVER/></SERVER_INFO>')

    def warm_boot(self):
        """Reset (Ctrl-Alt-Del equivalent)."""
        return self._ribcl('<SERVER_INFO MODE="write"><RESET_SERVER/></SERVER_INFO>')

    def reset_management_processor(self):
        """Reboots the iLO2 controller itself (NOT the host server). Useful
        to clear stuck web-UI sessions ('sessionkey="NONEAVAILABLE"') -- the
        session pool is tiny and each login holds a slot until it times out.
        The iLO2 web UI/console is unreachable for ~30-60s afterwards."""
        return self._ribcl('<RIB_INFO MODE="write"><RESET_RIB/></RIB_INFO>')
