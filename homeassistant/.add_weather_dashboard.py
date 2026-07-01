#!/usr/bin/env python3
"""Create/update the Weather dashboard in HA via the WebSocket API.
Pure stdlib WebSocket client (plain ws:// — fine for local LAN)."""
import socket, os, base64, json, struct, sys
import yaml

HOST, PORT = "192.168.1.5", 8123
URL_PATH = "weather-dashboard"          # HA requires a hyphen in url_path
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(HERE, "token")).read().strip()
CFG = yaml.safe_load(open(os.path.join(HERE, "dashboards/weather.yaml")))

# ── minimal RFC6455 client ────────────────────────────────────────────────
class WS:
    def __init__(self, host, port, path):
        self.s = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(1)
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise RuntimeError("handshake failed: " + resp.decode(errors="replace"))

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def send(self, obj):
        data = json.dumps(obj).encode()
        b1 = 0x81                       # FIN + text
        length = len(data)
        header = struct.pack("!B", b1)
        if length < 126:
            header += struct.pack("!B", 0x80 | length)
        elif length < 65536:
            header += struct.pack("!BH", 0x80 | 126, length)
        else:
            header += struct.pack("!BQ", 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.s.sendall(header + mask + masked)

    def recv(self):
        while True:
            b0, b1 = self._recv_exact(2)
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x9:           # ping -> pong
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed")
            if opcode in (0x1, 0x2):
                return json.loads(payload.decode())


def main():
    ws = WS(HOST, PORT, "/api/websocket")
    assert ws.recv()["type"] == "auth_required"
    ws.send({"type": "auth", "access_token": TOKEN})
    auth = ws.recv()
    if auth["type"] != "auth_ok":
        print("AUTH FAILED:", auth); sys.exit(1)
    print("authenticated, HA", auth.get("ha_version"))

    _id = [0]
    def cmd(**kw):
        _id[0] += 1
        kw["id"] = _id[0]
        ws.send(kw)
        while True:
            m = ws.recv()
            if m.get("id") == _id[0] and m.get("type") == "result":
                return m

    # list existing dashboards (idempotent)
    res = cmd(type="lovelace/dashboards/list")
    existing = {d["url_path"]: d for d in res.get("result", [])}
    print("existing dashboards:", list(existing) or "(none)")

    if URL_PATH not in existing:
        res = cmd(type="lovelace/dashboards/create", url_path=URL_PATH,
                  title="Weather", icon="mdi:weather-partly-cloudy",
                  show_in_sidebar=True, require_admin=False)
        if not res.get("success"):
            print("CREATE FAILED:", res.get("error")); sys.exit(1)
        print("created dashboard:", URL_PATH)
    else:
        print("dashboard already exists, updating config:", URL_PATH)

    # save the view config (strip top-level title; config uses views[])
    config = {"title": CFG.get("title", "Weather"), "views": CFG["views"]}
    res = cmd(type="lovelace/config/save", url_path=URL_PATH, config=config)
    if not res.get("success"):
        print("CONFIG SAVE FAILED:", res.get("error")); sys.exit(1)
    print("config saved ✅  →  open http://%s:%d/%s" % (HOST, PORT, URL_PATH))


if __name__ == "__main__":
    main()
