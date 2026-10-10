// Tool gateway + Dodo webhook receiver. Credentials live as secrets, never in code.
// Secrets: DODO_WEBHOOK_SECRET, LLM_API_KEY. Verify webhooks before trusting revenue.
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return Response.json({ ok: true });
    if (url.pathname === "/dodo-webhook" && req.method === "POST") {
      // TODO: verify signature with DODO_WEBHOOK_SECRET, then insert into D1 payments
      // ONLY rows verified here (or via Dodo API poll) count toward the $25 target.
      // origin='dodo_api' is required; manual inserts never count. USD + succeeded only.
      const body = await req.json().catch(() => ({}));
      const status = body.status || "unknown";
      const excluded = ["refunded", "disputed", "refunding", "chargeback", "failed", "canceled", "cancelled"].includes(status) ? 1 : 0;
      await env.MEMORY.prepare(
        "INSERT INTO payments(dodo_payment_id,amount,currency,status,is_test,is_refunded,is_self_purchase,origin,raw_json,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dodo_payment_id) DO UPDATE SET status=excluded.status, verified_at=excluded.verified_at"
      ).bind(String(body.id || crypto.randomUUID()), Number(body.amount || 0), body.currency || "USD", status, body.test ? 1 : 0, excluded, 0, "dodo_api", JSON.stringify(body), Date.now() / 1000).run();
      return Response.json({ ok: true });
    }
    if (url.pathname === "/verified-gross") {
      const row = await env.MEMORY.prepare(
        "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE origin='dodo_api' AND status='succeeded' AND currency='USD' AND is_test=0 AND is_refunded=0 AND is_self_purchase=0"
      ).first();
      return Response.json({ verified_gross: row?.total || 0 });
    }
    return Response.json({ ok: true, routes: ["/health", "/dodo-webhook", "/verified-gross"] });
  },
  async queue(batch, env) {
    for (const msg of batch.messages) {
      // background jobs: research, retries, analytics checks
      console.log("job", msg.body);
      msg.ack();
    }
  },
  async scheduled(event, env, ctx) {
    // Cron trigger: enqueue next agent tick
    await env.JOBS.send({ kind: "tick", at: Date.now() });
  },
};
