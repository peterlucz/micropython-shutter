#!/usr/bin/env python3
"""Create/update a Lovelace dashboard in HA from a YAML file via the WebSocket API.

Usage:
  ./push_dashboard.py dashboards/shutters_test.yaml shutters-test "Shutter test" mdi:window-shutter-cog

Args: <yaml> <url-path> [title] [icon]   (url-path must contain a hyphen)
"""
import sys
import yaml

from ha_ws import WS, load_token

HOST, PORT = "192.168.1.5", 8123


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    yaml_path, url_path = sys.argv[1], sys.argv[2]
    cfg = yaml.safe_load(open(yaml_path))
    title = sys.argv[3] if len(sys.argv) > 3 else cfg.get("title", url_path)
    icon = sys.argv[4] if len(sys.argv) > 4 else "mdi:view-dashboard"
    if "-" not in url_path:
        sys.exit("url-path must contain a hyphen (HA requirement)")

    ws = WS(HOST, PORT, load_token())

    existing = {d["url_path"] for d in ws.cmd(type="lovelace/dashboards/list")}
    if url_path not in existing:
        ws.cmd(type="lovelace/dashboards/create", url_path=url_path, title=title,
               icon=icon, show_in_sidebar=True, require_admin=False)
        print("created dashboard:", url_path)
    else:
        print("updating existing dashboard:", url_path)

    config = {"title": cfg.get("title", title), "views": cfg["views"]}
    ws.cmd(type="lovelace/config/save", url_path=url_path, config=config)
    print(f"saved ✅  ->  http://{HOST}:{PORT}/{url_path}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ConnectionError) as e:
        sys.exit(f"FAILED: {e}")
