"""Telegram alert manager: deduped, cooldown-guarded, crash-safe outbox.

- Queue in SQLite (notifications table); delivery counts ONLY on API {"ok": true}.
- Failures retry with backoff; loop work never blocks on notification errors.
- Messages carry diagnostics, never secrets or customer data.
"""
import hashlib
import json
import re
import time

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TG_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# Events that fire at most once per experiment (flag-guarded).
ONE_SHOT = frozenset({"experiment_started", "target_reached", "deadline_reached"})


class AlertManager:
    def __init__(self, config, store, supervisor, sender):
        self.cfg = config
        self.store = store
        self.sup = supervisor
        self.sender = sender

    # --- state ---
    def enabled(self):
        try:
            return bool(self.sender.configured())
        except Exception:
            return False

    def _secrets(self):
        c = self.cfg
        return [s for s in [
            c.telegram_bot_token, c.llm_api_key, c.github_token,
            c.cf_api_token, c.dodo_api_key, c.brave_api_key,
        ] if s]

    def scrub(self, text):
        out = str(text or "")
        for s in self._secrets():
            if s and s in out:
                out = out.replace(s, "***")
        out = TG_TOKEN_RE.sub("***", out)
        out = EMAIL_RE.sub("[redacted-email]", out)
        return out[:1200]

    def _log(self, line):
        try:
            with open(f"{self.cfg.data_dir}/notifications.log", "a") as f:
                f.write(f"{time.time():.0f} {line[:500]}\n")
        except OSError:
            pass

    # --- message building ---
    def format_message(self, event_type, ctx):
        ctx = ctx or {}
        goal = self.scrub(str(ctx.get("goal", "-"))[:160])
        tool = self.scrub(str(ctx.get("tool", "-"))[:80])
        err = self.scrub(str(ctx.get("error", "-"))[:300])
        gross = ctx.get("gross", self.store.verified_gross())
        lines = [
            f"[AUTO-agent] {event_type}",
            f"goal: {goal}",
            f"tool: {tool}",
            f"error: {err}",
            f"retries: {ctx.get('retries', 0)}",
            f"cycle: {ctx.get('cycle', self.store.get_state('cycle', 0) or 0)}",
            f"gross: ${float(gross):.2f}/${self.cfg.target_revenue_usd:.2f}",
            f"remaining: {self.sup.remaining_hours():.1f}h",
            f"status: {self.sup.experiment_status()}",
            f"next: {self.scrub(str(ctx.get('next', '-'))[:160])}",
        ]
        return "\n".join(lines)

    def _dedupe_key(self, event_type, ctx):
        if event_type in ONE_SHOT:
            return f"{event_type}:once"
        stable = json.dumps({
            "e": event_type,
            "g": str(ctx.get("goal_id", ctx.get("goal", "")))[:80],
            "t": str(ctx.get("tool", ""))[:40],
            "s": str(ctx.get("sig", ctx.get("error", "")))[:120],
        }, sort_keys=True)
        return event_type + ":" + hashlib.sha256(stable.encode()).hexdigest()[:16]

    # --- public API (never raises) ---
    def notify(self, event_type, ctx=None):
        try:
            return self._notify(event_type, ctx or {})
        except Exception as e:  # notifier must never break the loop
            self._log(f"notify-error {event_type} {e}")
            return {"sent": False, "reason": "internal-error"}

    def _notify(self, event_type, ctx):
        if event_type in ONE_SHOT:
            flag = f"telegram_notified_{event_type}"
            if self.store.get_state(flag, False):
                return {"sent": False, "reason": "already-notified"}
        key = self._dedupe_key(event_type, ctx)
        now = time.time()
        if event_type not in ONE_SHOT:
            last = self.store.get_state(f"telegram_last:{key}", 0) or 0
            if now - float(last) < float(self.cfg.telegram_cooldown_s):
                return {"sent": False, "reason": "cooldown"}
        message = self.format_message(event_type, ctx)
        nid, is_new = self.store.queue_notification(key, event_type, message)
        if not is_new and event_type in ONE_SHOT:
            return {"sent": False, "reason": "already-queued"}
        if not self.enabled():
            self._log(f"queued-no-credentials {event_type} id={nid}")
            return {"sent": False, "reason": "not-configured", "queued": nid}
        sent = self.flush(limit=5)
        if event_type in ONE_SHOT and sent:
            self.store.set_state(f"telegram_notified_{event_type}", True)
        if sent:
            self.store.set_state(f"telegram_last:{key}", now)
        return {"sent": bool(sent), "queued": nid}

    def flush(self, limit=10):
        """Attempt due outbox sends. Returns count delivered this call."""
        delivered = 0
        now = time.time()
        for row in self.store.due_notifications(now, limit):
            try:
                res = self.sender.send_message(row["message"])
            except Exception:
                res = None
            ok = bool(res and getattr(res, "ok", False)
                      and (res.data or {}).get("delivered"))
            if ok:
                self.store.mark_notification_sent(row["id"])
                self.store.set_state(f"telegram_last:{row['dedupe_key']}", time.time())
                delivered += 1
                self._cycle_log("telegram", "send", True, f"delivered {row['event_type']}")
            else:
                attempts = int(row["attempts"] or 0) + 1
                backoff = min(3600, 30 * (2 ** min(attempts, 6)))
                self.store.mark_notification_failed(row["id"], attempts, time.time() + backoff)
                self._log(f"send-failed {row['event_type']} attempt={attempts} next_in={backoff}s")
                self._cycle_log("telegram", "send", False, f"failed {row['event_type']}")
        return delivered

    def _cycle_log(self, tool, op, ok, note):
        try:
            cycle = int(self.store.get_state("cycle", 0) or 0)
            self.store.log_action(cycle, tool, op, {}, ok, note[:500])
        except Exception:
            pass

    # --- lifecycle events ---
    def notify_started(self):
        return self.notify("experiment_started", {
            "goal": f"earn ${self.cfg.target_revenue_usd:.2f} genuine revenue",
            "tool": "-",
            "error": "-",
            "next": "monitor with /status; stop anytime with /stop",
        })

    def notify_shutdown(self, reason):
        return self.notify("shutdown" if "kill" in str(reason) or "stop" in str(reason) else "paused"
                           if "pause" in str(reason) else "shutdown", {
            "goal": (self.store.active_goal() or {}).get("description", "-"),
            "tool": "supervisor",
            "error": str(reason)[:200],
            "next": "use /resume to continue (deadline unchanged) or /report for summary",
        })

    def notify_target(self):
        return self.notify("target_reached", {
            "goal": (self.store.active_goal() or {}).get("description", "-"),
            "tool": "dodo",
            "error": "-",
            "next": "no new revenue tasks; see /report",
        })

    def notify_deadline(self):
        return self.notify("deadline_reached", {
            "goal": (self.store.active_goal() or {}).get("description", "-"),
            "tool": "supervisor",
            "error": f"{self.cfg.max_runtime_hours}h window elapsed",
            "next": "no new revenue tasks; see /report",
        })

    def notify_crash(self, error):
        return self.notify("crash", {
            "goal": (self.store.active_goal() or {}).get("description", "-"),
            "tool": "-",
            "error": str(error)[:200],
            "next": "auto-recovery in progress; state preserved",
        })

    def notify_recovered(self):
        return self.notify("recovered", {
            "goal": (self.store.active_goal() or {}).get("description", "-"),
            "tool": "-",
            "error": "-",
            "next": "resuming from persisted cycle; deadline unchanged",
        })

    # --- blockers / failures ---
    def set_blocker(self, key, ctx):
        flag = f"telegram_blocker:{key}"
        if self.store.get_state(flag, None):
            return {"sent": False, "reason": "blocker-already-active"}
        self.store.set_state(flag, {"since": time.time(), **{k: str(v)[:200] for k, v in (ctx or {}).items()}})
        return self.notify("blocker_unresolved", {"sig": key, **(ctx or {})})

    def clear_blocker(self, key, ctx=None):
        flag = f"telegram_blocker:{key}"
        if not self.store.get_state(flag, None):
            return {"sent": False, "reason": "no-blocker"}
        self.store.set_state(flag, None)
        return self.notify("blocker_resolved", {"sig": key, **(ctx or {}),
                                                "next": "normal work resumed"})

    def check_failures(self, tool, error, cycle):
        fails = self.store.consecutive_failures(25)
        if fails >= 5:
            return self.set_blocker(f"failures:{tool}", {
                "goal": (self.store.active_goal() or {}).get("description", "-"),
                "tool": tool, "error": str(error)[:200], "retries": fails,
                "next": "inspect with /why; fix or /stop",
            })
        if fails >= 3:
            return self.notify("repeated_failures", {
                "goal": (self.store.active_goal() or {}).get("description", "-"),
                "tool": tool, "error": str(error)[:200], "retries": fails,
                "cycle": cycle, "next": "watching; will escalate if unresolved",
            })
        return {"sent": False, "reason": "below-threshold"}

    def check_missing_credential(self, tool, op, output):
        markers = {
            "github_allowlist": "not in AGENT_ALLOWED_REPOS",
            "dodo_key": "live mode requires DODO_API_KEY",
            "cf_token": "CF_API_TOKEN",
        }
        for key, marker in markers.items():
            if marker in str(output):
                flag = f"telegram_cred_warned:{key}"
                if self.store.get_state(flag, False):
                    return {"sent": False, "reason": "already-warned"}
                self.store.set_state(flag, True)
                advice = {
                    "github_allowlist": "set AGENT_ALLOWED_REPOS to a sandbox repo",
                    "dodo_key": "set DODO_API_KEY (test mode) for verification",
                    "cf_token": "set CF_API_TOKEN + CF_ACCOUNT_ID for deploys",
                }[key]
                return self.notify("missing_credential", {
                    "goal": (self.store.active_goal() or {}).get("description", "-"),
                    "tool": tool, "error": f"{op}: needs {key}", "sig": key,
                    "next": advice,
                })
        return {"sent": False, "reason": "no-marker"}

    def check_spend(self, usage):
        caps = {
            "llm_spend_usd": self.cfg.max_llm_spend_usd,
            "infra_spend_usd": self.cfg.max_infra_spend_usd,
            "spend_usd": self.cfg.max_spend_usd,
            "llm_calls": self.cfg.max_llm_calls,
            "tool_calls": self.cfg.max_tool_calls,
        }
        fired = False
        for metric, cap in caps.items():
            if not cap:
                continue
            pct = (float(usage.get(metric, 0) or 0) / float(cap)) * 100.0
            for threshold in self.cfg.telegram_spend_warn_pct:
                if pct >= threshold:
                    flag = f"telegram_spend_warned:{metric}:{threshold}"
                    if not self.store.get_state(flag, False):
                        self.store.set_state(flag, True)
                        self.notify("spend_threshold", {
                            "goal": (self.store.active_goal() or {}).get("description", "-"),
                            "tool": "supervisor",
                            "error": f"{metric} at {pct:.0f}% of cap (${usage.get(metric,0)}/${cap})",
                            "sig": f"{metric}:{threshold}",
                            "next": "reduce cadence or raise cap before hard stop",
                        })
                        fired = True
        return fired
