"""Shared minimal Home Assistant WebSocket API client — stdlib only.

HA's admin API (helper/dashboard/entity-registry management) is WebSocket-only,
so this implements just enough RFC 6455 (handshake, client-side masking, ping)
plus the HA auth handshake to run commands. Used by deploy_ha.py and
push_dashboard.py.

Usage:
    ws = WS(host, port, load_token())
    result = ws.cmd(type="config/entity_registry/list")
"""
import base64, json, os, socket, struct


def load_token(path=None):
    """Read the long-lived access token (default: ./token next to this file)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return open(path or os.path.join(here, "token")).read().strip()


class WS:
    def __init__(self, host, port, token):
        self.s = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /api/websocket HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(1)
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise RuntimeError("handshake failed: " + resp.decode(errors="replace"))
        assert self._recv()["type"] == "auth_required"
        self._send({"type": "auth", "access_token": token})
        if self._recv().get("type") != "auth_ok":
            raise RuntimeError("WS auth failed")
        self._id = 0

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def _send(self, obj):
        data = json.dumps(obj).encode()
        header = struct.pack("!B", 0x81)
        length = len(data)
        if length < 126:
            header += struct.pack("!B", 0x80 | length)
        elif length < 65536:
            header += struct.pack("!BH", 0x80 | 126, length)
        else:
            header += struct.pack("!BQ", 0x80 | 127, length)
        mask = os.urandom(4)
        self.s.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        while True:
            b0, b1 = self._recv_exact(2)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x9:
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed")
            if opcode in (0x1, 0x2):
                return json.loads(payload.decode())

    def cmd(self, **kw):
        """Run one command, wait for its result; raise RuntimeError on failure."""
        self._id += 1
        kw["id"] = self._id
        self._send(kw)
        while True:
            m = self._recv()
            if m.get("id") == self._id and m.get("type") == "result":
                if not m.get("success"):
                    raise RuntimeError(f"WS {kw.get('type')} failed: {m.get('error')}")
                return m.get("result")
