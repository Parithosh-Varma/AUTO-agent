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
| Telegram Bot API | **Mocked, ready** — 15 mocked tests pass; `telegram.configured: false` here, dry-run safe |
| Dashboard API (local) | **Verified** — token-gated, live smoke test (overview/events/diagnostic) |
| Cloudflare Pages/Worker deploy | **Not deployed** — wrangler missing, no account ID; see §10.5 |

Live financial actions stay disabled (`SANDBOX_MODE=1`, `APPROVE_LIVE_PAYMENTS=0`,
no `data/APPROVE_LIVE`). No production deploy performed.

## 7. Layout

```
config.py            env + caps (sandbox-first, 168h default) + Telegram settings
memory.py            SQLite store (D1-compatible) + protected keys + idempotent queue
                     + notifications outbox
supervisor.py        deadline, split spend/failure limits, kill switch, terminal states
                     + pause/resume/stop (Telegram routes through these)
tools/base.py        ToolResult + audited registry
tools/llm_tool.py    OpenAI-compatible LLM, mock fallback
tools/github_tool.py allowlist-only, dry-run mirror
tools/cloudflare_tool.py  deploy/r2/queue abstractions
tools/dodo_tool.py   authenticated verification only (origin-gated)
tools/search_tool.py Brave API or mock
tools/analytics_tool.py  local log (+PostHog/Sentry when keyed)
tools/build_tool.py  real local prototypes in data/workspace + data/artifacts
tools/telegram_tool.py  official Bot API sender/poller; delivery only on {"ok":true}
notify/alerts.py     deduped/cooldown-guarded outbox, spend/blocker/crash detectors
notify/commands.py   allowlisted /status /why /pause /resume /stop /report
agent/brain.py       goal generation, ranking, planning, reflection
agent/loop.py        Observe→Think→Act→Evaluate→Repeat + terminal guards + notifier hooks
main.py              CLI: --once / --cycles / --daemon / --status / --reset
                     --notify-test / --telegram-poll / --export / --sync / --dashboard
dashboard/           redact.py (secret choke point), api.py (authed backend, local serve),
                     sync.py (runner↔backend push/drain), index.html/app.js/styles.css
                     (static frontend: 6 tabs, polling, stale banner, ?demo mode)
cloudflare/          schema.sql, migrations/0001_observability.sql, worker.js (API+D1),
                     wrangler.toml.example
.github/workflows/agent.yml  bounded cycles + tests + export artifact
tests/               test_agent.py + test_seven_day.py + test_telegram.py (mocked)
                     + test_dashboard.py (auth/ingest/redact/pagination/revenue/deadline/controls)
```

## 8. Current status (last verified 2026-10-10)

- Commit `51c8be9` (+dashboard work below), `main` in sync with origin before this edit.
- `python3 -m unittest discover -s tests`: **55/55 pass**
  (5 core + 16 seven-day + 15 Telegram + 19 dashboard; no live creds).
- `python3 main.py --status`: cycle 17, `experiment_status: active`,
  remaining ~167.4h of 168h, `verified_gross: $0.00 / $25.00`, `live_allowed: false`,
  `telegram.configured: false`.
- Local dashboard API smoke test: `/api/health` public, `/api/overview`,
  `/api/events`, `/api/controls/diagnostic` all live behind `DASHBOARD_TOKEN`.
- `node --check cloudflare/worker.js`: OK.
- `python3 main.py --notify-test` without creds: prints setup hint, sends nothing.
- Cloudflare Pages/Worker: **not deployed** (wrangler missing, no account ID).

## 9. Telegram notifications + intervention

Bot API only (stdlib `urllib`, no new deps). Token is never logged, never in
outputs, never in errors (`tools/telegram_tool.py`). Delivery counts only when
the API responds `{"ok": true}`; anything else is a recorded failure with
backoff, retried from the SQLite outbox (`notifications` table).

### 9.1 Create the bot

1. Chat with `@BotFather` on Telegram: `/newbot` → name → username. Copy the
   `123456:ABC...` token.
2. Start a chat with your new bot (press Start) or add it to your group.

### 9.2 Discover the chat ID

```bash
# after Step 1 + sending /start to the bot:
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
# read result[0].message.chat.id  (use the bot token only here, never commit it)
```

### 9.3 Configure

```bash
# .env (never commit):
TELEGRAMBOTTOKEN=123456:ABC...
TELEGRAMCHATID=123456789
# optional:
# TELEGRAM_ALLOWED_IDS=123456789,987654321   # defaults to TELEGRAMCHATID
# TELEGRAM_TOKEN_FILE=/run/secrets/tg_token  # secret-manager file pattern
# TELEGRAM_ALERT_COOLDOWN_S=3600
# TELEGRAM_SPEND_WARN_PCT=50,80
# TELEGRAM_POLL_ENABLED=1
python3 main.py --status   # check telegram.configured: true (no secrets printed)
```

### 9.4 Test alerts (harmless)

```bash
python3 main.py --notify-test     # sends one test message; confirms API {"ok":true}
python3 main.py --telegram-poll   # poll commands once (see 9.5)
tail -f data/notifications.log
```

Without credentials `--notify-test` prints a setup hint and sends nothing;
unit tests never need credentials (mocked transport).

### 9.5 Commands (allowlisted senders only)

| Command | Effect (via `Supervisor`) |
|---|---|
| `/status` | cycle, gross/target, remaining h, fail streak, active goal |
| `/why` | last failing tool, error summary, streak, next action |
| `/pause` | engage kill switch (`supervisor.pause`); deadline unchanged |
| `/resume` | clear kill switch (`supervisor.resume`); refused when terminal |
| `/stop` | terminal `stopped_kill_switch` + kill switch (`supervisor.stop`) |
| `/report` | gross, usage splits, goal counts, remaining h |

Unauthorized senders get no reply and no state change; the attempt is audit-logged
(`telegram / command:*`). Unknown commands get a usage reply (authorized only).
Every authorized state change is logged to `actions`. Commands cannot change
caps, extend the deadline, bypass `APPROVE_LIVE` approval, or grant permissions —
those code paths do not exist (`notify/commands.py:execute` only calls
`pause`/`resume`/`stop`).

### 9.6 What notifies you, with what detail

Start, blocker-after-bounded-retries, missing credential/permission, repeated
failures, spend thresholds (default 50/80% per metric), pause/resume/shutdown,
$25 target, 168h deadline, crash, recovery, blocker resolution. Each message has:
event type, current goal, affected tool, concise error, retry count, cycle,
gross/target, runtime remaining, experiment status, next action. Secrets and
customer emails are scrubbed (`AlertManager.scrub`).

Flood control: per-event dedupe keys + `TELEGRAM_ALERT_COOLDOWN_S` cooldown;
one-shot flags for start/target/deadline; spend warns once per threshold;
recovery sent once when a blocker clears. A blocked goal is abandoned and work
continues elsewhere; only supervisor-terminal events stop the whole run.

Failure behavior: if Telegram is down, work continues, failures are recorded in
`notifications` (status `failed`, exponential backoff `30·2ⁿ` ≤ 1h) plus
`data/notifications.log`, and `flush()` retries each cycle. A failed send is
never reported as delivered.

## 10. Observability dashboard

Six views (tabs): Overview · Live activity · Goals · Revenue · Infra · Controls.
Answers: what is it doing, why this goal, what shipped/failed, spend, genuine
revenue, blocked?, what it needs. Polls every 5s, shows last-refresh time and a
stale banner when updates stop. Responsive desktop/mobile, with loading, error,
and empty states throughout.

### 10.1 Run locally

```bash
DASHBOARD_TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(24))")
export DASHBOARD_TOKEN
python3 main.py --dashboard   # http://127.0.0.1:8787
# serve the frontend separately (static files, no secrets baked in):
cd dashboard && python3 -m http.server 8080
# open http://127.0.0.1:8080 → token button → paste DASHBOARD_TOKEN
```

The server refuses to start without `DASHBOARD_TOKEN` (fail-closed). The token
lives in `sessionStorage` (this tab only). Demo mode without a backend:
`python3 main.py --export`, copy `data/export.json` → `dashboard/export.json`,
open `index.html?demo` (read-only snapshot banner).

### 10.2 Secrets & access control

| Secret | Where | Never in |
|---|---|---|
| `DASHBOARD_TOKEN` | `.env` / `DASHBOARD_TOKEN_FILE` / Worker secret | code, logs, responses |
| `DODO_WEBHOOK_SECRET` | Worker secret only | code, responses |
| Bot/provider keys | tool layer / env | events, errors, API output |

Every `/api/*` route except `/api/health` requires
`Authorization: Bearer <DASHBOARD_TOKEN>`. State changes accept only
pause/resume/shutdown/diagnostic plus approval decisions, all audit-logged, all
routed through `Supervisor` — caps, deadline, payment rows, and approvals policy
cannot be altered here (no such endpoints exist). Inputs validated
(page/per_page clamped, enums checked, 16KB body cap); per-IP rate limits
(120/min reads, 10/min controls); customer data (`raw_json`, emails) never
selected or returned. For stronger edge protection, put the Pages site behind
Cloudflare Access (optional, recommended).

### 10.3 Event model & revenue honesty

Append-only `events` table (stable `event_id`, `idempotency_key` UNIQUE,
indexes on ts/category/goal) records decisions, tool executions, deployments,
errors, goal changes, checkpoints, and controls **as they occur** in SQLite —
the same schema ships to D1 via `cloudflare/migrations/0001_observability.sql`.
Runner → backend sync (`main.py --sync`, auto-drained each run, plus the
scheduled Actions workflow) ships only events/audit rows; **payments are never
synced** — D1 payment rows enter solely via the HMAC-verified Dodo webhook
(403 when `DODO_WEBHOOK_SECRET` is unset) or server-side poll. Revenue endpoints
recompute from the same `origin='dodo_api'` predicate as the agent, so the
dashboard can never show revenue the verifier would reject. Fees/profit are
labeled `estimate` (or `unavailable` without `DODO_FEE_PCT`); visitor metrics
without PostHog read `unavailable` — never fabricated.

### 10.4 Monitoring & troubleshooting

- Stale banner ≙ no events for >~30s (`dashboard_poll_s × 6`); check daemon/Actions.
- `python3 main.py --status` now includes a `telegram` block (configured/pending/counts).
- `sqlite3 data/agent.db "SELECT category,severity,summary FROM events ORDER BY id DESC LIMIT 10;"`
- 401 from API ≙ missing/wrong token; 429 ≙ slow down; 500 ≙ check server log (never contains tokens).
- Deadline never moves: pause/resume/SQLite→D1 sync preserve `run_started_at`.

### 10.5 Deployment status (honest)

**Not deployed.** Verified preconditions: `node --check cloudflare/worker.js` OK,
55/55 tests pass, local API smoke-tested, workflow YAML valid. Deploy runs from
GitHub Actions (`.github/workflows/agent.yml`, `deploy` job) — never from this
checkout. Remaining, in order:

1. One-time Cloudflare setup (dashboard or `wrangler login` locally):
   `wrangler d1 create auto_memory` → note its database ID; Pages → Create
   application → Upload assets, name it `auto-agent-dashboard`.
2. Repo Settings → Secrets and variables → Actions → New repository secret:
   `CLOUDFLARE_API_TOKEN` (Workers/D1/Pages edit + Account read),
   `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_DATABASE_ID` (step 1),
   plus `DASHBOARD_API_URL` (worker URL after first deploy), `DASHBOARD_TOKEN`.
3. Repo Settings → Environments → New environment `production` (required
   reviewers = you). The `deploy` job waits for your approval every run.
4. Run the workflow (Actions → agent → Run workflow). It renders
   `wrangler.toml` from the example (ID injected, never logged), applies D1
   migrations, deploys the Worker, then the Pages frontend.
5. `wrangler secret put DASHBOARD_TOKEN` + `DODO_WEBHOOK_SECRET` on the worker
   (one-time; secret values never appear in code or logs).
6. Point the frontend at the worker (same-origin proxy or `DASHBOARD_API_URL`;
   currently the app calls same-origin `/api/*`).
7. Send one `--notify-test`-style dashboard check, confirm Telegram
   completion alerts arrive, then start the 7-day window.
