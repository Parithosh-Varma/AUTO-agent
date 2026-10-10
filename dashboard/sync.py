"""Runner <-> backend sync. Best-effort, offline-safe, deadline-preserving.

- push(): ships unsynced events/audit rows to POST <api>/api/ingest.
  Payments are NEVER pushed (D1 payments come only from the Dodo webhook or
  server-side poll). Cursor advances only on HTTP 200 + confirmed receipt.
- drain_controls(): applies pending operator control requests via Supervisor.
Both are no-ops without DASHBOARD_API_URL + DASHBOARD_TOKEN.
"""
import json
import time
import urllib.request

TIMEOUT = 8


def _base(cfg):
    url = (getattr(cfg, "dashboard_api_url", "") or "").rstrip("/")
    tok = (getattr(cfg, "dashboard_token", "") or "").strip()
    if not url or not tok:
        return None, None
    return url, tok


def _post(url, token, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url + path, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def _get(url, token, path):
    req = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def push(cfg, store):
    """Returns (pushed_events, pushed_audit) or (0, 0) when unconfigured/offline."""
    url, token = _base(cfg)
    if not url:
        return 0, 0
    try:
        cursor = store.get_state("sync_cursor", {"events": 0, "audit": 0}) or {}
        events = store.events_since(cursor.get("events", 0))
        audit = store.audit_since(cursor.get("audit", 0))
        if not events and not audit:
            return 0, 0
        status, resp = _post(url, token, "/api/ingest",
                             {"events": events, "audit": audit})
        if status != 200 or not resp.get("ok"):
            return 0, 0
        if events:
            cursor["events"] = max(r["id"] for r in events)
        if audit:
            cursor["audit"] = max(r["id"] for r in audit)
        store.set_state_internal("sync_cursor", cursor)
        return len(events), len(audit)
    except Exception:
        return 0, 0


def drain_controls(cfg, store, supervisor):
    """Apply operator control requests queued in the backend. Returns applied count."""
    url, token = _base(cfg)
    if not url:
        return 0
    try:
        status, resp = _get(url, token, "/api/controls/pending")
        if status != 200:
            return 0
        applied = 0
        for req in (resp.get("requests") or [])[:10]:
            rid = req.get("id")
            action = str(req.get("action", "")).lower()
            actor = str(req.get("actor", "dashboard"))[:40]
            try:
                if action == "pause":
                    supervisor.pause(source=actor)
                elif action == "resume":
                    supervisor.resume(source=actor)
                elif action == "shutdown":
                    supervisor.stop(source=actor)
                elif action == "diagnostic":
                    store.enqueue("diagnostic", {"at": time.time(), "by": actor})
                else:
                    continue
                store.audit_log(actor, f"control:{action}", True, f"applied from backend #{rid}")
                _post(url, token, f"/api/controls/requests/{int(rid)}/ack", {"actor": actor})
                applied += 1
            except Exception:
                continue
        return applied
    except Exception:
        return 0
