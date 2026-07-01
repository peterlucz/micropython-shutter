#!/usr/bin/env python3
"""Create/update a Lovelace dashboard in HA from a YAML file via the WebSocket API.
Generic version of .add_weather_dashboard.py.

Usage:
  ./push_dashboard.py dashboards/shutters_test.yaml shutters-test "Shutter test" mdi:window-shutter-cog

Args: <yaml> <url-path> [title] [icon]   (url-path must contain a hyphen)
"""
import base64, json, os, socket, struct, sys
import yaml

HOST, PORT = "192.168.1.5", 8123
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(HERE, "token")).read().strip()


class WS:
    def __init__(self, host, port, path):
        self.s = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
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

    def recv(self):
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


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    yaml_path, url_path = sys.argv[1], sys.argv[2]
    cfg = yaml.safe_load(open(yaml_path))
    title = sys.argv[3] if len(sys.argv) > 3 else cfg.get("title", url_path)
    icon = sys.argv[4] if len(sys.argv) > 4 else "mdi:view-dashboard"
    if "-" not in url_path:
        sys.exit("url-path must contain a hyphen (HA requirement)")

    ws = WS(HOST, PORT, "/api/websocket")
    assert ws.recv()["type"] == "auth_required"
    ws.send({"type": "auth", "access_token": TOKEN})
    if ws.recv().get("type") != "auth_ok":
        sys.exit("AUTH FAILED")

    _id = [0]
    def cmd(**kw):
        _id[0] += 1
        kw["id"] = _id[0]
        ws.send(kw)
        while True:
            m = ws.recv()
            if m.get("id") == _id[0] and m.get("type") == "result":
                return m

    existing = {d["url_path"] for d in cmd(type="lovelace/dashboards/list").get("result", [])}
    if url_path not in existing:
        res = cmd(type="lovelace/dashboards/create", url_path=url_path, title=title,
                  icon=icon, show_in_sidebar=True, require_admin=False)
        if not res.get("success"):
            sys.exit(f"CREATE FAILED: {res.get('error')}")
        print("created dashboard:", url_path)
    else:
        print("updating existing dashboard:", url_path)

    config = {"title": cfg.get("title", title), "views": cfg["views"]}
    res = cmd(type="lovelace/config/save", url_path=url_path, config=config)
    if not res.get("success"):
        sys.exit(f"CONFIG SAVE FAILED: {res.get('error')}")
    print(f"saved ✅  ->  http://{HOST}:{PORT}/{url_path}")


if __name__ == "__main__":
    main()
