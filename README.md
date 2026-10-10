# AUTO-agent — Continuously Autonomous Revenue Agent
# Objective: $25 in genuine Dodo Payments revenue. Agent chooses its own subgoals.

Target: 25.00 USD gross (verified, genuine customer payments only)
Mode: SANDBOX by default. Live financial actions require explicit approval.

## Quick start

```bash
cp .env.example .env
# edit .env — paste tokens, or leave blank for dry-run simulation
python3 main.py --once
python3 main.py --cycles 20
# continuous (crash-recovering) run, default 24h window + hard caps:
python3 main.py --daemon
# stop at any time (independent kill switch, agent cannot disable):
touch data/STOP
```

## What it does

```
Observe → Think → Act → Evaluate → Repeat (until $25 verified or stopped)
```

1. **Generate goals** — proposes 3–5 candidate projects, ranks by novelty/feasibility/usefulness/cost.
2. **Build** — writes code to `workspace/` + commits via GitHub tool (sandbox org only).
3. **Deploy** — via Cloudflare tool (Workers/D1/R2/Queues abstraction).
4. **Monetize** — Dodo product + checkout (test mode unless approved).
5. **Measure** — analytics, errors, payment verification. Reflects, pivots if stalled.

## Safety / control plane (outside agent control)

- `supervisor.py` enforces: max iterations, max runtime, max spend, kill-switch file.
- Credentials live in env / tool layer, never in prompts. Agent gets validated ops only.
- `tools/dodo_tool.py:verify_payments` is the ONLY success signal. Editing SQLite never counts.
- Excluded from revenue: test-mode, refunded, disputed, self-purchase.
- Live Dodo ops blocked unless `APPROVE_LIVE_PAYMENTS=1` + approval file exists.

## Cloudflare mapping

| Agent need | Local (dev) | Cloudflare (prod) |
|---|---|---|
| Structured memory | `data/agent.db` (SQLite) | D1 (see `cloudflare/schema.sql`, same schema) |
| Files/artifacts | `data/artifacts/` | R2 |
| Background jobs | `queue` table in SQLite | Queues |
| Coordination/locks | `state` kv + file lock | Durable Objects |
| Scheduled work | `--daemon` + cron | Cron Triggers (`cloudflare/worker.js`) |

See `cloudflare/wrangler.toml.example` + `cloudflare/worker.js` for the deploy gateway.

## Layout

```
config.py            env + caps (sandbox-first)
memory.py            SQLite store (D1-compatible schema)
supervisor.py        budgets, kill switch, approval gate, verification totals
tools/base.py        ToolResult + registry + audit log
tools/llm_tool.py    OpenAI-compatible LLM, mock fallback
tools/github_tool.py allowlisted repos only, dry-run simulation
tools/cloudflare_tool.py  Workers/D1/R2/Queues abstraction
tools/dodo_tool.py   revenue verification (only success signal)
tools/search_tool.py Brave API or mock
tools/analytics_tool.py PostHog/Sentry stubs + local log
agent/brain.py       goal generation, ranking, planning, reflection
agent/loop.py        continuous Observe→Think→Act→Evaluate→Repeat
main.py              CLI: --once / --cycles / --daemon / --reset / --status
cloudflare/          schema.sql, worker.js, wrangler example
tests/               unittest suite (stdlib only)
```

## Success report

On reaching $25 verified gross, agent stops the revenue objective and writes
`data/SUCCESS.json` + `data/final_report.md`: what it built, funnel, what worked/failed,
revenue/expenses, tool/LLM usage, next steps.
