"""Authenticated observability API for the 7-day experiment.

Shared logic used by the local stdlib server (serve()) and mirrored by
cloudflare/worker.js against D1. All routes except /api/health require
`Authorization: Bearer <DASHBOARD_TOKEN>` (fail-closed when unset).

Security rules enforced here:
- Revenue is computed ONLY from provider-origin payment rows; agent-written
  values are never trusted (no write endpoint for payments exists).
- State changes route exclusively through Supervisor (pause/resume/shutdown).
  Caps, deadline, and approvals cannot be altered via this API.
- Every response passes through redact.scrub_dict; customer data (raw_json,
  emails) is never selected.
"""
import hmac
import json
import time
import urllib.parse

from dashboard.redact import scrub, scrub_dict

RATE_MAX = 120          # GET requests per IP per minute
RATE_CONTROL_MAX = 10   # state-changing requests per IP per minute
MAX_BODY = 16 * 1024

CATEGORIES = ("decision", "tool", "deployment", "error", "retry", "goal",
              "checkpoint", "audit", "notification", "control")
SEVERITIES = ("info", "warn", "error")


class AuthError(Exception):
    pass


class RateLimited(Exception):
    pass


class BadRequest(Exception):
    pass


def authenticate(headers, cfg):
    token = (cfg.dashboard_token or "").strip()
    if not token:
        return False
    auth = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "authorization":
            auth = str(v)
            break
    if not auth.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


class RateLimiter:
    def __init__(self, now=None):
        self.hits = {}
        self._now = now or time.time

    def check(self, ip, control=False):
        now = self._now()
        window = now // 60
        key = (ip, "c" if control else "r", window)
        self.hits[key] = self.hits.get(key, 0) + 1
        limit = RATE_CONTROL_MAX if control else RATE_MAX
        # prune old windows opportunistically
        if len(self.hits) > 1024:
            self.hits = {k: v for k, v in self.hits.items() if k[2] >= window - 1}
        if self.hits[key] > limit:
            raise RateLimited("rate limit exceeded")


def _int_param(q, name, default, lo, hi):
    try:
        v = int(q.get(name, default))
    except (TypeError, ValueError):
        raise BadRequest(f"invalid {name}")
    return max(lo, min(hi, v))


def overview(cfg, store, sup):
    cycle = int(store.get_state("cycle", 0) or 0)
    st = sup.status(cycle)
    goal = store.active_goal()
    rev = revenue_summary(cfg, store)
    # next planned action: last decision event, else brain stage hint
    next_action = "-"
    evts, _ = store.list_events(category="decision", page=1, per_page=1)
    if evts:
        next_action = evts[0]["summary"][:200]
    # last successful cycle / most recent error from actions
    last_ok, last_err = None, None
    for a in store.recent_actions(50):
        if last_ok is None and a["ok"]:
            last_ok = {"cycle": a["cycle"], "tool": f"{a['tool']}.{a['op']}",
                       "at": a["created_at"]}
        if last_err is None and not a["ok"]:
            last_err = {"cycle": a["cycle"], "tool": f"{a['tool']}.{a['op']}",
                        "summary": scrub(str(a["output_text"])[:200]), "at": a["created_at"]}
        if last_ok and last_err:
            break
    evts_all, _ = store.list_events(page=1, per_page=1)
    last_refresh = evts_all[0]["ts"] if evts_all else st["deadline_at"] - st["remaining_hours"] * 3600
    stale = (time.time() - last_refresh) > max(60, cfg.dashboard_poll_s * 6)
    return scrub_dict({
        "status": st["experiment_status"],
        "terminal_reason": st["terminal_reason"],
        "started_at": float(sup.started_at),
        "deadline_at": st["deadline_at"],
        "elapsed_hours": st["elapsed_hours"],
        "remaining_hours": st["remaining_hours"],
        "target_usd": cfg.target_revenue_usd,
        "verified_gross_usd": rev["gross_usd"],
        "revenue": rev,
        "usage": st["usage"],
        "caps": store.get_state("caps", {}),
        "current_goal": goal,
        "next_action": next_action,
        "last_ok": last_ok,
        "last_error": last_err,
        "last_event_ts": last_refresh,
        "stale": stale,
        "kill_switch": st["kill_switch"],
        "sandbox": cfg.sandbox_mode,
        "live_allowed": st["live_allowed"],
    })


def revenue_summary(cfg, store):
    rows = store.conn.execute(
        "SELECT dodo_payment_id, amount, currency, status, is_test, is_refunded,"
        " is_self_purchase, origin, verified_at FROM payments"
        " WHERE origin='dodo_api' ORDER BY verified_at DESC LIMIT 200").fetchall()
    txns, gross, refunded = [], 0.0, 0.0
    for r in rows:
        d = dict(r)
        genuine = (d["origin"] == "dodo_api" and d["status"] == "succeeded"
                   and d["currency"] == "USD" and not d["is_test"]
                   and not d["is_refunded"] and not d["is_self_purchase"])
        excluded = bool(d["is_refunded"])
        if genuine:
            gross += float(d["amount"] or 0)
        if d["origin"] == "dodo_api" and excluded:
            refunded += float(d["amount"] or 0)
        txns.append({"id": d["dodo_payment_id"], "amount": d["amount"],
                     "currency": d["currency"], "status": d["status"],
                     "genuine": genuine, "verified_at": d["verified_at"]})
    fee_pct = float(cfg.dodo_fee_pct or 0)
    if fee_pct > 0:
        fee_est, fee_basis = round(gross * fee_pct / 100.0, 2), "estimate"
    else:
        fee_est, fee_basis = 0.0, "unavailable"
    usage = store.get_state("usage", {}) or {}
    spend = float(usage.get("spend_usd", 0) or 0)
    profit = round(gross - refunded - (fee_est if fee_basis == "estimate" else 0) - spend, 2)
    return {
        "gross_usd": round(gross, 2),
        "refunded_usd": round(refunded, 2),
        "fees_usd": fee_est,
        "fees_basis": fee_basis,  # 'estimate' | 'unavailable' (never presented as provider data)
        "profit_estimate_usd": profit,
        "profit_basis": "estimate",
        "spend_usd": spend,
        "basis": "provider",  # gross/refunded come from authenticated provider rows
        "transactions": txns,
    }


def goals_view(store):
    out = []
    for g in store.list_goals():
        tasks = store.list_tasks(goal_id=g["id"])
        out.append({
            "id": g["id"], "description": scrub(g["description"]),
            "status": g["status"], "score": g["score"],
            "why_selected": {
                "score": g["score"], "novelty": g["novelty"],
                "feasibility": g["feasibility"], "usefulness": g["usefulness"],
                "cost": g["cost"],
            },
            "tasks": tasks,
            "created_at": g["created_at"], "updated_at": g["updated_at"],
        })
    return out


def infra_view(cfg, store):
    q = store.conn.execute(
        "SELECT status, COUNT(*) AS c, COALESCE(SUM(attempts),0) AS r FROM queue"
        " GROUP BY status").fetchall()
    queue = {r["status"]: {"count": r["c"], "retries": r["r"]} for r in q}
    failed_jobs = store.conn.execute(
        "SELECT * FROM queue WHERE status='failed' ORDER BY id DESC LIMIT 20").fetchall()
    actions = store.recent_actions(200)
    llm_fail = sum(1 for a in actions if a["tool"] == "llm" and not a["ok"])
    deploys, _ = store.list_events(category="deployment", page=1, per_page=10)
    checkpoints, _ = store.list_events(category="checkpoint", page=1, per_page=5)
    gh = {"configured": bool(cfg.github_token),
          "note": "live run list requires GitHub API; local tool outcomes in events"}
    cf = {"configured": bool(cfg.cf_api_token),
          "note": "live health requires Cloudflare API; local deploy artifacts in events"}
    return scrub_dict({
        "queue": queue,
        "failed_jobs": [dict(r) for r in failed_jobs],
        "total_failures": store.total_failures(),
        "consecutive_failures": store.consecutive_failures(25),
        "llm_failures_recent": llm_fail,
        "deployments": deploys,
        "restarts": checkpoints,
        "github": gh,
        "cloudflare": cf,
    })


def control(cfg, store, sup, action, actor="dashboard"):
    action = str(action or "").lower()
    if action == "pause":
        sup.pause(source=actor)
        store.audit_log(actor, "pause", True, "dashboard pause; deadline unchanged")
        return {"ok": True, "action": "pause", "status": sup.experiment_status()}
    if action == "resume":
        sup.resume(source=actor)
        store.audit_log(actor, "resume", True, "dashboard resume; deadline unchanged")
        return {"ok": True, "action": "resume", "status": sup.experiment_status()}
    if action == "shutdown":
        sup.stop(source=actor)
        store.audit_log(actor, "shutdown", True, "dashboard safe shutdown")
        return {"ok": True, "action": "shutdown", "status": sup.experiment_status()}
    if action == "diagnostic":
        store.enqueue("diagnostic", {"at": time.time(), "by": actor})
        store.audit_log(actor, "diagnostic", True, "diagnostic job enqueued (bounded, read-only)")
        cycle = int(store.get_state("cycle", 0) or 0)
        st = sup.status(cycle)
        return {"ok": True, "action": "diagnostic",
                "result": {"status": st["experiment_status"],
                           "remaining_hours": st["remaining_hours"],
                           "verified_gross": st["verified_gross"],
                           "consecutive_failures": store.consecutive_failures(25)}}
    raise BadRequest("unknown action (pause|resume|shutdown|diagnostic)")


# --- minimal stdlib HTTP server (local use + tests) ---
def make_app(cfg, store, sup):
    from http.server import BaseHTTPRequestHandler

    limiter = RateLimiter()

    def send(h, code, obj):
        body = json.dumps(scrub_dict(obj)).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Access-Control-Allow-Origin", "*")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def client_ip(h):
        return (h.client_address[0] if h.client_address else "local")[:64]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # never log request lines (may contain paths, never tokens)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/health":
                    return send(self, 200, {"ok": True})
                if not authenticate(dict(self.headers), cfg):
                    return send(self, 401, {"error": "unauthorized"})
                limiter.check(client_ip(self))
                q = dict(urllib.parse.parse_qsl(parsed.query))
                if parsed.path == "/api/overview":
                    return send(self, 200, overview(cfg, store, sup))
                if parsed.path == "/api/events":
                    rows, total = store.list_events(
                        severity=q.get("severity") or None,
                        category=q.get("category") or None,
                        goal_id=int(q["goal_id"]) if q.get("goal_id") else None,
                        since=float(q["since"]) if q.get("since") else None,
                        search=q.get("search") or None,
                        page=int(q.get("page", 1)), per_page=int(q.get("per_page", 50)))
                    return send(self, 200, {"events": rows, "total": total,
                                            "page": int(q.get("page", 1))})
                if parsed.path == "/api/goals":
                    return send(self, 200, {"goals": goals_view(store)})
                if parsed.path == "/api/revenue":
                    return send(self, 200, revenue_summary(cfg, store))
                if parsed.path == "/api/infra":
                    return send(self, 200, infra_view(cfg, store))
                if parsed.path == "/api/approvals":
                    return send(self, 200, {"pending": store.pending_approvals()})
                if parsed.path == "/api/audit":
                    rows, total = store.list_audit(page=int(q.get("page", 1)),
                                                   per_page=int(q.get("per_page", 50)))
                    return send(self, 200, {"audit": rows, "total": total})
                return send(self, 404, {"error": "not found"})
            except RateLimited as e:
                return send(self, 429, {"error": str(e)})
            except (BadRequest, ValueError):
                return send(self, 400, {"error": "bad request"})
            except BrokenPipeError:
                pass
            except Exception:
                return send(self, 500, {"error": "internal error"})

        def do_POST(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if not authenticate(dict(self.headers), cfg):
                    return send(self, 401, {"error": "unauthorized"})
                limiter.check(client_ip(self), control=True)
                length = min(int(self.headers.get("Content-Length", 0) or 0), MAX_BODY)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode() or "{}")
                except ValueError:
                    raise BadRequest("invalid JSON")
                if not isinstance(body, dict):
                    raise BadRequest("invalid JSON")
                parts = parsed.path.strip("/").split("/")
                if parts[:2] == ["api", "controls"] and len(parts) == 3:
                    actor = str(body.get("actor", "dashboard"))[:40] or "dashboard"
                    return send(self, 200, control(cfg, store, sup, parts[2], actor))
                if parts[:2] == ["api", "approvals"] and len(parts) == 4 and parts[3] == "decide":
                    try:
                        aid = int(parts[2])
                    except ValueError:
                        raise BadRequest("invalid approval id")
                    approve = bool(body.get("approve"))
                    actor = str(body.get("actor", "dashboard"))[:40] or "dashboard"
                    res = store.decide_approval(aid, approve, actor)
                    if res is None:
                        return send(self, 409, {"error": "not pending"})
                    store.audit_log(actor, f"approval:{aid}:{res}", True, "")
                    return send(self, 200, {"ok": True, "status": res})
                return send(self, 404, {"error": "not found"})
            except RateLimited as e:
                return send(self, 429, {"error": str(e)})
            except BadRequest as e:
                return send(self, 400, {"error": str(e)})
            except BrokenPipeError:
                pass
            except Exception:
                return send(self, 500, {"error": "internal error"})

    return Handler


def serve(cfg, store, sup, port=None):
    from http.server import ThreadingHTTPServer
    handler = make_app(cfg, store, sup)
    srv = ThreadingHTTPServer(("127.0.0.1", int(port or cfg.dashboard_port)), handler)
    return srv
