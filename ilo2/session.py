"""HP iLO2 web login + RIBCL power control + Remote Console parameter fetch."""
import base64
import re
import xml.etree.ElementTree as ET

from . import legacy_tls


class LoginError(Exception):
    pass


class SessionExhaustedError(LoginError):
    """iLO2's web-UI session pool (observed: ~2-4 slots) is full --
    sessionkey="NONEAVAILABLE" on the login page. RIBCL calls (power,
    health, UID) are unaffected, since they use a separate per-request
    auth path; only the web-UI/Remote-Console login is blocked until a
    slot frees up or reset_management_processor() clears the pool."""
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
        if key == "NONEAVAILABLE":
            raise SessionExhaustedError(
                "iLO2 web-UI session pool exhausted (sessionkey=NONEAVAILABLE)")

        token = f"{idx}:{self._b64(self.username)}:{self._b64(self.password)}:{key}"
        raw2 = legacy_tls.raw_request(
            self.host, "GET", "/index.htm",
            headers={"Cookie": f"hp-iLO-Login={token}"}, port=self.port)
        m = re.search(rb"Set-Cookie: hp-iLO-Session=([^;]+)", raw2)
        if not m:
            raise LoginError("login rejected (bad credentials, or sessionkey expired)")
        self.session_cookie = m.group(1).decode()
        return self.session_cookie

    def logout(self):
        """Releases this session's slot in iLO2's small web-UI session pool
        (see SessionExhaustedError). Best-effort and never raises -- meant
        to be called opportunistically before a fresh login() (e.g. on
        reconnect), so a network hiccup here shouldn't block the retry
        that follows."""
        if not self.session_cookie:
            return
        try:
            self._get("/logout.htm")
        except Exception:
            pass
        finally:
            self.session_cookie = None

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

    # ---- UID (blue "locate" LED) ----------------------------------------
    def get_uid_status(self):
        resp = self._ribcl('<SERVER_INFO MODE="read"><GET_UID_STATUS/></SERVER_INFO>')
        m = re.search(r'GET_UID_STATUS UID="(\w+)"', resp)
        return m.group(1) if m else None

    def uid_on(self):
        return self._ribcl('<SERVER_INFO MODE="write"><UID_CONTROL UID="Yes"/></SERVER_INFO>')

    def uid_off(self):
        return self._ribcl('<SERVER_INFO MODE="write"><UID_CONTROL UID="No"/></SERVER_INFO>')

    # ---- identity ---------------------------------------------------------
    def get_fw_version(self):
        resp = self._ribcl('<RIB_INFO MODE="read"><GET_FW_VERSION/></RIB_INFO>')
        m = re.search(r"<GET_FW_VERSION\s+([^/]*)/>", resp, re.DOTALL)
        if not m:
            return {}
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1)))
        return {
            "firmware_version": attrs.get("FIRMWARE_VERSION"),
            "firmware_date": attrs.get("FIRMWARE_DATE"),
            "management_processor": attrs.get("MANAGEMENT_PROCESSOR"),
            "license_type": attrs.get("LICENSE_TYPE"),
        }

    def get_server_name(self):
        resp = self._ribcl('<SERVER_INFO MODE="read"><GET_SERVER_NAME/></SERVER_INFO>')
        m = re.search(r'SERVER_NAME VALUE="([^"]*)"', resp)
        return m.group(1) if m else None

    # ---- embedded health (fans, temps, PSUs) -------------------------------
    @staticmethod
    def _extract_block(resp, tag):
        """RIBCL responses are several concatenated `<?xml?><RIBCL>...</RIBCL>`
        documents (one ack per element iLO2's streaming parser processes),
        not one well-formed document -- so pull out just the `<TAG>...</TAG>`
        fragment we care about and parse *that* on its own."""
        m = re.search(rf"<{tag}[ >].*?</{tag}>", resp, re.DOTALL)
        if not m:
            return None
        try:
            return ET.fromstring(m.group(0))
        except ET.ParseError:
            return None

    def get_embedded_health(self):
        resp = self._ribcl('<SERVER_INFO MODE="read"><GET_EMBEDDED_HEALTH/></SERVER_INFO>')
        root = self._extract_block(resp, "GET_EMBEDDED_HEALTH_DATA")
        if root is None:
            return {"fans": [], "temperatures": [], "power_supplies": [], "summary": {}}

        fans = [{
            "label": fan.find("LABEL").get("VALUE"),
            "zone": fan.find("ZONE").get("VALUE"),
            "status": fan.find("STATUS").get("VALUE"),
            "speed": fan.find("SPEED").get("VALUE"),
        } for fan in root.findall("./FANS/FAN")]

        temperatures = []
        for temp in root.findall("./TEMPERATURE/TEMP"):
            reading = temp.find("CURRENTREADING")
            if reading is None or reading.get("VALUE") == "N/A":
                continue
            temperatures.append({
                "label": temp.find("LABEL").get("VALUE"),
                "location": temp.find("LOCATION").get("VALUE"),
                "status": temp.find("STATUS").get("VALUE"),
                "current": reading.get("VALUE"),
                "caution": temp.find("CAUTION").get("VALUE") if temp.find("CAUTION") is not None else None,
                "critical": temp.find("CRITICAL").get("VALUE") if temp.find("CRITICAL") is not None else None,
            })

        power_supplies = [{
            "label": psu.find("LABEL").get("VALUE"),
            "status": psu.find("STATUS").get("VALUE"),
        } for psu in root.findall("./POWER_SUPPLIES/SUPPLY")]

        summary = {}
        for el in root.findall("./HEALTH_AT_A_GLANCE/*"):
            key = el.tag.lower()
            for attr_name, attr_val in el.attrib.items():
                summary_key = key if attr_name == "STATUS" else f"{key}_{attr_name.lower()}"
                summary[summary_key] = attr_val

        return {
            "fans": fans,
            "temperatures": temperatures,
            "power_supplies": power_supplies,
            "summary": summary,
        }
