"""Authenticated Telegram commands. All state changes route via Supervisor.

Allowed: /status /why /pause /resume /stop /report.
- Sender must be in the allowlist (TELEGRAM_ALLOWED_IDS, defaults to TELEGRAMCHATID).
- Unauthorized senders get no reply and no state change (attempt is logged).
- Commands cannot change caps, extend the deadline, bypass approvals, or grant
  permissions: the only mutations are supervisor.pause/resume/stop.
"""
import time

COMMANDS = ("/status", "/why", "/pause", "/resume", "/stop", "/report")

USAGE = ("commands: /status /why /pause /resume /stop /report\n"
         "pause/resume toggle the supervisor kill switch; stop ends the experiment.\n"
         "limits, deadline and approvals cannot be changed from Telegram.")


class CommandRouter:
    def __init__(self, config, store, supervisor, sender, notifier=None):
        self.cfg = config
        self.store = store
        self.sup = supervisor
        self.sender = sender
        self.notifier = notifier

    # --- auth ---
    def is_authorized(self, chat_id, user_id):
        allowed = set(str(x) for x in (self.cfg.telegram_allowed_ids or ()))
        return str(chat_id) in allowed or str(user_id) in allowed

    # --- polling ---
    def poll_and_handle(self):
        """Fetch updates once, handle authorized commands. Returns handled count."""
        if not self.cfg.telegram_poll_enabled:
            return 0
        if not self.cfg.telegram_bot_token:
            return 0
        offset = self.store.get_state("telegram_update_offset", None)
        res = self.sender.get_updates(offset, timeout=0)
        if not res.ok:
            return 0
        handled = 0
        max_id = offset
        for upd in (res.data or {}).get("updates", []):
            uid = upd.get("update_id")
            if uid is not None and (max_id is None or uid >= max_id):
                max_id = uid + 1
            handled += self._handle_update(upd)
        if max_id is not None:
            self.store.set_state("telegram_update_offset", max_id)
        return handled

    def _handle_update(self, upd):
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = str(msg.get("text", "") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        if not text.startswith("/"):
            return 0
        cmd = text.split()[0].split("@")[0].lower()
        if cmd not in COMMANDS:
            if self.is_authorized(chat, user):
                self._reply(chat, USAGE)
                self._audit(cmd, chat, user, False, "unknown command")
            else:
                self._audit(cmd, chat, user, False, "rejected (unauthorized)")
            return 1
        if not self.is_authorized(chat, user):
            self._audit(cmd, chat, user, False, "rejected (unauthorized)")
            return 1
        try:
            reply = self.execute(cmd, source=f"telegram:{user}")
            self._reply(chat, reply)
            self._audit(cmd, chat, user, True, reply[:200])
        except Exception as e:
            self._reply(chat, f"{cmd} failed: {str(e)[:200]}")
            self._audit(cmd, chat, user, False, str(e)[:200])
        return 1

    def _reply(self, chat_id, text):
        try:
            self.sender.send_message(text[:3500], chat_id=chat_id)
        except Exception:
            pass

    def _audit(self, cmd, chat_id, user_id, ok, note):
        try:
            cycle = int(self.store.get_state("cycle", 0) or 0)
            # never log chat/token contents beyond ids already known to operator
            self.store.log_action(cycle, "telegram", f"command:{cmd}",
                                  {"chat_id": str(chat_id), "user_id": str(user_id)},
                                  ok, note[:500])
        except Exception:
            pass

    # --- execution (state changes ONLY via supervisor) ---
    def execute(self, cmd, source="telegram"):
        if cmd == "/status":
            return self._status()
        if cmd == "/why":
            return self._why()
        if cmd == "/report":
            return self._report()
        if cmd == "/pause":
            self.sup.pause(source)
            if self.notifier:
                self.notifier.notify("paused", {"tool": "supervisor",
                                                "error": f"paused by {source}",
                                                "next": "/resume to continue"})
            return "paused. Deadline unchanged. /resume to continue."
        if cmd == "/resume":
            self.sup.resume(source)
            if self.notifier:
                self.notifier.notify("resumed", {"tool": "supervisor",
                                                 "error": f"resumed by {source}",
                                                 "next": "work continuing"})
            return "resumed. Deadline unchanged."
        if cmd == "/stop":
            self.sup.stop(source)
            if self.notifier:
                self.notifier.notify("shutdown", {"tool": "supervisor",
                                                  "error": f"stopped by {source}",
                                                  "next": "experiment ended; see /report"})
            return "stopped. Experiment terminal; limits/deadline untouched."
        return USAGE

    # --- read-only views (no secrets) ---
    def _status(self):
        cycle = int(self.store.get_state("cycle", 0) or 0)
        st = self.sup.status(cycle)
        goal = (self.store.active_goal() or {}).get("description", "-")
        return (f"status: {st['experiment_status']} cycle={cycle} "
                f"gross=${st['verified_gross']:.2f}/${st['target']:.2f} "
                f"remaining={st['remaining_hours']:.1f}h "
                f"fail_streak={self.store.consecutive_failures(25)} "
                f"goal={str(goal)[:120]}")

    def _why(self):
        recent = self.store.recent_actions(5)
        fails = self.store.consecutive_failures(25)
        last_err = "-"
        last_tool = "-"
        for a in recent:
            if not a.get("ok"):
                last_err = str(a.get("output_text", ""))[:200]
                last_tool = f"{a.get('tool')}.{a.get('op')}"
                break
        blockers = [k for k in ("failures",) ]
        _ = blockers
        goal = (self.store.active_goal() or {}).get("description", "-")
        nxt = "fix error and continue" if fails else "no active blocker; continuing plan"
        return (f"why: fail_streak={fails} last_tool={last_tool} "
                f"last_error={last_err} goal={str(goal)[:120]} next={nxt}")

    def _report(self):
        cycle = int(self.store.get_state("cycle", 0) or 0)
        st = self.sup.status(cycle)
        goals = self.store.list_goals()
        counts = {}
        for g in goals:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        return (f"report: gross=${st['verified_gross']:.2f}/${st['target']:.2f} "
                f"status={st['experiment_status']} cycle={cycle} "
                f"remaining={st['remaining_hours']:.1f}h "
                f"usage={st['usage']} goals={counts}")
