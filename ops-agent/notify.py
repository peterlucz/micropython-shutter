#!/usr/bin/env python3
"""POST an alert to the shared HA/Proxmox webhook (only call this for severity >= warn).

Reuses the existing `ha-webhook` PVE notification target / automation.proxmox_alert
pipeline (PVE -> HA webhook -> phone), so ops-agent alerts show up the same way
PVE's own native alerts do, grouped under "proxmox" on the phone.

Usage: ./notify.py "<title>" "<message>" [severity]

Guarded 2026-09-11: this hits the *real* webhook -> phone regardless of what
machine runs it -- there's nothing localhost-scoped about it. Found the hard
way: manually running run_all.py from a workstation to test a fix (Ollama is
only reachable from CT103 itself, so triage() 404'd) still paged the real
phone via run_all.py's own top-level "a broken pipeline is worth knowing
about" exception handler, which calls this. Only actually notify when
running on the real ops-agent box; anywhere else, print what *would* have
been sent and return -- so ad-hoc/manual runs during development stay
harmless by default. Set OPS_AGENT_FORCE_NOTIFY=1 to override (e.g. to
actually test the phone-delivery path from elsewhere on purpose).
"""
import json
import os
import pathlib
import socket
import sys
import urllib.request

WEBHOOK_URL_FILE = pathlib.Path(__file__).parent / "webhook_url"
EXPECTED_HOSTNAME = "ops-agent"


def notify(title: str, message: str, severity: str = "warn"):
    hostname = socket.gethostname()
    if hostname != EXPECTED_HOSTNAME and os.environ.get("OPS_AGENT_FORCE_NOTIFY") != "1":
        print(
            f"notify: skipping real webhook POST -- running on '{hostname}', not "
            f"'{EXPECTED_HOSTNAME}' (looks like a manual/off-target run). Would have sent: "
            f"[{severity}] {title}: {message}\n"
            "(set OPS_AGENT_FORCE_NOTIFY=1 to send for real anyway)",
            file=sys.stderr,
        )
        return
    url = WEBHOOK_URL_FILE.read_text().strip()
    body = json.dumps({"title": title, "message": message, "severity": severity}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    arg_title, arg_message = sys.argv[1], sys.argv[2]
    arg_severity = sys.argv[3] if len(sys.argv) > 3 else "warn"
    notify(arg_title, arg_message, arg_severity)
    print("notified")
