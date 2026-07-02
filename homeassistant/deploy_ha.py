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
import argparse, json, sys, urllib.error, urllib.request

from ha_ws import WS, load_token

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

    token = load_token(args.token)
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
