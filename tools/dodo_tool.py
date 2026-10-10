"""Dodo Payments tool. ONLY source of revenue truth.
- verify_payments: queries Dodo API, mirrors genuine records into memory.payments,
  returns verified gross (excludes test/refunded/disputed/self-purchase).
- create_product / create_checkout: test-mode only unless supervisor allows live.
Self-purchases and fabricated transactions are excluded and flagged.
"""
import json
import urllib.request

from .base import ToolResult

SELF_BLOCKLIST = ("agent@", "test@", "localhost", "example.com")


class DodoTool:
    name = "dodo"

    def __init__(self, config, store, supervisor):
        self.cfg = config
        self.store = store
        self.sup = supervisor

    def run(self, op, args):
        if op == "verify_payments":
            return self.verify()
        if op == "create_product":
            return self._create_product(args)
        if op == "create_checkout":
            return self._create_checkout(args)
        return ToolResult(ok=False, output=f"unknown op {op}")

    def _is_self(self, email):
        e = (email or "").lower()
        return any(s in e for s in SELF_BLOCKLIST)

    def verify(self):
        if not self.cfg.dodo_api_key:
            gross = self.store.verified_gross()
            return ToolResult(ok=True, output=f"[dry-run] verified_gross=${gross:.2f} (no DODO_API_KEY; db mirror only)",
                              data={"verified_gross": gross, "dry_run": True})
        try:
            req = urllib.request.Request(
                self.cfg.dodo_base_url.rstrip("/") + "/payments?limit=50",
                headers={"Authorization": f"Bearer {self.cfg.dodo_api_key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode())
            items = payload.get("data") or payload.get("items") or payload.get("payments") or []
            n = 0
            for p in items:
                pid = str(p.get("id") or p.get("payment_id") or "")
                if not pid:
                    continue
                status = p.get("status", "unknown")
                amount = float(p.get("amount", 0) or 0) / 100.0 if p.get("amount", 0) > 100 else float(p.get("amount", 0) or 0)
                currency = p.get("currency", self.cfg.currency)
                is_test = 1 if (p.get("test") or p.get("mode") == "test" or self.cfg.dodo_mode == "test") else 0
                is_refunded = 1 if status in ("refunded", "disputed", "refunding") else 0
                is_self = 1 if self._is_self(p.get("customer_email", "") or p.get("email", "")) else 0
                self.store.upsert_payment(pid, amount, currency, status, is_test, is_refunded, is_self, p)
                n += 1
            gross = self.store.verified_gross()
            return ToolResult(ok=True, output=f"verified {n} records. verified_gross=${gross:.2f}",
                              data={"verified_gross": gross, "records": n})
        except Exception as e:
            return ToolResult(ok=False, output=f"dodo verify failed: {e}")

    def _create_product(self, args):
        if not self.sup.allow_live_money():
            # test-mode stub: record intent, no real charge
            return ToolResult(ok=True, output="[sandbox] product stub created (no live charge). Set SANDBOX_MODE=0 + APPROVE_LIVE* to go live.",
                              data={"sandbox": True, "name": args.get("name", "product")})
        if not self.cfg.dodo_api_key:
            return ToolResult(ok=False, output="live mode requires DODO_API_KEY")
        try:
            body = json.dumps({"name": args.get("name", "product"),
                               "price": args.get("price_cents", 900),
                               "currency": self.cfg.currency}).encode()
            req = urllib.request.Request(self.cfg.dodo_base_url.rstrip("/") + "/products",
                                         data=body, headers={"Authorization": f"Bearer {self.cfg.dodo_api_key}",
                                                             "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            return ToolResult(ok=True, output=f"live product created: {data}", data=data)
        except Exception as e:
            return ToolResult(ok=False, output=f"create_product failed: {e}")

    def _create_checkout(self, args):
        # checkout links are safe (customer pays); still gate live behind approval
        if not self.sup.allow_live_money():
            return ToolResult(ok=True, output="[sandbox] checkout stub (test mode).",
                              data={"sandbox": True})
        return self._create_product(args)
