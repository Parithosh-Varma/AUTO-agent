// AUTO-agent observability backend (Cloudflare Worker + D1).
// Mirrors dashboard/api.py routes. Auth: every /api/* route except /api/health
// requires Authorization: Bearer <DASHBOARD_TOKEN> (Worker secret, fail-closed).
// Revenue is computed ONLY from origin='dodo_api' provider rows; there is NO
// endpoint that writes payments except the signature-verified Dodo webhook.
// Secrets: DASHBOARD_TOKEN, DODO_WEBHOOK_SECRET. Never logged, never returned.

const RATE_MAX = 120, RATE_CONTROL_MAX = 10;
const hits = new Map();

function authed(req, env) {
  const token = (env.DASHBOARD_TOKEN || "").trim();
  if (!token) return false;
  const h = req.headers.get("authorization") || "";
  if (!h.toLowerCase().startsWith("bearer ")) return false;
  const got = h.slice(7).trim();
  if (got.length !== token.length) return false;
  let diff = 0;
  for (let i = 0; i < token.length; i++) diff |= got.charCodeAt(i) ^ token.charCodeAt(i);
  return diff === 0;
}

function checkRate(ip, control) {
  const w = Math.floor(Date.now() / 60000);
  const k = ip + (control ? ":c:" : ":r:") + w;
  const n = (hits.get(k) || 0) + 1;
  hits.set(k, n);
  if (hits.size > 1024) for (const key of hits.keys()) { hits.delete(key); break; }
  if (n > (control ? RATE_CONTROL_MAX : RATE_MAX)) throw resp(429, { error: "rate limit exceeded" });
}

function resp(code, obj) {
  return new Response(JSON.stringify(obj), { status: code, headers: {
    "Content-Type": "application/json", "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
  }});
}
const ok = (obj) => resp(200, obj);

function scrubStr(s) {
  s = String(s == null ? "" : s);
  s = s.replace(/\d{5,}:[A-Za-z0-9_-]{20,}/g, "***");
  s = s.replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, "[redacted-email]");
  return s.slice(0, 2000);
}
function scrubRow(r) {
  const o = {};
  for (const [k, v] of Object.entries(r || {})) {
    if (/token|secret|password|api_key|apikey|auth/i.test(k)) o[k] = "***";
    else if (k === "raw_json" || k === "context_json" && false) o[k] = "***";
    else o[k] = typeof v === "string" ? scrubStr(v) : v;
  }
  delete o.raw_json; // customer data never leaves the API
  return o;
}

const REV_PRED = "origin='dodo_api' AND status='succeeded' AND currency='USD' AND is_test=0 AND is_refunded=0 AND is_self_purchase=0";

async function revenue(env) {
  const gross = await env.MEMORY.prepare(
    `SELECT COALESCE(SUM(amount),0) AS t FROM payments WHERE ${REV_PRED}`).first();
  const ref = await env.MEMORY.prepare(
    "SELECT COALESCE(SUM(amount),0) AS t FROM payments WHERE origin='dodo_api' AND is_refunded=1").first();
  const tx = await env.MEMORY.prepare(
    "SELECT dodo_payment_id, amount, currency, status, is_test, is_refunded, is_self_purchase, origin, verified_at FROM payments WHERE origin='dodo_api' ORDER BY verified_at DESC LIMIT 200").all();
  const txns = (tx.results || []).map(r => ({
    id: r.dodo_payment_id, amount: r.amount, currency: r.currency, status: r.status,
    genuine: r.origin === "dodo_api" && r.status === "succeeded" && r.currency === "USD" && !r.is_test && !r.is_refunded && !r.is_self_purchase,
    verified_at: r.verified_at,
  }));
  return { gross_usd: gross?.t || 0, refunded_usd: ref?.t || 0, fees_usd: 0,
    fees_basis: "unavailable", profit_basis: "estimate", basis: "provider", transactions: txns };
}

async function overview(env) {
  const st = await env.MEMORY.prepare("SELECT key, value FROM state").all();
  const kv = {};
  for (const r of st.results || []) { try { kv[r.key] = JSON.parse(r.value); } catch { kv[r.key] = null; } }
  const usage = kv.usage || {};
  const rev = await revenue(env);
  const goal = await env.MEMORY.prepare("SELECT * FROM goals WHERE status='active' ORDER BY id DESC LIMIT 1").first();
  const dec = await env.MEMORY.prepare("SELECT summary FROM events WHERE category='decision' ORDER BY id DESC LIMIT 1").first();
  const okA = await env.MEMORY.prepare("SELECT cycle, tool, op, created_at FROM actions WHERE ok=1 ORDER BY id DESC LIMIT 1").first();
  const errA = await env.MEMORY.prepare("SELECT cycle, tool, op, output_text, created_at FROM actions WHERE ok=0 ORDER BY id DESC LIMIT 1").first();
  const lastEv = await env.MEMORY.prepare("SELECT ts FROM events ORDER BY id DESC LIMIT 1").first();
  const started = Number(kv.run_started_at || Date.now() / 1000);
  const caps = kv.caps || {};
  const maxH = Number(caps.max_runtime_hours || 168);
  const elapsed = (Date.now() / 1000 - started) / 3600;
  return {
    status: kv.experiment_status || "active",
    started_at: started, deadline_at: started + maxH * 3600,
    elapsed_hours: Math.round(elapsed * 1000) / 1000,
    remaining_hours: Math.max(0, Math.round((maxH - elapsed) * 1000) / 1000),
    target_usd: Number(caps.target_revenue_usd || 25),
    verified_gross_usd: rev.gross_usd, revenue: rev, usage: usage, caps: caps,
    current_goal: goal ? scrubRow(goal) : null,
    next_action: dec ? scrubStr(dec.summary).slice(0, 200) : "-",
    last_ok: okA ? { cycle: okA.cycle, tool: okA.tool + "." + okA.op, at: okA.created_at } : null,
    last_error: errA ? { cycle: errA.cycle, tool: errA.tool + "." + errA.op, summary: scrubStr(errA.output_text).slice(0, 200), at: errA.created_at } : null,
    last_event_ts: lastEv?.ts || null, stale: lastEv ? (Date.now() / 1000 - lastEv.ts > 300) : true,
    sandbox: true, live_allowed: false,
  };
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;
    const ip = req.headers.get("cf-connecting-ip") || "unknown";
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: {
      "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, content-type",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS" }});
    if (path === "/health" || path === "/api/health") return ok({ ok: true });
    try {
      // ---- Dodo webhook: signature-verified only ----
      if (path === "/dodo-webhook" && req.method === "POST") {
        const secret = (env.DODO_WEBHOOK_SECRET || "").trim();
        if (!secret) return resp(403, { error: "webhook not configured" });
        const raw = await req.text();
        const sig = req.headers.get("x-dodo-signature") || "";
        const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
          { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
        const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(raw));
        const hex = [...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, "0")).join("");
        if (sig !== hex && sig !== "sha256=" + hex) {
          await env.MEMORY.prepare("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)")
            .bind("webhook", "dodo-webhook", 0, "bad signature", Date.now() / 1000).run();
          return resp(403, { error: "bad signature" });
        }
        let body = {};
        try { body = JSON.parse(raw); } catch { return resp(400, { error: "bad JSON" }); }
        const status = body.status || "unknown";
        const excluded = ["refunded", "disputed", "refunding", "chargeback", "failed", "canceled", "cancelled"].includes(status) ? 1 : 0;
        const isTest = body.test || body.mode === "test" ? 1 : 0;
        await env.MEMORY.prepare(
          "INSERT INTO payments(dodo_payment_id,amount,currency,status,is_test,is_refunded,is_self_purchase,origin,raw_json,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(dodo_payment_id) DO UPDATE SET status=excluded.status, verified_at=excluded.verified_at"
        ).bind(String(body.id || crypto.randomUUID()), Number(body.amount || 0) / 100, body.currency || "USD",
          status, isTest, excluded, 0, "dodo_api", "{}", Date.now() / 1000).run();
        return ok({ ok: true });
      }
      // ---- everything below requires the operator token ----
      if (!path.startsWith("/api/")) return ok({ ok: true, routes: ["GET /api/health", "GET /api/overview", "GET /api/events", "GET /api/goals", "GET /api/revenue", "GET /api/infra", "GET /api/approvals", "GET /api/audit", "POST /api/controls/:action", "POST /api/ingest"] });
      if (!authed(req, env)) return resp(401, { error: "unauthorized" });
      const control = req.method === "POST";
      checkRate(ip, control);
      const q = url.searchParams;

      if (req.method === "GET" && path === "/api/overview") return ok(await overview(env));
      if (req.method === "GET" && path === "/api/verified-gross") {
        const r = await revenue(env); return ok({ verified_gross: r.gross_usd });
      }
      if (req.method === "GET" && path === "/api/revenue") return ok(await revenue(env));
      if (req.method === "GET" && path === "/api/goals") {
        const goals = await env.MEMORY.prepare("SELECT * FROM goals ORDER BY score DESC LIMIT 200").all();
        const out = [];
        for (const g of goals.results || []) {
          const tasks = await env.MEMORY.prepare("SELECT * FROM tasks WHERE goal_id=? ORDER BY id DESC LIMIT 50").bind(g.id).all();
          out.push({ id: g.id, description: scrubStr(g.description), status: g.status, score: g.score,
            why_selected: { score: g.score, novelty: g.novelty, feasibility: g.feasibility, usefulness: g.usefulness, cost: g.cost },
            tasks: (tasks.results || []).map(t => scrubRow(t)), created_at: g.created_at, updated_at: g.updated_at });
        }
        return ok({ goals: out });
      }
      if (req.method === "GET" && path === "/api/events") {
        const conds = [], args = [];
        const sev = q.get("severity"), cat = q.get("category");
        if (sev && ["info", "warn", "error"].includes(sev)) { conds.push("severity=?"); args.push(sev); }
        if (cat) { conds.push("category=?"); args.push(String(cat).slice(0, 20)); }
        if (q.get("goal_id")) { conds.push("goal_id=?"); args.push(Number(q.get("goal_id"))); }
        if (q.get("since")) { conds.push("ts>=?"); args.push(Number(q.get("since"))); }
        if (q.get("search")) { conds.push("summary LIKE ?"); args.push("%" + String(q.get("search")).slice(0, 80) + "%"); }
        const where = conds.length ? " WHERE " + conds.join(" AND ") : "";
        const per = Math.max(1, Math.min(200, Number(q.get("per_page") || 50)));
        const page = Math.max(1, Number(q.get("page") || 1));
        const total = await env.MEMORY.prepare(`SELECT COUNT(*) AS c FROM events${where}`).bind(...args).first();
        const rows = await env.MEMORY.prepare(`SELECT * FROM events${where} ORDER BY id DESC LIMIT ? OFFSET ?`).bind(...args, per, (page - 1) * per).all();
        return ok({ events: (rows.results || []).map(scrubRow), total: total?.c || 0, page });
      }
      if (req.method === "GET" && path === "/api/infra") {
        const qq = await env.MEMORY.prepare("SELECT status, COUNT(*) AS c, COALESCE(SUM(attempts),0) AS r FROM queue GROUP BY status").all();
        const queue = {};
        for (const r of qq.results || []) queue[r.status] = { count: r.c, retries: r.r };
        const failed = await env.MEMORY.prepare("SELECT * FROM queue WHERE status='failed' ORDER BY id DESC LIMIT 20").all();
        const fails = await env.MEMORY.prepare("SELECT COUNT(*) AS c FROM actions WHERE ok=0").first();
        const deploys = await env.MEMORY.prepare("SELECT * FROM events WHERE category='deployment' ORDER BY id DESC LIMIT 10").all();
        const restarts = await env.MEMORY.prepare("SELECT * FROM events WHERE category='checkpoint' ORDER BY id DESC LIMIT 5").all();
        return ok({ queue, failed_jobs: (failed.results || []).map(scrubRow),
          total_failures: fails?.c || 0, deployments: (deploys.results || []).map(scrubRow),
          restarts: (restarts.results || []).map(scrubRow),
          github: { note: "live run list requires GitHub API; tool outcomes in events" },
          cloudflare: { note: "live health requires Cloudflare API; deploys in events" } });
      }
      if (req.method === "GET" && path === "/api/approvals") {
        const rows = await env.MEMORY.prepare("SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC").all();
        return ok({ pending: (rows.results || []).map(scrubRow) });
      }
      if (req.method === "GET" && path === "/api/audit") {
        const per = Math.max(1, Math.min(200, Number(q.get("per_page") || 50)));
        const page = Math.max(1, Number(q.get("page") || 1));
        const total = await env.MEMORY.prepare("SELECT COUNT(*) AS c FROM audit").first();
        const rows = await env.MEMORY.prepare("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?").bind(per, (page - 1) * per).all();
        return ok({ audit: (rows.results || []).map(scrubRow), total: total?.c || 0 });
      }
      if (req.method === "GET" && path === "/api/controls/pending") {
        const rows = await env.MEMORY.prepare("SELECT * FROM control_requests WHERE status='pending' ORDER BY id ASC LIMIT 10").all();
        return ok({ requests: rows.results || [] });
      }
      if (req.method === "POST") {
        const m = path.match(/^\/api\/controls\/requests\/(\d+)\/ack$/);
        if (m) {
          const body = await req.json().catch(() => ({}));
          await env.MEMORY.prepare("UPDATE control_requests SET status='applied' WHERE id=?").bind(Number(m[1])).run();
          await env.MEMORY.prepare("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)")
            .bind(String((body.actor || "runner")).slice(0, 40), "control:ack", 1, "request #" + m[1], Date.now() / 1000).run();
          return ok({ ok: true });
        }
      }
      if (req.method === "POST" && path.startsWith("/api/controls/")) {
        const action = path.split("/")[3];
        if (!["pause", "resume", "shutdown", "diagnostic"].includes(action)) return resp(400, { error: "unknown action" });
        const body = await req.json().catch(() => ({}));
        const actor = String(body.actor || "dashboard").slice(0, 40);
        const cur = await env.MEMORY.prepare("SELECT value FROM state WHERE key='control_request'").first();
        void cur;
        await env.MEMORY.prepare("INSERT INTO control_requests(action,actor,status,created_at) VALUES(?,?, 'pending',?)")
          .bind(action, actor, Date.now() / 1000).run();
        await env.MEMORY.prepare("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)")
          .bind(actor, "control:" + action, 1, "queued for runner; caps/deadline/revenue untouched", Date.now() / 1000).run();
        await env.MEMORY.prepare("INSERT INTO events(event_id,idempotency_key,ts,severity,category,goal_id,task_id,summary,details_json,cycle) VALUES(?,?,?,?,?,?,?,?,?,?)")
          .bind("ev_" + Date.now() + "_" + action, "control:" + action + ":" + Date.now(), Date.now() / 1000,
            "warn", "control", 0, 0, "control requested: " + action, JSON.stringify({ actor }), 0).run();
        return ok({ ok: true, action, queued: true,
          note: "applied by the runner on its next cycle; nothing here touches caps, deadline, or revenue" });
      }
      if (req.method === "POST" && path === "/api/approvals/decide") {
        const body = await req.json().catch(() => ({}));
        return resp(400, { error: "use /api/approvals/:id/decide" });
      }
      {
        const m = path.match(/^\/api\/approvals\/(\d+)\/decide$/);
        if (req.method === "POST" && m) {
          const body = await req.json().catch(() => ({}));
          const actor = String(body.actor || "dashboard").slice(0, 40);
          const row = await env.MEMORY.prepare("SELECT * FROM approvals WHERE id=?").bind(Number(m[1])).first();
          if (!row || row.status !== "pending") return resp(409, { error: "not pending" });
          const st = body.approve ? "approved" : "denied";
          await env.MEMORY.prepare("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?")
            .bind(st, actor, Date.now() / 1000, Number(m[1])).run();
          await env.MEMORY.prepare("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)")
            .bind(actor, "approval:" + m[1] + ":" + st, 1, "", Date.now() / 1000).run();
          return ok({ ok: true, status: st });
        }
      }
      if (req.method === "POST" && path === "/api/ingest") {
        const body = await req.json().catch(() => null);
        if (!body || typeof body !== "object") return resp(400, { error: "bad JSON" });
        let nEv = 0, nAu = 0;
        for (const e of (body.events || []).slice(0, 500)) {
          if (!e || typeof e !== "object") continue;
          const cat = String(e.category || "");
          if (!["decision", "tool", "deployment", "error", "retry", "goal", "checkpoint", "audit", "notification", "control"].includes(cat)) continue;
          const sev = ["info", "warn", "error"].includes(e.severity) ? e.severity : "info";
          try {
            await env.MEMORY.prepare("INSERT OR IGNORE INTO events(event_id,idempotency_key,ts,severity,category,goal_id,task_id,summary,details_json,cycle) VALUES(?,?,?,?,?,?,?,?,?,?)")
              .bind(String(e.event_id || ("ev_" + Date.now() + Math.random())).slice(0, 64),
                String(e.idempotency_key || ("k_" + Date.now() + Math.random())).slice(0, 64),
                Number(e.ts || Date.now() / 1000), sev, cat, Number(e.goal_id || 0), Number(e.task_id || 0),
                scrubStr(e.summary).slice(0, 500), JSON.stringify(e.details_json || e.details || {}).slice(0, 4000),
                Number(e.cycle || 0)).run();
            nEv++;
          } catch { /* duplicate or malformed: skip */ }
        }
        for (const a of (body.audit || []).slice(0, 200)) {
          if (!a || typeof a !== "object") continue;
          await env.MEMORY.prepare("INSERT INTO audit(actor,command,ok,note,created_at) VALUES(?,?,?,?,?)")
            .bind(String(a.actor || "").slice(0, 80), String(a.command || "").slice(0, 80),
              a.ok ? 1 : 0, scrubStr(a.note).slice(0, 1000), Number(a.created_at || Date.now() / 1000)).run();
          nAu++;
        }
        // NOTE: payments are intentionally NOT ingestible. Provider data enters
        // only via the verified Dodo webhook or server-side poll.
        return ok({ ok: true, events: nEv, audit: nAu });
      }
      return resp(404, { error: "not found" });
    } catch (e) {
      if (e instanceof Response) throw e;
      return resp(500, { error: "internal error" });
    }
  },
  async queue(batch, env) {
    for (const msg of batch.messages) { console.log("job", scrubStr(JSON.stringify(msg.body)).slice(0, 200)); msg.ack(); }
  },
  async scheduled(event, env, ctx) {
    await env.JOBS.send({ kind: "tick", at: Date.now() });
  },
};
function throw_(r) { throw r; }
