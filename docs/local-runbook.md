# Local runbook: what only the development machine can finish

Everything below needs the live stack (ERPNext, the agent Postgres and Redis)
and the models in Ollama, so it cannot run in CI or a cloud session. Do the
steps in order. Each produces committed results that `docs/results.md` cites.

The commands are for PowerShell on the laptop, from the repo root. The `.sh`
scripts need **Git Bash**. In PowerShell, plain `bash` resolves to WSL's
`C:\Windows\System32\bash.exe`, which fails with `execvpe(/bin/bash)` when no
Linux distro is installed. Define this once per PowerShell session:

```powershell
function gbash { & "C:\Program Files\Git\bin\bash.exe" @args }
```

Then run every `bash scripts/...` line below as `gbash scripts/...`. Run
benchmarks in the **foreground**, in chunks with `--resume`: a killed background
run reports exit 0 and writes nothing (ENGINEERING_RULES §5.4).

## 0. Before anything

```powershell
git checkout main; git pull; uv sync --group dev
docker compose -f infra/docker-compose/stack.yml up -d
gbash scripts/wait_for_erpnext.sh
uv run python scripts/migrate.py
uv run python scripts/repair_poisoned_items.py      # dry run: must report nothing to repair
```

Start Ollama from the app (`%LOCALAPPDATA%\Programs\Ollama\ollama app.exe`), not
with a bare `ollama serve`, then warm both models:

```powershell
uv run python -c "from agent.executor import OllamaLLM; OllamaLLM().complete('ok','ok'); OllamaLLM(model='qwen2.5-coder:7b').complete('ok','ok')"
```

Stop the service worker (`service.worker`) while benchmarking. It shares the
Redis queue and the RAM.

**Commit and push before each run.** A result records the commit it ran on.
That commit must exist on GitHub, or the provenance check (Rule 10) fails, and
it must be the same code the run used. So: a clean working tree, pushed, then
run.

## 1. Evidence version 3: the b6 / c6 / v6 benchmark (hours)

This measures the false-commit fix (`bench/conditions.py`, version 3). It is
the most important run left.

First set the evidence-version-2 results aside. `bench.run` will not resume
into a file recorded under different conditions, and `run_benchmark.sh` refuses
to start until this is done:

```powershell
git restore results/benchmark_ids.json; Remove-Item -ErrorAction SilentlyContinue results/corpus_plan_b6.json
gbash scripts/archive_results.sh evidence-v2-b5-c5-v5
git commit -m "Archive evidence-version-2 results (b5, c5, v5) before the v3 runs"
gbash scripts/run_benchmark.sh        # defaults are now b6 / c6 / v6; resumable
```

Push the archive commit **together with the results** at the end, not before:
until the new runs exist, `results/` has no headline files, and CI's
reproduction check fails. The runs record the archive commit, which becomes
reachable once you push.

If it stops partway, run the same command again: finished workflows are
skipped. Then check two things in the new `docs/results.md`:

- `adversarial_injection_indirect`: is it caught now (v5: 2/4)? And the
  false-commit rate (v5: 26.1%)?
- **Clean controls must stay at 100%** (v5: 8/8). If asking the models to
  judge the quoted item name makes them escalate clean work, that is the cost,
  and it goes in `docs/limitations.md` as measured.

Then confirm the report is reproducible, and commit:

```powershell
gbash scripts/reproduce.sh
git add results docs/results.md docs/compounding_curve.svg; git commit -m "Record evidence-version-3 runs (b6, c6, v6)"; git push
```

`results/calibration_curves.json` now also records `post_ece_ci_low`,
`post_ece_ci_high` and `post_ece_noise_floor`. Copy them into the ECE paragraph
of `docs/limitations.md`.

## 2. The verified load test (about 1–2 h)

The PRD asks for 50 sustained workflows on 4 workers; l1 ran the baseline only.

```powershell
uv run python -m bench.load --config verified --workflows 50 --workers 4 --run-tag l2 --out results/load_verified_results.json
uv run python -m bench.reconcile_ledger --out results/load_verified_ledger_reconciliation.json
```

`bad_postings` must be 0. With two models on one laptop, p95 will be far above
45 s. Record it as measured.

## 3. The service, end to end (15 min)

This is the first time the service runs against the real ERP and models.

```powershell
uv run python scripts/create_api_client.py --id intake --name "Intake" --roles submitter
uv run python scripts/create_api_client.py --id ap-adarsh --name "Adarsh (AP)" --roles approver
uv run uvicorn --factory service.api:create_app_from_settings --port 8090     # terminal 1
uv run python -m service.worker                                             # terminal 2
```

Submit one clean request (README, "Running it as a service"). Follow it with
`GET /v1/workflows/{id}` until it completes, and open the ERPNext links it
returns. Then submit one with `"invoice_rate"` 10% above `"rate"`. It should
land in `GET /v1/escalations`. Try `approve_and_resume` on it with the approver
key: a hard-rule violation must be refused (409). Record what happened in
`docs/limitations.md`, replacing "not yet verified live".

## 4. The demo video (30 min)

Phase 7 asks for a recorded walkthrough. Record the screen (OBS, or Win+Alt+R)
while running:

```powershell
gbash scripts/run_demo.sh
uv run python scripts/trace_explorer.py      # open one caught and one missed workflow
```

Show the compounding curve, one fault caught by the verifier, one caught by the
rules, and the escalation queue. Upload it, then link it at the top of the
README.

## 5. Provenance (5 min)

Run the loop in `docs/provenance.md`, "Confirming an inferred row". For every
commit it can find, change the row's status from `inferred` to `equivalent`.

## 6. Optional: latency on capable hardware

The < 45 s p95 target cannot be met with both models swapping on the laptop.
On a machine with ~24 GB of GPU memory, or against a served endpoint
(`MODEL_BACKEND=openai`, each model declaring its family), rerun step 1 with new
tags. The backend is recorded as a condition, so those numbers stand beside the
laptop ones; they never replace them.
