#!/usr/bin/env python3
"""Bridge between Alertmanager's webhook and the existing HA notification
pipeline. Alertmanager's webhook_configs always POST its own fixed JSON
schema (status/alerts/labels/annotations) -- there's no way to template it
into an arbitrary shape -- so this reshapes each alert into the
{"title","message","severity"} format automation.proxmox_alert expects
(the same shape ops-agent/notify.py already sends), then forwards it to the
same HA webhook, so Prometheus-driven alerts land on the same phone
notification path as ops-agent's own.

Usage: ./alert_bridge.py   (listens on 0.0.0.0:9099)
Alertmanager config: webhook_configs: - url: "http://<this host>:9099/alert"
"""
import http.server
import json
import pathlib
import urllib.request

PORT = 9099
WEBHOOK_URL_FILE = pathlib.Path(__file__).parent / "webhook_url"

# Prometheus alert rules use "warning"/"critical" (see alert_rules.yml);
# map to ops-agent's none/info/warn/critical scale so both pipelines agree
# on what these words mean downstream.
SEVERITY_MAP = {"critical": "critical", "warning": "warn", "page": "critical"}


def _notify(title: str, message: str, severity: str):
    url = WEBHOOK_URL_FILE.read_text().strip()
    body = json.dumps({"title": title, "message": message, "severity": severity}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        for alert in payload.get("alerts", []):
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            alertname = labels.get("alertname", "Alert")
            summary = annotations.get("summary", alertname)
            severity = SEVERITY_MAP.get(labels.get("severity", "warning"), "warn")

            if alert.get("status") == "resolved":
                title, message, severity = "Prometheus: resolved", f"{summary} (resolved)", "info"
            else:
                title = "Prometheus: CRITICAL" if severity == "critical" else "Prometheus"
                message = summary

            try:
                _notify(title, message, severity)
            except Exception as e:
                print(f"forward to HA webhook failed: {e}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"alert_bridge listening on :{PORT}")
    server.serve_forever()
