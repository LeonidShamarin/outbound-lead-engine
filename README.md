# Outbound Lead Engine

An outbound lead generation pipeline: company intake, an enrichment cascade that
calls the cheapest data provider first, lead scoring, a "why this person, why now"
hypothesis per segment, AI-written emails with validation, signed event ingest,
and a kill switch that turns off hypotheses that don't get replies.

**Demo mode by design.** Every company and person is fictional, every domain ends
in `.example` (reserved by RFC 2606, so no address can reach a mailbox), data
providers are mocks with the real APIs' response shapes, and sending is a
simulator. No real email is sent and no real person's data is processed. The
LinkedIn Sales Navigator module from the original design stays documentation only:
automating it breaks LinkedIn's terms and gets accounts banned.

> Status: **stage 1 of 6** (schema, migrations, synthetic data). The pipeline
> steps, dashboard and deployment are next; see [Roadmap](#roadmap).

## Stage 1: what exists and what it measured

### Schema, with the original design's bugs fixed

The design this project implements had SQL that would have failed or misbehaved
in production. Each fix below is pinned by a test that runs on a real Postgres:

| Problem in the design | Fix | Test |
|---|---|---|
| Two overlapping runs claimed the same batch and paid for enrichment twice | `claim_companies` / `claim_leads` with `FOR UPDATE SKIP LOCKED` and the status flip in one statement | two open transactions claim 20 + 10 of 30 rows, no overlap |
| "Variables ready" aggregator used `COUNT(DISTINCT (subquery))` in `HAVING`, which Postgres rejects | JSONB containment: `required_variables <@ collected_keys` | full, partial and empty variable sets |
| `mark_lead_failed` inserted another dead letter row on every call after the limit | moves a lead to `dead_letter` exactly once, and back to an explicit retry status, never blindly to `new` | 6 failures: `[F, F, T, F, F, F]`, 1 row |
| The same person could be stored once per provider that found them | unique `(company_id, email)` | duplicate insert raises |
| Statuses were free text, a typo created a status nothing reads | `CHECK` constraints on every status | `'scroed'` is rejected |
| Suppression lookup lower-cased both sides and could not use the key | emails stored lower-case by constraint, plain equality | suppressed lead is not sendable |
| psql-only `\connect` and a non-idempotent `ALTER TABLE` broke any other runner and any re-run | plain SQL, re-runnable | second `migrate` applies nothing |

GDPR deletion removes a person's leads, events, variables and messages, and adds
the address to suppressions even when nothing was found, so a later import is
blocked too.

### Migration runner

Forward-only, one transaction per file, checksummed. An applied file that was
edited afterwards is refused. Writing its test caught a real bug: in psycopg 3 a
`transaction()` block inside an implicitly opened transaction is only a savepoint,
so a failing migration silently rolled back the ones before it. The runner now
requires an idle connection and the test proves a failure in file 2 keeps file 1.

### Synthetic world

`python -m leadengine generate --seed 42 --companies 250`, deterministic per seed.
Measured on seed 42:

| | |
|---|---|
| companies / people | 250 / 882 |
| intake rows (URLs, `www.`, upper case, emails, junk) | 398 |
| after normalisation | 250 inserted, 144 duplicates collapsed, 4 rejected as not a domain |
| second load | 0 inserted |
| people covered by at least one cheap provider (Apollo, Hunter, Snov) | 753 (85.4%) |
| people only the expensive provider has | 121 (13.7%) |
| people no provider has | 8 (0.9%) |
| email status valid / risky / invalid | 698 / 120 / 64 |
| role mailboxes (`info@`, `sales@`) / no last name | 44 (5.0%) / 80 (9.1%) |

Those last rows are what stage 2's cascade and verification have to handle.

## Running it

Requirements: Docker Desktop.

```powershell
docker compose -p outbound-lead-engine up -d        # capped local Postgres on :5433
copy .env.example .env
python -m leadengine migrate
python -m leadengine generate
python -m leadengine seed
```

Tests run only through the capped runner: the test container and a throwaway
Postgres both get a kernel-enforced memory limit, on a private Docker network, and
are removed afterwards.

```powershell
.\scripts\run_tests_capped.ps1
```

Current result: `39 passed`.

If an antivirus on your machine intercepts HTTPS (pip in the image fails with
`CERTIFICATE_VERIFY_FAILED`), run `.\scripts\export_local_ca.ps1` once. It exports
that root certificate to the git-ignored `.certs/`, and the build passes it to
`pip install` as a BuildKit secret. Verification stays on and the certificate is
not stored in the image.

## Roadmap

1. **Schema, migrations, synthetic data** (done)
2. Mock providers with real response shapes, 429s and pagination; intake and the enrichment cascade
3. Scoring, hypotheses and copy through an LLM with schema-validated output, fallbacks and an eval table
4. Sending simulator, HMAC-signed event webhook with idempotency, Wilson-bound kill switch, dead letter
5. Dashboard (Next.js) and deployment: Vercel, Neon, scheduled cycles in GitHub Actions
6. n8n workflows orchestrating the same steps, demo recording
