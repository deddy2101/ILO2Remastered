"""RC4 keystream as used by the HP iLO2 Remote Console applet.

Reverse-engineered from com.hp.ilo2.remcons.RC4 / VMD5 (rc175p11.jar).
VMD5 is a plain, unmodified MD5 implementation (verified against the RFC 1321
constants), so hashlib.md5 is used directly here.

Key schedule: given a 16-byte "pre" key (sent by the server as INFOB/INFOC,
hex-encoded), the actual RC4 key is MD5(pre + previous_key), where
previous_key starts out as 16 zero bytes. The console can ask the client to
rotate keys mid-session ("change key" firmware command); each rotation
re-derives key = MD5(pre + key) and re-runs the KSA. This must mirror the
Java side exactly or the two ends desync.
"""
import hashlib


class RC4:
    def __init__(self, pre: bytes):
        if len(pre) != 16:
            raise ValueError("pre-key must be 16 bytes")
        self.pre = bytes(pre)
        self.key = b"\x00" * 16
        self.update_key()

    def update_key(self):
        self.key = hashlib.md5(self.pre + self.key).digest()
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + self.key[i % 16]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self.S = S
        self.i = 0
        self.j = 0

    def next_byte(self) -> int:
        self.i = (self.i + 1) & 0xFF
        self.j = (self.j + self.S[self.i]) & 0xFF
        self.S[self.i], self.S[self.j] = self.S[self.j], self.S[self.i]
        return self.S[(self.S[self.i] + self.S[self.j]) & 0xFF]

    def xor(self, data: bytes) -> bytes:
        return bytes(b ^ self.next_byte() for b in data)
