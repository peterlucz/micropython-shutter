#!/usr/bin/env python3
"""Ask a cloud model (OpenAI gpt-4o-mini) to double-check a candidate alert
before it pages the phone. Only meant to be called for narrative (LLM-driven)
alerts -- floor-driven ones (container down, backup failed, disk genuinely
>=90%) are already deterministic and don't need a second opinion.

Usage: import as verify_with_cloud.verify(report, severity, summary) -> (confirmed, cloud_summary)
"""
import json
import pathlib
import re
import urllib.request

API_KEY_FILE = pathlib.Path(__file__).parent / "openai_api_key"
MODEL = "gpt-4o-mini"
API_URL = "https://api.openai.com/v1/chat/completions"

PROMPT_TEMPLATE = """A small local LLM monitoring a homelab (Proxmox host + Immich photo server) \
flagged the following as severity="{severity}":

"{summary}"

Here is the raw data it was given:

{report}

Is this actually a real, worth-paging-someone's-phone issue, or is it a misread/false positive/\
hallucination by the small model? This local model has repeatedly invented issues that don't \
exist in the data (e.g. claiming errors when a container's logs were completely empty, \
misjudging normal disk usage well under any real threshold as critical, or once inventing \
"corrupt image files" failing Immich jobs when the actual container logs for that window had \
zero errors and no container had restarted). A plausible-sounding claim is not evidence -- find \
the specific line(s) in the raw data below that back up the specific claim in the summary. If \
you cannot point to actual matching text (an error, a failed job, a stopped container, a \
concrete number over a real threshold), deny it as unconfirmed even if the claim sounds \
reasonable in isolation.

{history}

If recent history above shows this same issue already confirmed and notified multiple times \
without resolution, it's still real (a chronic unfixed problem) -- confirm it again rather than \
denying just because it's not new. If it shows this exact claim already denied before for the \
same reason, lean toward denying again unless the raw data shows something has changed.

Reply with ONLY these two lines, nothing else:
VERDICT: <confirm|deny>
SUMMARY: <if confirm, one clear sentence describing the real issue for a phone notification; if deny, a short reason it's a false positive>
"""


def verify(report: str, severity: str, summary: str, history: str = ""):
    api_key = API_KEY_FILE.read_text().strip()
    prompt = PROMPT_TEMPLATE.format(severity=severity, summary=summary, report=report, history=history)
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = json.loads(resp.read())["choices"][0]["message"]["content"]

    verdict_match = re.search(r"VERDICT:\s*(confirm|deny)", content, re.IGNORECASE)
    summary_match = re.search(r"SUMMARY:\s*(.+)", content, re.IGNORECASE | re.DOTALL)

    confirmed = bool(verdict_match) and verdict_match.group(1).lower() == "confirm"
    cloud_summary = summary_match.group(1).strip() if summary_match else content.strip()
    return confirmed, cloud_summary


if __name__ == "__main__":
    import sys
    report_text = sys.stdin.read()
    test_severity = sys.argv[1] if len(sys.argv) > 1 else "warn"
    test_summary = sys.argv[2] if len(sys.argv) > 2 else "(test claim)"
    ok, result_summary = verify(report_text, test_severity, test_summary)
    print("CONFIRMED" if ok else "DENIED", "-", result_summary)
