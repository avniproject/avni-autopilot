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
4. Stores secrets in AWS Secrets Manager, never in the image.
5. Deploys from `main` via GitHub Actions in <10 min, end-to-end.
6. Survives single-AZ failure for the always-on path; can recover within the
   declared v1 limits when the task restarts (see §11).

V1 is single-task (one Fargate container). Horizontal scaling is a documented
follow-up, not a v1 goal — see `AVNI_WEBAPP_INTEGRATION_SDD.md` §8.3 and §11.

---

## 2. Scope

### In scope

- AWS architecture: VPC placement, ECS Fargate service + task definition,
  ALB + ACM certificate, ECR repository, Secrets Manager entries, CloudWatch
  log group, IAM roles.
- Dockerfile + image build for the FastAPI service.
- GitHub Actions pipeline that builds, pushes to ECR, and rolls the ECS
  service.
- Health-check, deploy-strategy, and observability defaults.
- Cost estimate at v1 scale.
- Migration path to multi-task (RDS + ElastiCache) called out but not built.

### Out of scope

- Terraform module wiring inside `avni-infra/provision/`. This SDD specifies
  the AWS resources; the actual `.tf` files land as a follow-up PR against
  `avni-infra` (the repo is on Terraform 0.11-era syntax — modernising it is
  its own project).
- Changes to `avni-server`'s deployment. The integration uses
  already-public endpoints (`/me`, `/import/new`); no server-side changes.
- Multi-region. Single region (matches the rest of the Avni stack).
- Blue/green or canary deploys. Rolling deploy with one task at a time is
  sufficient given the single-task v1.
- Disaster recovery beyond "restart the task and the user re-uploads."
- DataDog / NewRelic integration. CloudWatch only in v1.
- Provisioning a separate dev account. Dev uses the existing staging account
  with a distinct ECS cluster + DNS name.

### Precondition

`AVNI_WEBAPP_INTEGRATION_SDD.md` is implemented and the service runs locally
against a staging `avni-server`. Container image builds and runs.

---

## 3. Why Fargate (and not EC2 or Lambda)

The rest of Avni runs on EC2 with SSH-provisioned systemd units (see
`avni-infra/provision/webapp/webapp.tf`). Diverging to Fargate for this one
service is a deliberate choice — written down here so the trade is explicit.

| Option | Verdict | Reason |
|---|---|---|
| **ECS Fargate** | ✅ Chosen | No host to patch; ALB-native; CI/CD is one `ecs update-service` call; right-sized for a single Python container with SSE; cost at v1 (~0.5 vCPU, 1 GB) is ~$15-20/mo, lower than the t3.small EC2 the rest of the stack uses. |
| **EC2 + systemd** | Alternative | Matches existing avni-infra pattern, lets ops reuse runbooks. Adds AMI maintenance, deploy via SSH/Ansible, manual ALB target registration. Acceptable fallback if the team prefers operational consistency over container ergonomics — §12 documents the changes. |
| **AWS Lambda** | ❌ Ruled out | SSE streams are long-lived (minutes per session); Lambda's 15 min hard cap and per-invocation model are incompatible with the in-process `MemorySaver` checkpoint. |
| **App Runner** | ❌ Ruled out | Simpler than Fargate but no control over ALB idle timeout (60 s default kills SSE) and no straightforward path to sticky sessions when v2 multi-task lands. |
| **EKS** | ❌ Ruled out | One service does not justify a Kubernetes control plane. |

The recommendation is Fargate; the EC2 alternative is documented for the case
where ops want to keep one operational model across the stack.

---

## 4. Architecture

```
┌────────────────────────── public internet ──────────────────────────┐
│                                                                     │
│   browser ── HTTPS ──▶ ai-assistant.<env>.avniproject.org           │
│                                  │                                  │
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                       ┌───────────▼────────────┐
                       │ Route 53 alias         │
                       │ → ALB (ai-assistant)   │
                       └───────────┬────────────┘
                                   │ HTTPS:443 (ACM cert)
                                   │ idle_timeout = 180 s
                                   │ stickiness = app_cookie ("AI_SID")
                                   │
                       ┌───────────▼────────────┐
                       │ Target group           │
                       │ port 8080 / HTTP       │
                       │ health: GET /health    │
                       └───────────┬────────────┘
                                   │
                  ┌────────────────▼────────────────┐
                  │ ECS service (Fargate)           │
                  │ desired_count = 1               │
                  │ deployment = rolling, max=1     │
                  └────────────────┬────────────────┘
                                   │
                  ┌────────────────▼────────────────┐
                  │ Fargate task                    │
                  │ 0.5 vCPU / 1024 MB              │
                  │ private subnets (2 AZ)          │
                  │ env from Secrets Manager        │
                  │ logs → CloudWatch               │
                  └─────┬────────────┬─────────────┘
                        │            │
            ┌───────────▼──┐   ┌─────▼────────────┐
            │ avni-server  │   │ NAT GW           │
            │ (private)    │   │ → api.anthropic. │
            │ /me, /import │   │   com            │
            └──────────────┘   │ → api.voyageai.  │
                               │   com            │
                               └──────────────────┘
```

Single AZ for the Fargate task at v1 (one task), but the subnets list spans
two AZs so a restart by the ECS scheduler can move the task on AZ failure.

---

## 5. AWS resources

| Resource | Name (per env) | Notes |
|---|---|---|
| ECR repository | `avni/ai-web` | Single repo; tag per git SHA + `staging` / `prod`. |
| ECS cluster | `avni-ai-<env>` | Dedicated cluster (isolates the Python service from the existing Java EC2 fleet). |
| ECS service | `ai-web` | `desired_count=1`; `deployment_minimum_healthy_percent=0`, `maximum_percent=100` (single-task rolling deploy). |
| ECS task definition | `ai-web:<rev>` | 0.5 vCPU, 1024 MB; `awsvpc` mode; container port 8080. |
| ALB | `alb-ai-assistant-<env>` | HTTPS:443; HTTP:80 → 443 redirect; `idle_timeout = 180 s`. |
| Target group | `tg-ai-web-<env>` | HTTP:8080; health `GET /health` (200); `stickiness = app_cookie name="AI_SID" duration=3600`. |
| ACM cert | `ai-assistant.<env>.avniproject.org` | DNS-validated. |
| Route 53 record | A-alias to ALB | |
| Secrets Manager | `avni/ai-web/<env>` | One JSON secret with `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`. |
| Parameter Store (SSM) | `/avni/ai-web/<env>/AVNI_SERVER_BASE_URL`, `/.../AI_WEBAPP_ORIGIN`, `/.../LOG_LEVEL` | Plain config; cheaper and easier to audit-diff than Secrets Manager. |
| CloudWatch log group | `/avni/ai-web/<env>` | Retention 30 days. |
| Security group `sg-ai-web-alb` | ALB inbound | 443 from `0.0.0.0/0` (public-facing webapp). |
| Security group `sg-ai-web-task` | Task | 8080 from `sg-ai-web-alb` only; egress all. |

Two envs: `staging` and `prod`. Same resource shapes, different sizes only if
prod load demands it (defaults match between envs at v1).

---

## 6. Networking

- **VPC.** Re-use the existing Avni VPC in the target account (the same one
  that hosts `avni-server`). Don't provision a new one — the task needs
  private access to `avni-server` and a new VPC would force VPC peering.
- **Subnets.** Two private subnets across two AZs from the existing VPC.
  The task runs in the private subnets; the ALB sits in the public subnets
  (same ones the existing webapp ALB uses).
- **NAT.** Outbound to `api.anthropic.com` and `api.voyageai.com` via the
  existing NAT gateway. Both domains are accessed by hostname; no IP allowlist
  needed.
- **avni-server reachability.** If `avni-server` is in the same VPC, talk to
  it on its private DNS name (set `AVNI_SERVER_BASE_URL=http://avni-server.
  internal:...`). If it's only reachable via its public hostname today, use
  that — the cost is one round-trip out-and-back through the NAT, which is
  negligible at session-creation rate.
- **Egress.** Task SG allows all egress; nothing inbound except 8080 from the
  ALB SG.

---

## 7. ALB and SSE specifics

Three settings matter for SSE; defaults will break the feature:

1. **`idle_timeout = 180`** (default is 60). SSE streams are quiet between
   events; 60 s closes them mid-conversation. 180 s gives the heartbeat
   comfortable margin.
2. **Stickiness on a session-id cookie.** Target-group attribute
   `stickiness.type = app_cookie`, `stickiness.app_cookie.cookie_name = AI_SID`,
   duration 3600 s. The FastAPI service sets `AI_SID=<session_id>` on
   `POST /sessions`. The ALB then routes every subsequent request for that
   session to the same task — required because `MemorySaver` is in-process
   (integration SDD §8.3).
3. **HTTP/2 enabled, gzip off for `text/event-stream`.** The ALB does not
   compress SSE; ensure no CloudFront or reverse proxy upstream toggles it
   on by mistake. There is no CloudFront in front of this service in v1.

The FastAPI service emits a keepalive comment (`:\n\n`) every 25 s on every
SSE stream — well inside the 180 s window. This is implemented in
`src/web/events.py` (integration SDD §10).

---

## 8. Secrets and config

Everything separates into "config" (read-only env, fine in SSM Parameter
Store) and "secrets" (Secrets Manager). Both are injected into the task at
container start via the ECS task definition's `secrets` block — no app code
talks to either service directly.

| Variable | Source | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secrets Manager `avni/ai-web/<env>` → key `ANTHROPIC_API_KEY` | Rotated quarterly; rotation is a no-op restart. |
| `VOYAGE_API_KEY` | Secrets Manager `avni/ai-web/<env>` → key `VOYAGE_API_KEY` | Same. |
| `AVNI_SERVER_BASE_URL` | SSM Parameter Store | e.g. `https://staging.avniproject.org`. |
| `AI_WEBAPP_ORIGIN` | SSM | Used for CORS allowlist. |
| `AI_SESSION_IDLE_MIN` | SSM, default 30 | Per integration SDD §8.1. |
| `AI_SESSION_MAX_HOURS` | SSM, default 2 | Same. |
| `LOG_LEVEL` | SSM, default `INFO` | |
| `LANGSMITH_*` | SSM, optional | Off by default in prod; on in staging for triage. |

The admin user's auth token (used for the `/import/new` relay) is **never**
stored anywhere durable — it lives only in the in-process session record per
integration SDD §6 and §9.

---

## 9. IAM

Two task roles:

**`ecs-ai-web-execution`** (ECS uses this to pull the image and read secrets):

- `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`
  on the `avni/ai-web` repo.
- `secretsmanager:GetSecretValue` on `avni/ai-web/<env>/*`.
- `ssm:GetParameters` on `/avni/ai-web/<env>/*`.
- `logs:CreateLogStream`, `logs:PutLogEvents` on the log group.

**`ecs-ai-web-task`** (the running container assumes this):

- No AWS API calls in v1 — the service only talks to Anthropic, Voyage, and
  `avni-server` over HTTPS, none of which need AWS credentials.
- Empty policy. When v2 adds RDS / ElastiCache, this role grows accordingly.

Keeping the task role empty in v1 means a compromised container has zero AWS
blast radius beyond its own logs.

---

## 10. CI/CD

GitHub Actions workflow `.github/workflows/deploy-ai-web.yml` in this repo
(`avni-ai-tools`):

```
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment: { staging | prod }
```

Steps:

1. Checkout.
2. Set up Python 3.11, run `pytest` (gate the deploy on green tests).
3. Configure AWS credentials via OIDC (no long-lived keys; trust policy on
   the GitHub-Actions OIDC provider in each AWS account).
4. `docker build -t $ECR_URI:$SHA -t $ECR_URI:$ENV .` and push both tags.
5. Render the task definition from a template, substituting the new image
   URI; register a new revision.
6. `aws ecs update-service --force-new-deployment --task-definition <new-rev>`.
7. Wait for `services-stable` (timeout 10 min). Fail the workflow if the new
   tasks don't pass health checks.

Staging deploys on every push to `main`. Prod is `workflow_dispatch` only —
explicit promotion, same image SHA as staging (pull `:staging`, retag
`:prod`, redeploy). This keeps the "what's in prod" answer one tag lookup
away.

**Rollback:** redeploy the previous task-definition revision via the AWS
console or `aws ecs update-service --task-definition <prev-rev>`. The image
for the previous revision stays in ECR (lifecycle policy keeps the last 10).

---

## 11. Observability and operations

| Signal | Source | Where it goes |
|---|---|---|
| App logs | stdout/stderr from the FastAPI service | CloudWatch log group, 30-day retention. |
| Per-session trace | `session_id` tag in every log line (integration SDD §9) | CloudWatch Logs Insights queryable by session. |
| LLM cost + latency | LangSmith (env-var-toggled) | LangSmith dashboard; off in prod by default. |
| ALB metrics | ALB → CloudWatch | `TargetResponseTime`, `HTTPCode_Target_5XX_Count`, `ActiveConnectionCount` (SSE health). |
| ECS task health | ECS → CloudWatch | `CPUUtilization`, `MemoryUtilization`, `RunningTaskCount`. |
| Alarms | CloudWatch | (a) Service has 0 running tasks for 2 min → SNS to ops; (b) ALB 5xx rate > 1% over 5 min → SNS. |

The service is intentionally lightweight on metrics in v1. The integration
SDD's failure-mode table already covers the user-visible failure surface;
ops alarms only fire when the service itself is down or repeatedly erroring.

**Restart behaviour.** When ECS replaces the task (deploy, AZ failure,
health-check failure):

- All active sessions are dropped (integration SDD §8.2). Clients see
  `session.closed`.
- The new task starts cold — empty `MemorySaver`, empty session store.
- Users re-upload the xlsx and resume work.

This is the v1 contract. The fix is §12.

---

## 12. Future: multi-task and resume-across-deploys

Single-task v1 was the deliberate trade. When usage warrants horizontal
scaling or "I closed my laptop yesterday, finish the bundle today" becomes a
requirement, three things change together:

1. **LangGraph checkpointer.** Swap `MemorySaver` → `PostgresSaver` (a small
   RDS Postgres in the same VPC, db.t4g.micro is enough at any plausible
   v2 scale). Now the LangGraph state survives task restarts and is visible
   to every task.
2. **Session store.** Move the `ChatSession` records and the SSE event-replay
   buffer to ElastiCache Redis (single-node, `cache.t4g.micro`). Every task
   queries Redis to find the session's task affinity (still required because
   the SSE stream is owned by one task at a time).
3. **ALB stickiness.** Keep stickiness, but the cookie now keys an *affinity*
   that any task can resolve via Redis instead of a single-task constraint.
   `desired_count` can rise above 1; deploys roll one task at a time without
   dropping sessions, because the next task picks up the session's state from
   Postgres + Redis.

Additional changes that come with v2:

- Task IAM role gains `rds:Connect` (IAM auth to Postgres) and Redis
  endpoint access.
- The Dockerfile gains the Postgres client lib.
- `pyproject.toml` adds `langgraph-checkpoint-postgres` and `redis`.
- A `migrations/` folder under `src/web/` for the Postgres schema.

None of this is built in v1. Calling it out so the day-one architecture
decisions (single Fargate cluster in an existing VPC, ALB with sticky
cookies, task role that does no AWS work) all extend without rework.

---

## 13. EC2 alternative (if the team prefers operational consistency)

Documented for completeness; pick one path before implementing.

**Shape:** one t3.small EC2 instance per env, in the same VPC as the rest of
the Avni stack. systemd unit runs `uvicorn web.app:app --port 8080`. Behind a
new ALB (same shape as §4) — the ALB layer is unchanged, only what sits
behind it differs.

**What changes from §3–§11:**

- ECR + ECS cluster + task definition → replaced by an AMI / Ansible
  playbook under `avni-infra/provision/ai-web/`, matching the
  `avni-infra/provision/webapp/` pattern.
- CI/CD: GitHub Actions builds a venv, rsyncs to the EC2 instance via SSH,
  restarts the systemd unit. Slower (~3-5 min) and requires an SSH bastion
  the way the current webapp deploy does.
- Secrets: `.env` file written by the playbook from Secrets Manager values,
  read by systemd's `EnvironmentFile=`.
- Restart contract is identical to Fargate (sessions drop on restart).
- Cost: ~$15/mo for t3.small vs ~$18/mo for Fargate at the chosen size.
  Effectively a wash.

The trade is: EC2 reuses the team's existing operational model and SSH
deploy story; Fargate avoids AMI patching and gives a cleaner CI/CD path at
the cost of introducing one new pattern. The recommendation in this SDD is
Fargate; the EC2 path is acceptable if ops would rather not own two
operational models.

---

## 14. Files to create / change

### avni-ai-tools (this repo)

| File | Status | Description |
|---|---|---|
| `Dockerfile` | new | Python 3.11 slim base; copies `src/` and `resources/rules/`; `CMD uvicorn web.app:app --host 0.0.0.0 --port 8080`. |
| `.dockerignore` | new | Excludes tests, requirements xlsx, logs. |
| `.github/workflows/deploy-ai-web.yml` | new | Build + push + ECS update flow described in §10. |
| `.github/workflows/test.yml` | edit | Reuse for the pytest gate (already exists; reference it from the deploy workflow). |
| `deployment/task-definition.json.tmpl` | new | Templated ECS task definition; placeholders for image URI, secret ARNs, log group. |
| `deployment/README.md` | new | Operator notes: how to roll back, how to read logs by session, how to rotate API keys. |
| `src/web/app.py` | edit | Add a `GET /health` endpoint returning `200 {"ok": true}` and a CORS middleware reading `AI_WEBAPP_ORIGIN`. |

### avni-infra (follow-up PR, not built in this SDD)

| File | Status | Description |
|---|---|---|
| `provision/ai-web/` | new | Terraform module for the resources in §5 — ECR, ECS cluster + service, ALB, target group, SGs, log group, Route 53 record, ACM cert, Secrets Manager + SSM entries, IAM roles. |
| `provision/vars/<env>/ai-web.tfvars` | new | Per-env values (domain name, VPC + subnet IDs, AZs, certificate SANs). |

### avni-webapp (no changes from this SDD)

The integration SDD already covers the React changes. Deployment just sets
`window.ENV.AI_ASSISTANT_URL = "https://ai-assistant.<env>.avniproject.org"`
at build time per env, the same mechanism used for the existing API base URL.

---

## 15. Out of scope (recap)

- Terraform code itself (specified here; implemented in avni-infra later).
- Multi-region / DR beyond "task restarts on AZ failure."
- RDS / ElastiCache provisioning (lands with v2 per §12).
- Blue/green or canary deploy strategy.
- DataDog / NewRelic / Sentry integration.
- Cost optimisation below the v1 baseline (~$20/mo Fargate is already
  small enough that scrutiny adds no value).
- Hosting inside the existing `avni-server` JVM.
