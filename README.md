# AUTO-agent — 7-Day Autonomous Revenue Experiment
Objective: **$25.00 USD genuine Dodo Payments revenue**. Agent generates its own goals
and strategies. No live charges without explicit approval.

- Deadline: **168 hours from persisted `run_started_at`** (configurable, restarts never reset it).
- Success signal: **only `origin='dodo_api'` + `status='succeeded'` + `currency='USD'` +
  live + non-refunded/disputed + non-self-purchase** rows (see `memory.py:verified_gross`).
- Sandbox-first. Live money requires `SANDBOX_MODE=0` + `APPROVE_LIVE_PAYMENTS=1` +
  `data/APPROVE_LIVE` file. Do not enable for the 7-day dry observation unless authorized.
- Terminal states: `completed_target`, `deadline_expired`, `stopped_*`. No new
  revenue-seeking tasks start after target or deadline (`agent/loop.py:cycle`,
  `supervisor.py:terminal_reason`).

## 1. Setup (exact)

```bash
cd AUTO-agent   # clone of https://github.com/Parithosh-Varma/AUTO-agent
python3 --version          # 3.9+ (stdlib only for core; tested on 3.9.6)
cp -n .env.example .env    # create once; never commit .env
pip install -r requirements.txt   # optional; core runs on stdlib
python3 -m unittest discover -s tests -v
python3 main.py --status
```

## 2. Environment variables (.env)

| Var | Default | Meaning |
|---|---|---|
| `TARGET_REVENUE_USD` | `25.0` | Genuine-revenue target (gross, USD) |
| `CURRENCY` | `USD` | Required payment currency |
| `SANDBOX_MODE` | `1` | Keep `1`. `0` only with written authorization |
| `APPROVE_LIVE_PAYMENTS` | `0` | Keep `0`. Live needs `1` + `data/APPROVE_LIVE` file |
| `MAX_RUNTIME_HOURS` / `EXPERIMENT_HOURS` | `168` | Experiment window from persisted start |
| `MAX_ITERATIONS` | `500` | Cycle cap |
| `MAX_LLM_CALLS` | `1000` | LLM call cap |
| `MAX_TOOL_CALLS` | `5000` | Tool call cap |
| `MAX_LLM_SPEND_USD` | `10.0` | LLM spend cap (est. $0.002/call) |
| `MAX_INFRA_SPEND_USD` | `25.0` | Infra spend cap (est. $0.01/deploy) |
| `MAX_SPEND_USD` | `10.0` | Legacy total cap (enforced alongside splits) |
| `MAX_CONSECUTIVE_FAILURES` | `25` | Failure streak cap |
| `CYCLE_SLEEP_SECONDS` | `5` | Pause between daemon cycles |
| `DATA_DIR` / `KILL_SWITCH_FILE` | `./data`, `./data/STOP` | State dir / external kill switch |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | — | OpenAI-compatible; blank = mock |
| `GITHUB_TOKEN`, `AGENT_ALLOWED_REPOS` | — | Blank = dry-run mirror; else allowlist-only |
| `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_ZONE_ID` | — | Blank = local artifacts; else verify + wrangler |
| `DODO_API_KEY`, `DODO_MODE`, `DODO_BASE_URL` | `test` | Blank = dry-run (cannot succeed); live needs approval |
| `BRAVE_API_KEY`, `POSTHOG_KEY`, `POSTHOG_HOST`, `SENTRY_DSN` | — | Optional; blank = stubs |

Agent code cannot change limits: `run_started_at`, `caps`, `usage`,
`experiment_status` reject direct writes (`memory.py:PROTECTED_STATE_KEYS`);
only `Supervisor` writes via `set_state_internal`.

## 3. Seven-day run

```bash
# fresh experiment (operator-only destructive step: resets deadline + history)
python3 main.py --reset
rm -rf data/workspace data/artifacts   # optional full wipe
python3 main.py --status               # run_started_at set on first start

# continuous run with crash recovery (deadline + usage + cycle persist)
nohup python3 main.py --daemon > data/daemon.log 2>&1 &
echo $! > data/daemon.pid

# single / batched cycles (for testing)
python3 main.py --once
python3 main.py --cycles 20
```

Stop conditions (automatic): verified gross ≥ $25 → `completed_target` + writes
`data/SUCCESS.json` + `data/final_report.md`; 168h elapsed → `deadline_expired`;
kill-switch file present; any hard cap reached; failure streak exceeded.
Restarts reuse the same `run_started_at` and never duplicate jobs (`dedupe_key`)
or payments (`dodo_payment_id` UNIQUE + `origin` gate).

Safe stop / resume:
```bash
touch data/STOP        # independent kill switch; agent has no tool to remove it
tail -f data/daemon.log
python3 main.py --status
rm data/STOP           # resume when authorized
kill $(cat data/daemon.pid)   # stop daemon process itself
```

## 4. Monitoring

```bash
python3 main.py --status    # cycle, elapsed/remaining h, deadline_at,
                            # experiment_status, usage splits, verified_gross
sqlite3 data/agent.db "SELECT * FROM actions ORDER BY id DESC LIMIT 5;"
sqlite3 data/agent.db "SELECT dodo_payment_id,amount,currency,status,is_test,is_refunded,is_self_purchase,origin FROM payments;"
sqlite3 data/agent.db "SELECT kind,status,attempts FROM queue LIMIT 10;"
ls data/artifacts data/workspace
```

Watch: `remaining_hours` → 0 means deadline; `experiment_status` terminal means
no new tasks; `usage` shows `llm_spend_usd` / `infra_spend_usd` separately;
`verified_gross` only moves on authenticated Dodo records.

## 5. Deployment (Cloudflare; requires authorization)

```bash
cp cloudflare/wrangler.toml.example cloudflare/wrangler.toml
# fill database_id, account id; set secrets (never in code):
# wrangler secret put DODO_WEBHOOK_SECRET
wrangler d1 execute auto_memory --file=cloudflare/schema.sql
wrangler deploy
```

Local ↔ CF mapping: SQLite → D1 (`cloudflare/schema.sql`, same schema + `origin`
+ `dedupe_key`), `data/artifacts/` → R2, `queue` table → Queues,
`state` kv → Durable Objects, `--daemon` → Cron Triggers (`cloudflare/worker.js`).
Do not deploy to production or enable live payments without required approval.

## 6. Integration status (this checkout)

| Integration | Status |
|---|---|
| Local build / SQLite / artifacts / queue | **Verified** (21 tests pass; cycles 10→13 built real files) |
| Revenue verification logic (exclusions, idempotency, origin gate) | **Verified** (unit-tested, no key needed) |
| Deadline / crash recovery / limits / termination | **Verified** (unit-tested incl. real `data/agent.db` migration) |
| `cloudflare/worker.js` syntax | **Verified** (`node --check`) |
| LLM (OpenAI-compatible) | **Mocked** — `LLM_API_KEY` empty, mock fallback active |
| GitHub API | **Awaiting credentials** — needs valid `GITHUB_TOKEN` + `AGENT_ALLOWED_REPOS`; dry-run mirror only |
| Cloudflare API | **Awaiting credentials** — `CF_API_TOKEN` empty, local artifacts only |
| Dodo Payments API | **Awaiting credentials** — `DODO_API_KEY` empty, dry-run (cannot reach $25 by design) |
| Brave / PostHog / Sentry | **Stubs** — keys empty, local logs only |

Live financial actions stay disabled (`SANDBOX_MODE=1`, `APPROVE_LIVE_PAYMENTS=0`,
no `data/APPROVE_LIVE`). No production deploy performed.

## 7. Layout

```
config.py            env + caps (sandbox-first, 168h default)
memory.py            SQLite store (D1-compatible) + protected keys + idempotent queue
supervisor.py        deadline, split spend/failure limits, kill switch, terminal states
tools/base.py        ToolResult + audited registry
tools/llm_tool.py    OpenAI-compatible LLM, mock fallback
tools/github_tool.py allowlist-only, dry-run mirror
tools/cloudflare_tool.py  deploy/r2/queue abstractions
tools/dodo_tool.py   authenticated verification only (origin-gated)
tools/search_tool.py Brave API or mock
tools/analytics_tool.py  local log (+PostHog/Sentry when keyed)
tools/build_tool.py  real local prototypes in data/workspace + data/artifacts
agent/brain.py       goal generation, ranking, planning, reflection
agent/loop.py        Observe→Think→Act→Evaluate→Repeat + terminal guards
main.py              CLI: --once / --cycles / --daemon / --status / --reset
cloudflare/          schema.sql, worker.js, wrangler.toml.example
tests/               test_agent.py (core) + test_seven_day.py (deadline/recovery/limits)
```

## 8. Current status (last verified 2026-10-10)

- Commit `72578d1`, `main` in sync with origin; working tree clean before this edit.
- `python3 -m unittest discover -s tests`: **21/21 pass**.
- `python3 main.py --status`: cycle 13, `experiment_status: active`,
  remaining ~167.9h of 168h, `verified_gross: $0.00 / $25.00`, `live_allowed: false`.
- `node --check cloudflare/worker.js`: OK.
