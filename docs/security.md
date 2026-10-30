# Security

What VERITAS defends against, how, and what it does not cover. Each control
names the code that implements it, so a claim here can be checked.

## What is at stake

The agent posts to a real double-entry ledger. A wrong step moves real money.
The assets, in order:
1. the ledger;
2. the credentials that can write to it;
3. the integrity of the audit trail (traces and human decisions);
4. availability.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| **Prompt injection** through ERP text (item names, supplier names, descriptions) | Every ERP-sourced string shown to a model is quarantined in `«…»`, and both system prompts say it is data, never instructions. The verifier never sees the executor's rationale. Hard accounting invariants are deterministic code, not model output. | `agent/context.py` (`untrusted`), `verify/verifier.py`, `verify/rules.py` |
| Injection that the quarantine misses | Measured, not assumed: the indirect variant is phrased outside the heuristic's patterns, and results report both variants separately. | `harness/faults.py`, `docs/limitations.md` |
| **Compromised or careless request input** | Strict request schema: unknown fields refused, ERP names restricted to a safe character set, bounds on every number. Body size limited, including streamed bodies. | `service/schemas.py`, `service/api.py` (`BodyLimit`) |
| **Replay or duplicate submission** | `Idempotency-Key` is required, unique per client, and bound to a body hash: a replay returns the original workflow, and a reused key with another body is refused. Each ERPNext write carries its own key (Rule 5). | `service/store.py`, `erp/idempotent.py` |
| **Double posting** from concurrency, retries or crashes | ERPNext writes are never retried at the HTTP layer (a rule check enforces it). Each workflow has one renewed lease, checked right before every write. The `commits` primary key is the last guard. | `erp/client.py`, `orchestrator/lease.py`, `scripts/check_write_retry.py` |
| **Stolen API key** | Keys are 32 random bytes, stored as salted SHA-256 only and compared in constant time. Disabled clients are refused. Each client has a rate limit. | `service/auth.py`, `migrations/0002_service.sql` |
| **Malicious or mistaken approver** | Separation of duties: the submitter cannot decide their own request. Approval lifts only a verifier or low-confidence veto, and only when the executor itself again proposes proceeding and the hard rules pass. Hard invariants, the agent's own refusal and a spent budget are never approvable (a rule check enforces it). Decisions are append-only, with who and why. | `service/store.py`, `agent/pipeline.py`, `verify/gate.py`, `scripts/check_rules.sh` |
| **Over-privileged agent** | Step-scoped ERP access: S1–S3 run as a buyer identity with no Accounts role. Production refuses to start without it. The agent identity never holds System Manager. | `erp/scoped.py`, `orchestrator/settings.py` |
| **Credential leakage** | Secrets are `SecretStr`, absent from reprs and errors, and accept `<NAME>_FILE` mounts. Log lines redact tokens, bearer headers, API keys, passwords and DSN credentials. `.env` is gitignored and excluded from the image build context. | `orchestrator/settings.py`, `trace/logging.py`, `.dockerignore` |
| **Tampered audit trail** | `traces` and `human_decisions` refuse UPDATE and DELETE with triggers. Results files record their commit, and `docs/provenance.md` maps rewritten history (Rule 10). | `migrations/0001_baseline.sql`, `0002_service.sql`, `scripts/check_provenance.py` |
| **Insecure deployment** | Production refuses a non-TLS Postgres `sslmode`, the default database password, a missing buyer key and equal model families. It refuses `python -O`, which would strip PRD §9 guards. | `orchestrator/settings.py` |
| **Container escape or lateral movement** | Non-root UID 10001, read-only root filesystem, all capabilities dropped, `no-new-privileges`, no compilers in the image. In Kubernetes: RuntimeDefault seccomp and NetworkPolicies that let the API accept ingress-controller traffic only and limit workers' egress to Postgres, Redis, ERPNext and model endpoints. | `Dockerfile`, `infra/docker-compose/service.yml`, `deploy/helm/veritas/` |
| **Vulnerable dependencies** | `uv.lock` pins everything, and CI fails on a known vulnerability (pip-audit) or a high or critical image finding (trivy). An SBOM is attached to every image. Ruff's bandit rules run on non-test code. | `.github/workflows/` |
| **Outage turned into a bad decision** | An outage is typed and deferred, never scored or escalated as an agent decision, and bounded by `MAX_DEFERRALS` (Rule 7). | `erp/resilience.py`, `orchestrator/queue.py` |

## Not covered

- **The trace explorer has no authentication.** It binds to localhost and must
  not be exposed. Put it behind the API's auth before any shared deployment.
- **No per-tenant isolation.** One deployment serves one ERPNext company.
- **Model-provider data handling.** With `MODEL_BACKEND=openai` and a hosted
  provider, step contexts (ERP data) leave your network. Use a self-hosted
  endpoint for anything sensitive.
- **Key rotation is manual:** issue a new client, then disable the old one.
- **Not penetration-tested.** These controls are tested in code, not by an
  adversary.
