#!/usr/bin/env python3
"""Feed fetched log excerpts to the local Ollama model and get a severity verdict.

Usage: ./fetch_proxmox_log.py > report.txt; ./fetch_immich_log.py >> report.txt
       ./triage.py < report.txt
       or import as: triage.triage(report_text) -> (severity, summary)
"""
import json
import re
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"

PROMPT_TEMPLATE = """Log excerpts from a homelab Proxmox server and its Immich (photo backup) VM:

{report}

{history}

---
Do NOT describe, narrate, or summarize the log contents above. Your only job is to decide: \
does anything above need a human's attention right now? Most of the time nothing does -- \
routine reboots, healthy containers, application startup/init noise, and normal disk usage \
are NOT worth flagging. Only escalate for things that are actually broken, failing, or \
trending toward a real problem (e.g. a container crash-looping, a disk pool degraded, a \
filesystem above ~90% full, repeated task failures). Use the recent activity history above \
only as context (e.g. a thing already reported many times isn't a fresh emergency) -- keep \
judging today's data on its own merits.

Reply with ONLY these two lines, nothing else -- no explanation, no restating the logs:
SEVERITY: <none|info|warn|critical>
SUMMARY: <one short sentence, plain language>
"""


def triage(report: str, history: str = ""):
    prompt = PROMPT_TEMPLATE.format(report=report, history=history)
    # num_predict caps generation length -- without it, the model can ramble past
    # its own stop token (observed: 0.47 tok/s under host contention with no end
    # in sight, climbing toward the 4096-token context limit, i.e. hours) instead
    # of the ~2 sentences the prompt asks for. 80 tokens is plenty for the
    # requested format even at that measured worst-case rate (~170s); the
    # client-side timeout is a second, more generous backstop.
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 80},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=400) as resp:
        response_text = json.loads(resp.read())["response"]

    sev_match = re.search(r"SEVERITY:\s*(none|info|warn|critical)", response_text, re.IGNORECASE)
    sum_match = re.search(r"SUMMARY:\s*(.+)", response_text, re.IGNORECASE)

    if not sev_match:
        # Silently swallowing an unparseable response would defeat the point of
        # the agent -- treat "the model didn't answer sensibly" as worth a look.
        return "warn", f"ops-agent: triage response could not be parsed. Raw: {response_text[:300]}"

    severity = sev_match.group(1).lower()
    summary = sum_match.group(1).strip() if sum_match else "(no summary provided)"
    return severity, summary


if __name__ == "__main__":
    report_text = sys.stdin.read()
    result_severity, result_summary = triage(report_text)
    print(f"SEVERITY: {result_severity}")
    print(f"SUMMARY: {result_summary}")
