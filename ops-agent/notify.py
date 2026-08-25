#!/usr/bin/env python3
"""POST an alert to the shared HA/Proxmox webhook (only call this for severity >= warn).

Reuses the existing `ha-webhook` PVE notification target / automation.proxmox_alert
pipeline (PVE -> HA webhook -> phone), so ops-agent alerts show up the same way
PVE's own native alerts do, grouped under "proxmox" on the phone.

Usage: ./notify.py "<title>" "<message>" [severity]
"""
import json
import pathlib
import sys
import urllib.request

WEBHOOK_URL_FILE = pathlib.Path(__file__).parent / "webhook_url"


def notify(title: str, message: str, severity: str = "warn"):
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
