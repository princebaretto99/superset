# Devin Issue Remediation

An event-driven automation that watches a GitHub repo's issues, opens a
[Devin](https://docs.devin.ai/api-reference/overview) session for each one, manages
those sessions until they finish, and reports back on the issue as a pull request.

```
 trigger              one run does both               observable output
┌──────────────┐    ┌────────────────────────┐    ┌────────────────────────┐
│ schedule     │    │ advance running        │    │ comment on the issue    │
│ webhook      │──▶ │   sessions             │──▶ │ devin:* labels          │
│ manual       │    │ start sessions for     │    │ pull request            │
└──────────────┘    │   issues without one   │    │ report.md + ledger.json │
                    └────────────────────────┘    │ stats.md  + stats.json  │
                                                  └────────────────────────┘
```

## The files

| File | What it is |
|---|---|
| `remediate.py` | The whole automation: both API clients, the loop, the report, the stats dashboard. |
| `webhook.py` | Optional receiver so a GitHub webhook can trigger a run instantly. |
| `.github/workflows/devin-remediate.yml` | Runs it every 15 minutes in GitHub Actions. |
| `test_remediate.py` | Unit tests over the loop, with fake API clients. |
| `test_integration.py` | Full cycle through the real HTTP clients against a stub server. |

## How it works

**One run does everything.** It looks at every open issue and either advances the
Devin session that issue already has, or starts one if it has none. There is no
queue and no database.

**State lives in the issue.** Each issue gets exactly one bot comment, edited in
place as things progress. Hidden at the bottom of it is the state:

```html
<!-- devin-state {"session_id": "devin-abc", "status": "working", "pr": null, ...} -->
```

That comment is the only record the automation keeps, which makes runs safe to
repeat: a re-run, a duplicate webhook, or a run that died half-way all land in the
same place, and a session is never started twice for the same issue.

**Lifecycle:**

```
devin:queued ─▶ devin:working ─┬─▶ devin:pr-open       (PR opened — done)
                               ├─▶ devin:needs-human   (finished, no PR)
                               ├─▶ devin:blocked       (Devin asked a question)
                               └─▶ devin:failed        (expired or timed out)
```

Add `devin:skip` to an issue and the automation leaves it alone.

**Devin API calls used** (`https://api.devin.ai/v1`):

| Call | Where |
|---|---|
| `POST /sessions` | Start a session. Sends `idempotent`, `max_acu_limit`, `tags` and a `structured_output_schema`. |
| `GET /sessions/{id}` | Poll `status_enum`, read `structured_output` and `pull_request.url`. |
| `POST /sessions/{id}/message` | Nudge a session that has gone `blocked`. |
| `GET /sessions` | Used by `doctor` to check the key works. |

The `structured_output_schema` is what makes the output readable by machine: Devin
fills in `root_cause`, `fix_summary`, `files_changed`, `tests_run`, `confidence`,
`pull_request_url` and `needs_human` as it works, and those land in the issue
comment and the ledger.

## Setup

The target repo needs **Issues enabled**. Forks have them off by default —
Settings → General → Features → Issues. Nothing else is installed into it.

```bash
pip install -r requirements-dev.txt
cp .env.example .env        # fill in TARGET_REPO, GH_TOKEN, DEVIN_API_KEY
set -a && source .env && set +a
```

```bash
python remediate.py doctor
```

Checks both credentials, confirms Issues are enabled, and counts eligible issues.

```bash
python remediate.py run --dry-run
```

Reads the real issues and prints the exact `POST /sessions` payloads it *would*
send. Costs nothing. Always do this first.

```bash
python remediate.py run --issue 4     # one issue, live
python remediate.py run               # everything, live
```

## Is it working? — `stats`

```bash
python remediate.py stats             # markdown dashboard
python remediate.py stats --json      # same numbers, machine-readable
```

This is the answer to *"if I were an engineering leader, how would I know this is
working?"*. It reads every issue in the repo — **open and closed** — plus the state
markers, and reports:

| Section | Answers |
|---|---|
| **Is it working?** | How many issues were picked up, how many produced a PR, how many were handed back to a human, how many failed, how many are in flight right now |
| **Speed** | Median time from issue opened → session started, and session started → PR |
| **Throughput** | Sessions started and PRs opened in the last 24h and 7d |
| **Pipeline** | Live count at each stage |
| **Every issue** | One row per issue: stage, session link, PR link, pickup time, duration |

The headline numbers are **success rate** (PRs ÷ completed sessions), **escalation
rate** (needed a human), and **failure rate**. An issue that was closed after its
PR landed is counted separately as `closed_with_pr` — the strongest signal the
system actually resolved something.

Every number is derived from GitHub itself, so there is no metrics database to
keep in sync, nothing to back up, and the figures stay correct after a lost
artifact or a re-run. `stats.json` is written alongside `stats.md` for anything
downstream.

The workflow runs `stats` on **every** run (with `if: always()`, so a failed run
still publishes its dashboard) and appends it to the Actions job summary.

### In GitHub Actions

Push this folder to its own repo, then set:

- Variable `TARGET_REPO` → e.g. `princebaretto99/superset`
- Secret `GH_TOKEN` → a PAT with `issues:write` on the target repo
  (the built-in `GITHUB_TOKEN` only works on the repo the workflow lives in)
- Secret `DEVIN_API_KEY` → your Devin key

The workflow then runs every 15 minutes. Use **Run workflow** with `dry_run: true`
(the default) for a safe first run, and check the job summary.

### Triggering on the webhook instead of the schedule

The 15-minute schedule is the simplest trigger and needs no infrastructure. For an
instant reaction there are two options:

1. **`repository_dispatch`** — have anything that sees the GitHub webhook call
   `POST /repos/<this-repo>/dispatches` with
   `{"event_type": "issue-event", "client_payload": {"issue": 4}}`.
2. **`webhook.py`** — run it somewhere reachable (a tunnel is fine), add a webhook
   on the target repo for the *Issues* event pointing at `/webhook`, and set
   `WEBHOOK_SECRET` to the same secret. Every delivery is HMAC-verified before
   anything runs.

## Cost controls

Devin sessions cost ACUs, so the loop is capped by default:

| Setting | Default | What it does |
|---|---|---|
| `MAX_ACU` | 10 | Hard ACU ceiling per session |
| `MAX_NEW_PER_RUN` | 3 | New sessions started in one run |
| `MAX_IN_FLIGHT` | 5 | Sessions running at once |
| `TIMEOUT_MINUTES` | 240 | Older than this is marked failed, freeing a slot |
| `AUTO_NUDGE` | true | Nudge a blocked session once, then ask a human |

Anything over the cap is reported as `deferred` and picked up next run.

## Notes

- **Issue text is untrusted.** It is passed to Devin inside an `<issue_body>` fence,
  truncated, with any nested closing tag stripped, and the prompt says explicitly
  that it is a bug report and not instructions.
- **PRs are pinned to the target repo.** If the target is a fork, the prompt tells
  Devin not to open the PR against the upstream repository — the most likely way
  for this to go wrong.
- One failing issue never aborts a run; the error goes in the report and the rest
  keep going.

## Tests

```bash
python -m pytest
```

33 tests, no network and no ACUs. `test_integration.py` runs a stub HTTP server
that speaks both APIs, so the real client code is exercised too.
