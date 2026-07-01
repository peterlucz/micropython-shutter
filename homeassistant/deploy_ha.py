#!/usr/bin/env python3
"""Deploy a Home Assistant package YAML to a running HA instance over its API.

The box has no SSH / file access, so YAML packages can't be dropped into
<config>/packages/. This tool keeps the YAML file as the source of truth (git)
and pushes its contents via the API instead:

  * helpers (input_number / input_datetime / input_boolean / input_select /
    input_text) -> WebSocket storage-collection API, with entity_id renamed to
    match the YAML key so automation references resolve.
  * automations -> REST config API (POST /api/config/automation/config/<id>),
    then automation.reload.

All operations are idempotent: existing entities are updated in place, not
duplicated. Use --delete to tear the same entities down again.

Usage:
  ./deploy_ha.py packages/shutters.yaml                 # deploy everything
  ./deploy_ha.py packages/shutters.yaml --dry-run       # show plan only
  ./deploy_ha.py packages/shutters.yaml \
        --automations shutters_update_daily_temps        # subset by id
  ./deploy_ha.py packages/shutters.yaml --delete ...     # remove them

Host/token default to 192.168.1.5:8123 and the ./token file next to this script.
"""
import argparse, base64, json, os, socket, struct, sys, time, urllib.error, urllib.request

HELPER_DOMAINS = ("input_number", "input_datetime", "input_boolean",
                  "input_select", "input_text", "counter", "timer")


# ── REST ───────────────────────────────────────────────────────────────────
class Rest:
    def __init__(self, base, token):
        self.base = base
        self.hdr = {"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"}

    def __call__(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=self.hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")


# ── minimal RFC6455 WebSocket client (from .add_weather_dashboard.py) ────────
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
        self._id += 1
        kw["id"] = self._id
        self._send(kw)
        while True:
            m = self._recv()
            if m.get("id") == self._id and m.get("type") == "result":
                if not m.get("success"):
                    raise RuntimeError(f"WS {kw.get('type')} failed: {m.get('error')}")
                return m.get("result")


# ── helpers deploy/delete ────────────────────────────────────────────────────
def registry_map(ws, domain):
    """entity_id -> unique_id (collection item id) for a helper domain."""
    return {e["entity_id"]: e["unique_id"]
            for e in ws.cmd(type="config/entity_registry/list")
            if e.get("platform") == domain}


def deploy_helper(ws, domain, object_id, cfg, dry):
    want = f"{domain}.{object_id}"
    existing = registry_map(ws, domain).get(want)
    if existing:
        print(f"  update  {want}")
        if not dry:
            ws.cmd(type=f"{domain}/update", **{f"{domain}_id": existing}, **cfg)
        return
    print(f"  create  {want}")
    if dry:
        return
    item = ws.cmd(type=f"{domain}/create", **cfg)
    new_uid = item["id"]
    # created entity_id is slug(name); rename it to match the YAML key
    got = next((eid for eid, uid in registry_map(ws, domain).items()
                if uid == new_uid), None)
    if got and got != want:
        ws.cmd(type="config/entity_registry/update",
               entity_id=got, new_entity_id=want)
        print(f"          renamed {got} -> {want}")


def delete_helper(ws, domain, object_id, dry):
    want = f"{domain}.{object_id}"
    existing = registry_map(ws, domain).get(want)
    if not existing:
        print(f"  absent  {want}")
        return
    print(f"  delete  {want}")
    if not dry:
        ws.cmd(type=f"{domain}/delete", **{f"{domain}_id": existing})


# ── automations deploy/delete ────────────────────────────────────────────────
def deploy_automation(rest, auto, dry):
    aid = auto["id"]
    body = {k: v for k, v in auto.items() if k != "id"}
    print(f"  push    automation:{aid}")
    if dry:
        return
    st, res = rest("POST", f"/api/config/automation/config/{aid}", body)
    if st != 200:
        raise RuntimeError(f"automation {aid} write failed: {st} {res}")


def delete_automation(rest, aid, dry):
    print(f"  delete  automation:{aid}")
    if dry:
        return
    st, res = rest("DELETE", f"/api/config/automation/config/{aid}")
    if st not in (200, 404):
        raise RuntimeError(f"automation {aid} delete failed: {st} {res}")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Deploy an HA package YAML via the API.")
    ap.add_argument("package", help="path to the package YAML")
    ap.add_argument("--host", default="192.168.1.5")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--token", default=None, help="path to token file (default ./token)")
    ap.add_argument("--delete", action="store_true", help="remove the entities instead of deploying")
    ap.add_argument("--automations", help="comma-separated automation ids to include (default all)")
    ap.add_argument("--helpers", help="comma-separated helper object_ids to include (default all)")
    ap.add_argument("--no-reload", action="store_true", help="skip automation.reload")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args()

    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML required: pip install pyyaml")

    here = os.path.dirname(os.path.abspath(__file__))
    token = open(args.token or os.path.join(here, "token")).read().strip()
    doc = yaml.safe_load(open(args.package)) or {}

    auto_filter = set(args.automations.split(",")) if args.automations else None
    help_filter = set(args.helpers.split(",")) if args.helpers else None

    # collect helpers: {domain: {object_id: cfg}}
    helpers = []
    for domain in HELPER_DOMAINS:
        for object_id, cfg in (doc.get(domain) or {}).items():
            if help_filter and object_id not in help_filter:
                continue
            helpers.append((domain, object_id, cfg or {}))

    # collect automations
    autos = []
    for a in (doc.get("automation") or []):
        if "id" not in a:
            print(f"  skip    automation without id: {a.get('alias')!r}", file=sys.stderr)
            continue
        if auto_filter and a["id"] not in auto_filter:
            continue
        autos.append(a)

    # warn about unsupported sections
    known = set(HELPER_DOMAINS) | {"automation", "homeassistant"}
    for k in doc:
        if k not in known:
            print(f"  NOTE    unsupported section '{k}:' skipped "
                  f"(templates / groups / platforms can't be pushed as storage entities)",
                  file=sys.stderr)

    verb = "DELETE" if args.delete else "DEPLOY"
    dry = " (dry-run)" if args.dry_run else ""
    print(f"{verb}{dry}  {args.package}  ->  {args.host}:{args.port}")
    print(f"  {len(helpers)} helper(s), {len(autos)} automation(s)\n")

    ws = WS(args.host, args.port, token)
    rest = Rest(f"http://{args.host}:{args.port}", token)

    if args.delete:
        for a in autos:
            delete_automation(rest, a["id"], args.dry_run)
        for domain, oid, _ in helpers:
            delete_helper(ws, domain, oid, args.dry_run)
    else:
        for domain, oid, cfg in helpers:
            deploy_helper(ws, domain, oid, cfg, args.dry_run)
        for a in autos:
            deploy_automation(rest, a, args.dry_run)

    if autos and not args.no_reload and not args.dry_run:
        st, _ = rest("POST", "/api/services/automation/reload", {})
        print(f"\n  automation.reload -> {st}")

    print("\nDone." + (" (nothing changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
