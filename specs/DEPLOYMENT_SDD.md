# AWS Deployment — Software Design Document

Companion to `AVNI_WEBAPP_INTEGRATION_SDD.md`. That SDD designs the FastAPI
service and the React surface; this one designs how the service is hosted,
networked, secured, and rolled out on AWS.

## 1. Objective

Run the `avni-ai-web` FastAPI service (the `src/web/` package added by the
integration SDD) as an AWS-hosted service that:

1. The production `avni-webapp` can reach over HTTPS via a stable hostname.
2. Holds long-lived Server-Sent Events streams without idle-timeout drops.
3. Calls `avni-server` (private) and Anthropic / Voyage (public) outbound.
4. Stores secrets in the same store the rest of the Avni stack already uses.
5. Deploys via the existing Ansible/SSH path that runs `avni-server` and
   `avni-webapp` — no new operational model.
6. Survives single-AZ failure to the same extent `avni-server` does; recovers
   within the declared v1 limits when the systemd unit restarts (see §11).

V1 is single-process (one `uvicorn` worker per env). Horizontal scaling is a
documented follow-up, not a v1 goal — see `AVNI_WEBAPP_INTEGRATION_SDD.md`
§8.3 and §11.

---

## 2. Scope

### In scope

- Co-locating `avni-ai-web` on the existing `avni-server` EC2 boxes
  (staging, prerelease, prod) using the same Ansible/systemd pattern the
  rest of the stack already uses.
- Per-env sizing analysis — confirming the existing instance has headroom
  for the Python service.
- ALB listener rule that routes `/ai-api/*` from each env's existing
  hostname to the Python process on `127.0.0.1:8090`.
- Secrets — extending the existing secrets path (whatever
  `avni-server` already uses) with `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`.
- systemd unit definition + Ansible role.
- Per-env health-check + observability defaults using the existing
  CloudWatch agent.
- Documented migration path to a dedicated EC2 (or Fargate) if usage grows.

### Out of scope

- Terraform / Ansible code itself. This SDD specifies the resources; the
  actual playbook + listener rule edits land as a follow-up PR against
  `avni-infra`.
- Changes to `avni-server`'s deployment story. The integration uses
  already-public avni-server endpoints (`/web/userInfo`, `/import/new`).
- Multi-region. Single region (matches the rest of the Avni stack).
- Blue/green or canary deploys. systemd restart-on-failure is sufficient
  for a single-process v1.
- Disaster recovery beyond "restart the unit and the user re-uploads."
- DataDog / NewRelic. CloudWatch only.
- Provisioning a separate dev account. Dev uses the existing staging EC2
  box with a distinct ALB path.

### Precondition

`AVNI_WEBAPP_INTEGRATION_SDD.md` is implemented. The service runs locally
against staging avni-server. `pip install -e .` succeeds on the target
Python version (3.11+).

---

## 3. Why co-locate on existing EC2 (and not Fargate)

The rest of Avni runs on EC2 with Ansible/systemd provisioning. The original
DEPLOYMENT_SDD draft led with ECS Fargate — that was the right *greenfield*
choice but the wrong *brownfield-Avni* choice. Diverging to a containerised
deployment for one new Python service would introduce a second operational
model for ops to maintain.

| Option | Verdict | Reason |
|---|---|---|
| **Co-locate on existing EC2** | ✅ Chosen | Reuses the existing AMI, Ansible role, secrets path, ALB, DNS, and CloudWatch agent. Marginal cost (~$0/mo on existing boxes — just a systemd unit). One operational model across the whole stack. |
| **Dedicated EC2 per env** | Fallback (prod, if sizing fails) | Same operational pattern as the existing stack, but the Python service gets its own t3.micro / t3.small. Adds ~$8-15/mo per env and one more box to monitor. Use only when the shared box is memory-constrained. |
| **ECS Fargate** | Future option | Cleanest CI/CD (image-based) and easiest horizontal scaling. Worth doing when v2 multi-task lands or if image-based deploys become the org standard. Not v1. |
| **AWS Lambda** | ❌ Ruled out | SSE streams are long-lived (minutes); Lambda's 15-min cap + per-invocation model can't hold an in-process `MemorySaver` checkpoint. |
| **App Runner / EKS** | ❌ Ruled out | App Runner can't customise ALB idle timeout (60s default kills SSE). EKS isn't justified for one service. |

The recommendation is **co-locate on staging + prerelease unconditionally,
and on prod if sizing allows**. Fall back to a dedicated EC2 box only on
prod and only if the existing box can't fit the Python service.

---

## 4. Architecture

```
┌────────────────────────── public internet ──────────────────────────┐
│                                                                     │
│   browser ── HTTPS ──▶ staging.avniproject.org           │
│                                                                     │
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                       ┌───────────▼────────────────────┐
                       │ Existing avni ALB              │
                       │ ─ idle_timeout = 180s          │  ← bumped from 60
                       │ ─ rules:                       │
                       │   /api/* ────▶ avni-server TG  │
                       │   /ai-api/* ─▶ avni-ai-web TG  │  ← new
                       │   /* ────────▶ avni-webapp TG  │
                       └────────┬──────────────┬────────┘
                                │              │
                ┌───────────────▼───┐      ┌───▼───────────────┐
                │ Existing EC2 box  │      │ Existing EC2 box  │
                │  (avni-server)    │ ←──→ │  (avni-webapp)    │
                │  systemd units:   │      │  nginx static     │
                │   avni-server     │      └───────────────────┘
                │   avni-ai-web ←── │  new
                │   :8090 (local)   │
                └─────────┬─────────┘
                          │
              ┌───────────▼────────────┐
              │ NAT GW (existing)      │
              │  api.anthropic.com     │
              │  api.voyageai.com      │
              └────────────────────────┘
```

Both browser-facing endpoints (`/api/*` and `/ai-api/*`) terminate on the
existing ALB. Path-based routing avoids needing a new ACM cert or Route 53
record — same hostname, different path. The Python service binds to
`127.0.0.1:8090` so only the ALB target group sees it.

---

## 5. Per-env sizing

The cheapest path is co-location, but it depends on whether the existing
box has memory headroom for the Python service. v1 footprint:

| Component | Resident memory |
|---|---|
| `avni-ai-web` idle (uvicorn + FastAPI + LangChain imports) | ~250 MB |
| `avni-ai-web` peak (active rule generation, KB loaded) | ~400-500 MB |
| Headroom recommended | +200 MB for OS / agent / etc |

So plan on **~700 MB** carved out for the Python service.

### Per-env recommendation

| Env | Instance | avni-server typical RSS | Headroom for ai-web | Verdict |
|---|---|---|---|---|
| **staging** | **t3.small (2 GB)** | ~1.2-1.5 GB | ~400-700 MB | ⚠️ **Tight** — see §5.1 below |
| **prerelease** | Confirm size | Confirm with `free -m` | Calc | Likely fits |
| **prod** | Confirm size | Confirm with `free -m` + `top` | Calc | Likely fits (prod boxes are usually larger) |

### 5.1 staging on t3.small — three options

t3.small has 2 GB total. With avni-server's JVM at typical settings
(`-Xmx1g` or so → ~1.2-1.5 GB resident), the leftover is right at the
edge of our ~700 MB requirement. Three ways to handle it:

1. **Run with strict JVM heap cap + monitor**: Set avni-server's
   `-Xmx512m` to free up ~500 MB, run `avni-ai-web` co-located, watch
   for OOM kills via CloudWatch alarm. **Cheapest** but riskiest.
2. **Upgrade staging to t3.medium (4 GB)**: +$15/mo. Gives 1.5+ GB of
   permanent headroom. **Recommended** — staging instability blocks the
   whole team, and the cost is trivial.
3. **Add a dedicated t3.micro for ai-web** (1 GB): +$8/mo. Cleaner
   isolation. Slight ops complexity (one more box to apply OS patches to).
   Better than #1, marginally more expensive than #2.

**Recommendation for staging: option 2** (upgrade to t3.medium). Pay $15/mo
to remove ambiguity.

### 5.2 Verifying prerelease + prod sizing

Before deciding co-location vs dedicated for each env, run on the box:

```bash
free -m                    # Total + available memory
ps aux --sort=-%mem | head # Who's using it (look for `java`)
top -bn1 | head -15        # CPU load
```

If `available` memory consistently exceeds 800 MB and load is < 50%,
co-locate. Otherwise dedicate.

---

## 6. Resources to add per env

| Resource | Status | Detail |
|---|---|---|
| Python 3.11 + venv on the box | new (AMI prep) | One-time install in `avni-infra/provision/`'s AMI build. `uv` optional. |
| systemd unit `avni-ai-web.service` | new | `ExecStart=/opt/avni-ai/venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8090`, `Restart=on-failure`, `EnvironmentFile=/etc/avni-ai/.env`. |
| `/etc/avni-ai/.env` | new | Rendered by Ansible from the secrets store. Includes `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `AVNI_SERVER_BASE_URL=http://127.0.0.1:8080`, `AI_WEBAPP_ORIGIN=https://<env>.avniproject.org`. |
| Secrets in the existing secrets store | new entries | Same store avni-server already reads — Secrets Manager or Parameter Store (whichever the existing playbooks use). Two new keys per env. |
| ALB listener rule | new | Path-pattern `/ai-api/*` → forward to new target group. Higher priority than the `/*` catch-all that routes to webapp. |
| ALB target group `tg-ai-web-<env>` | new | HTTP:8090; health check `GET /ai-api/health` (302 → `GET /health` after path stripping, or expose `/ai-api/health` directly — see §7.2). |
| ALB attributes | edit existing | `idle_timeout` bumped from 60 to 180. Affects every service on the ALB — see §7. |
| Security group for EC2 box | edit existing | No new inbound rule needed: 8090 is bound to `127.0.0.1`, accessible only via the ALB target group on the same box. |
| CloudWatch agent config | edit existing | Add a new log stream for `/var/log/avni-ai-web/*.log` (rendered by Ansible from `LOG_FILE` env var). |
| Route 53 / ACM cert | no change | Existing `<env>.avniproject.org` cert covers everything via path routing. |

Total new infrastructure cost per env: **~$0/mo on existing boxes** (just a
systemd unit), plus the optional t3.medium upgrade for staging (+$15/mo).

---

## 7. ALB + SSE specifics

Three settings matter for SSE; defaults will break the feature.

### 7.1 `idle_timeout = 180` on the shared ALB

The existing ALB defaults to 60 seconds. SSE streams are quiet between
events; 60s closes them mid-conversation. Bumping to 180s gives the
25-second keepalive (emitted by `web/events.py`) comfortable margin.

**Caveat — affects every service behind the ALB.** This isn't isolated to
the new target group; ALB `idle_timeout` is per-load-balancer. In
practice 180s is harmless for normal HTTP traffic (avni-server's REST
calls complete in milliseconds; longer-lived clients close their own
connections), but worth confirming with whoever owns the ALB.

### 7.2 Path stripping for `/ai-api/*`

Two ways to route:

- **Strip the prefix** (preferred): ALB target group with action
  `forward → tg-ai-web`, plus a `redirect` or rewrite that drops
  `/ai-api`. Service sees plain paths (`POST /sessions`). Simpler service
  code; needs an ALB rule that does the rewrite (action of type
  `redirect` with path manipulation, OR an nginx sidecar on the box).
- **Mount the prefix in FastAPI**: keep `/ai-api/` on the wire; configure
  `FastAPI(root_path="/ai-api")` so generated OpenAPI URLs stay correct.
  No ALB-side rewrite needed. **Recommended** — simpler.

If we go with FastAPI's `root_path`, the health check is `GET /ai-api/health`.

### 7.3 Stickiness on the session-id cookie

Same as the original SDD: target-group attribute
`stickiness.type = app_cookie`, `cookie_name = AI_SID`, duration 3600s.
Required because `MemorySaver` is in-process. Without stickiness, a second
request for the same session could land on a different box (today there's
only one, but the rule should exist for v2).

### 7.4 No gzip on `text/event-stream`

ALBs do not compress SSE by default. Confirm there's no CloudFront upstream
mistakenly compressing it.

---

## 8. Secrets and config

Inject via the existing secrets path. The `avni-server` Ansible playbook
already renders an env file from Secrets Manager / Parameter Store — add
the new keys to the same source.

| Variable | Source | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secrets Manager: `avni/ai-web/<env>/ANTHROPIC_API_KEY` | Rotated quarterly; rotation = `systemctl restart avni-ai-web`. |
| `VOYAGE_API_KEY` | Secrets Manager: `avni/ai-web/<env>/VOYAGE_API_KEY` | Same. |
| `AVNI_SERVER_BASE_URL` | Parameter Store | `http://127.0.0.1:8080` (loopback to the co-located avni-server). |
| `AI_WEBAPP_ORIGIN` | Parameter Store | `https://<env>.avniproject.org`. |
| `AI_SESSION_IDLE_MIN` / `AI_SESSION_MAX_HOURS` | Parameter Store | Defaults: 30, 2. |
| `AI_WEB_PORT` | Parameter Store | 8090 (avoids the avni-server 8080 collision). |
| `LOG_LEVEL` / `LOG_FILE` | Parameter Store | `INFO`, `/var/log/avni-ai-web/avni.log`. |

The admin user's avni-server bearer token (forwarded for `/import/new`)
lives only in the in-process session record per integration SDD §6.

---

## 9. CI/CD — Ansible / SSH

GitHub Actions workflow `.github/workflows/deploy-ai-web.yml`. Reuses the
same SSH-deploy pattern the existing `avni-server` and `avni-webapp` use:

```
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment: { staging | prerelease | prod }
```

Steps:

1. Checkout.
2. Set up Python 3.11; run `pytest` (gate the deploy on green tests).
3. Build a release tarball: source tree + `pyproject.toml` + frozen
   `requirements.txt` (or `uv.lock` if the team commits it — see follow-up
   discussion).
4. Configure AWS credentials via OIDC (no long-lived keys).
5. `rsync` the tarball to the target EC2 box via SSH (same SSH bastion
   the existing webapp deploys through).
6. Run the Ansible role: extract tarball, recreate venv, `pip install`,
   render env file, `systemctl restart avni-ai-web`.
7. Health-check the ALB: poll `GET /ai-api/health` until 200 (timeout 60s).

Staging auto-deploys on push to `main`. Prerelease + prod are
`workflow_dispatch` only — manual promotion, same git SHA pinned to the
deploy artefact.

**Rollback:** the prior tarball stays in `/opt/avni-ai/releases/<sha>/`;
flip the `/opt/avni-ai/current` symlink and restart. Same pattern the
existing Avni deploys use.

---

## 10. Observability and operations

| Signal | Source | Where |
|---|---|---|
| App logs | stdout/stderr + `/var/log/avni-ai-web/avni.log` | CloudWatch via the existing CloudWatch agent (add a new log stream definition). 30-day retention. |
| Per-session trace | `session_id` tag in every log line (integration SDD §9) | CloudWatch Logs Insights, queryable by `session_id`. |
| ALB metrics | ALB → CloudWatch | `TargetResponseTime`, `HTTPCode_Target_5XX_Count`, `ActiveConnectionCount` (SSE health) on the new target group only. |
| EC2 host metrics | Existing CloudWatch agent | Memory + CPU already collected. Add an alarm: memory > 85% sustained 5 min → SNS to ops. **Critical for the co-located staging box.** |
| Alarms | CloudWatch | (a) systemd unit reports failure → SNS; (b) health-check 5xx rate > 1% over 5 min → SNS; (c) memory > 85% (new — co-location concern). |

**Restart behaviour.** When the systemd unit restarts (deploy, OOM,
hardware failure):

- All active sessions are dropped (integration SDD §8.2). Clients see
  `session.closed`.
- The new process starts cold — empty `MemorySaver`, empty session store.
- Users re-upload the xlsx and resume work.

This is the v1 contract. The fix lives in §11.

---

## 11. Future: multi-process and resume-across-restarts

Single-process v1 was the deliberate trade. When usage warrants it or
"resume across restarts" becomes a requirement, three things change
together — same as the original SDD §12:

1. Swap `MemorySaver` → `PostgresSaver` (RDS Postgres in the same VPC,
   `db.t4g.micro` is enough).
2. Move `ChatSession` records + SSE event-replay buffer to ElastiCache
   Redis (`cache.t4g.micro`).
3. Drop ALB stickiness; allow multiple uvicorn workers behind the target
   group.

After that, **migrating to Fargate or a dedicated EC2 box becomes
straightforward** because the service is stateless. Until then, single
process on shared EC2 is the right shape.

---

## 12. Files to create / change

### avni-ai-tools (this repo)

| File | Status | Description |
|---|---|---|
| `deployment/avni-ai-web.service` | new | systemd unit template (env file path, ExecStart, Restart=on-failure). |
| `deployment/README.md` | new | Operator notes: how to roll back, how to read logs by session, how to rotate API keys. |
| `.github/workflows/deploy-ai-web.yml` | new | Build + rsync + Ansible deploy + health-check polling. |
| `.github/workflows/test.yml` | edit / reference | Reuse for the pytest gate. |
| `src/web/app.py` | edit | Add `root_path` configurability via `AI_WEB_ROOT_PATH` env (defaults to `""`, set to `/ai-api` in prod). |

### avni-infra (follow-up PR, not built in this SDD)

| File | Status | Description |
|---|---|---|
| `provision/ai-web/` | new | Ansible role: install Python, create user + dirs, install venv, render env file, manage systemd unit. |
| `provision/<env>/group_vars/ai-web.yml` | new | Per-env values (memory limits, log paths, secrets store keys). |
| ALB listener rule + target group config | edit | New `/ai-api/*` rule pointing at port 8090 on each EC2 box. |
| `idle_timeout` on the shared ALB | edit | 60 → 180. |

### avni-webapp

Already covered in the integration SDD. Set
`window.ENV.AI_ASSISTANT_URL = "https://<env>.avniproject.org/ai-api"` per
env at build time.

---

## 13. Out of scope (recap)

- Provisioning a dedicated EC2 box per env upfront (only as a fallback if
  staging sizing fails — see §5.1).
- Fargate / containerised deployment (future option per §3, §11).
- Multi-region / DR beyond "systemd restart-on-failure".
- RDS / ElastiCache provisioning (lands with v2 per §11).
- Blue/green or canary deploy strategy.
- DataDog / NewRelic / Sentry integration.
- Hosting inside the existing `avni-server` JVM.
