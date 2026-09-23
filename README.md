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

> Status: **stage 3 of 6** (schema, synthetic data, mock providers, enrichment
> cascade, scoring, theories, LLM-written emails, reply classification). Sending,
> dashboard and deployment are next; see [Roadmap](#roadmap).

## Stage 3: scoring, theories, emails, replies

LLM: Groq, `openai/gpt-oss-120b` for emails and `openai/gpt-oss-20b` for replies,
both with strict JSON schema output. Every answer is still validated in code,
because a schema guarantees the shape, not the content. All numbers below come
from real Groq runs; per-item results are in [`eval/results/`](eval/results).

### Emails: 30 leads through the whole pipeline on Groq

`python -m leadengine prepare --copy-limit 30 --dump` on the seed 42 world.

| | |
|---|---|
| valid on the first answer | 29 of 30 |
| valid after one repair | 1 (it used no fact from the lead's data) |
| rejected for good | 0 |
| average length | 48 words (limit 90) |
| cost | $0.0114 for 30 emails with two variants each, **$0.38 per 1000** |
| latency | 1.2 s per request |

The validator carries over the design's rules (subject of 6 words or fewer and
lowercase, body of 90 words or fewer and 3 paragraphs or fewer, ends with a
question, no placeholders, no "I hope this finds you well", no links, A differs
from B) and adds the one the design left to the prompt: **no invented facts**.
Every number in an email must appear in the lead's own data, and "our customers"
is rejected. The design's own sample email said "Three of our customers hired
their VP DG with us", a claim a recipient can check. A rejected answer goes back
to the model once with the reasons; after that the lead is retried on the next run
and moved to `dead_letter` after 3 attempts.

Read by eye, the emails are specific and factual. Two weaknesses the validator
does not catch: an unverifiable generalisation without a number ("seeing many SaaS
teams add HubSpot") passes, and almost every variant A opens with "I saw ...".

### Scoring: an LLM against a rubric it was given

The design scored every lead with an LLM. The rubric only uses fields that are
already structured (seniority, industry, size band, country), so here it is
computed in code, and the LLM scorer was measured against it on 60 leads with the
rubric written into its prompt:

| model | same score | within 1 | same send / don't send decision | cost per 1000 leads |
|---|---|---|---|---|
| `gpt-oss-20b` | 56.7% | 98.3% | 83.3% | $0.07 |
| `gpt-oss-120b` | 68.3% | 100% | 83.3% | $0.14 |

Nearly every disagreement is the model scoring one point lower: it uses the number
of matching criteria as the score (2 matches, score 2) although the prompt says 2
matches is 3. The smaller model also misread facts ("201-500" as not in the ICP,
"1000+" as a negative signal). With either model, **1 lead in 6 would change
between contacted and not contacted**. So scoring is code (`--scoring rules`, the
default), the LLM scorer stays as an option, and a failed LLM call falls back to
the rubric and is counted.

### Reply classification: 40 labelled replies

[`eval/replies.jsonl`](eval/replies.jsonl): six classes, including the design's
subtle cases ("busy right now" is neutral, "not interested and stop emailing me" is
unsubscribe, an out-of-office that names a colleague is not a referral).

| | accuracy | cost per 1000 replies |
|---|---|---|
| keyword rules (baseline) | 70% (28 of 40) | 0 |
| `gpt-oss-20b` | 100% (40 of 40) | $0.05 |
| `gpt-oss-120b` | 100% (40 of 40) | $0.11 |

The baseline fails on paraphrase: it misses 4 of 8 positive and 4 of 7 negative
replies, the two classes that matter most. Two
caveats about the 100%. The set was written by the same person as the prompt, so
it is likely easier than real replies. And 40 of 40 still leaves the true error
rate anywhere up to about 7%. A referral address must literally appear in the reply
(checked in code); the model returned the right address in all 4 replies that had
one, and null in the 2 that named a person without an address.

The design's classifier also returned a self-reported confidence. It is gone: on
an earlier project the model reported 1.00 on 36 of 40 answers, wrong ones
included.

### Where the money goes (seed 42, target 3)

| step | leads in | leads out | cost |
|---|---|---|---|
| enrichment | 250 companies | 721 contacts, 578 verified | $36.98 |
| scoring (rules) | 578 verified | 375 eligible, 203 below 3 | $0 |
| signals (funding, hiring, tools, news) | 166 companies | | $49.80 |
| theory assignment | 375 | 203 with a theory, 172 no theory fits | $0 |
| email | 203 | | $0.08 at the measured rate |

$0.43 per lead that reaches an email, and the email itself is 0.1% of that.
**Signals cost more than finding the people**, and they are bought for every
company with an eligible lead, although 172 of 375 leads then fit no theory.
Fetching only the signal the cheapest fitting theory needs, one at a time, is the
obvious next saving.

### Design bugs fixed in this stage

| in the design | here |
|---|---|
| scoring passed score 3, variable enrichment only took score 4, so every score-3 lead got a theory and waited forever | one threshold; a test takes a score-3 lead to `variables_ready` |
| a theory was assigned before its variables were fetched; a company without a funding round left the lead stuck | a theory is assigned only if the company has every variable it needs, otherwise `cold_reserve` with the reason |
| copy generation set status `generating_copy`, which the schema does not have | `drafting` |
| the reply classifier returns `referral`, which `events.reply_class` rejected | allowed; tested |
| theory assignment updated leads without a lock | `SKIP LOCKED` like every other step |
| signals were fetched per lead and theory | once per company; a test checks 3 leads cost 1 lookup |

Runs that die mid-step leave leads in `scoring` or `drafting`; the next run releases
claims older than 15 minutes and counts that as an attempt.

## Stage 2: enrichment cascade

Four data providers, cheapest first: Apollo, Hunter, Snov, PhantomBuster. Every
contact found is checked with Findymail before it counts. A more expensive provider
is called only while the cheaper ones have not produced `--target` verified
contacts for the company.

### Measured on the seed 42 world (250 companies, 882 people)

`python -m leadengine enrich --target N`, prices from the original design's cost
table (`src/leadengine/providers/clients.py`, one place). Runs are reproducible:
two runs give identical numbers.

| stopping rule | companies with a verified contact | verified leads | cost | per 1000 verified leads |
|---|---|---|---|---|
| `--target 1` (the design: stop at the first hit) | 235 of 250 | 426 | $17.30 | $40.62 |
| `--target 3` (default) | 235 of 250 | 578 | $36.98 | $63.98 |
| `--target 99` (call every provider) | 235 of 250 | 656 | $64.29 | $98.00 |

What the table says:

- **The design's rule already reaches every company that can be reached.** All
  three rules find a verified contact at the same 235 companies (the other 15 have
  nobody who verifies). The extra money buys depth, not coverage: 1.8 verified
  people per company at target 1, 2.5 at target 3.
- **The marginal lead gets expensive fast.** Going from target 1 to 3 adds 152
  verified leads for $19.68, $0.13 each. Going from 3 to "everything" adds 78 for
  $27.31, $0.35 each.
- **PhantomBuster is 46% of the bill for 12% of the contacts** at target 3: $17.00 of
  $36.98, 85 of 721 contacts kept. It charges per lead returned and returns everyone
  it knows at the company, including the people the cheaper providers already
  found: it returned 340 people, and 255 of them were duplicates that were paid for
  and thrown away. Apollo, for comparison, cost $7.33 for 458 contacts.
- **Verification is the second largest line**, $8.65 for 721 addresses. It is what
  keeps 93 risky (catch-all) and 50 invalid addresses out of sending: they are
  stored, but `leads_sendable` only returns verified ones.

### Same run with faults injected

`--rate-429 0.10 --rate-5xx 0.03 --phantom-empty 0.10`: one call in ten is rate
limited, three in a hundred fail with 503, and one PhantomBuster run in ten
finishes "successfully" with no data, which is what happens when its LinkedIn
session cookie expires.

| | clean | with faults |
|---|---|---|
| companies failed | 0 | 0 (one retried and succeeded on the next cycle) |
| calls | 2519 | 2895 |
| verified leads | 578 | 573 |
| companies with a verified contact | 235 | 234 |

429s and 503s cost only calls: retries absorbed all of them and no data was lost.
The 5 missing leads and the 1 lost company all come from the 14 silent empty
PhantomBuster runs. No error was raised for them; the only trace is the
`phantom empty runs` counter. That is the failure worth alerting on.

### How it is built

- **Mock providers** (`providers/mock.py`) answer from the synthetic world with the
  real APIs' awkward parts: Apollo search masks emails and each reveal is a separate
  billed `people/match`; Hunter pages by offset and returns role mailboxes with no
  name; Snov needs an OAuth token that expires in an hour and pages by `lastId`;
  PhantomBuster is launch, poll, fetch. Fault draws are seeded by the request
  itself, not by a shared sequence, so results do not depend on the order companies
  are processed in.
- **Retries** (`providers/http.py`) only for transport: 429 honours `Retry-After`
  (capped at 30 s), 5xx and network errors back off exponentially, at most 4
  attempts. Any other 4xx is raised at once. Failed attempts are logged but not
  billed.
- **Every loop has a bound that does not trust the API**: at most 10 pages per
  provider, a Snov cursor that does not advance stops the loop, PhantomBuster is
  polled at most 20 times, and one company cannot make more than 2000 calls.
- **Cost ledger** (`provider_calls`): one row per HTTP call, including failed ones.
  Per-person costs (reveal, per-lead fee, verification) are also on the lead;
  per-domain searches belong to the company and are only in the ledger.
- **Claims and failures**: a run claims a batch with `SKIP LOCKED` and commits the
  claim before any HTTP call. Everything found for a company is written in one
  transaction with its status and its ledger. A provider outage sends the company
  back to the queue, three failures move it to `dead_letter` once. A run that
  crashed leaves its companies claimed; the next run releases claims older than 15
  minutes and counts that as an attempt, so a company that crashes the process
  every time cannot loop forever.

### Tested (37 tests, mock over `httpx.MockTransport`, real Postgres)

Among them: two runs in parallel threads over 40 companies never call a provider
twice for the same company, and both runs really did work; a cascade with 30% of
calls rate limited stores exactly the same leads as a clean one; the expensive
provider is never called for a company the cheap ones cover; a person found by two
providers is stored and paid for once.

## Stage 1: schema and synthetic data

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
python -m leadengine enrich --target 3            # add --rate-429 0.1 etc. to inject faults
python -m leadengine prepare --copy-limit 30 --dump   # needs GROQ_API_KEY
python -m leadengine eval-replies                  # 40 labelled replies against the LLM
python -m leadengine eval-scoring --n 60           # LLM score vs the rubric
```

Live runs on this machine go through `.\scripts\run_live_capped.ps1 -WithDb -Command '...'`:
the same memory cap as the tests, a throwaway Postgres, and the Groq key passed
through the environment so it never appears on a command line.

`enrich` runs the whole queue against the in-process mock providers. Retry and
polling waits are added up instead of slept, and reported as simulated time.

Tests run only through the capped runner: the test container and a throwaway
Postgres both get a kernel-enforced memory limit, on a private Docker network, and
are removed afterwards.

```powershell
.\scripts\run_tests_capped.ps1
```

Current result: `125 passed`.

If an antivirus on your machine intercepts HTTPS (pip in the image fails with
`CERTIFICATE_VERIFY_FAILED`), run `.\scripts\export_local_ca.ps1` once. It exports
that root certificate to the git-ignored `.certs/`, and the build passes it to
`pip install` as a BuildKit secret. Verification stays on and the certificate is
not stored in the image.

## Roadmap

1. **Schema, migrations, synthetic data** (done)
2. **Mock providers with real response shapes, 429s and pagination; the enrichment cascade** (done)
3. **Scoring, theories and copy through an LLM with schema-validated output, fallbacks and evals** (done)
4. Sending simulator, HMAC-signed event webhook with idempotency, Wilson-bound kill switch, dead letter
5. Dashboard (Next.js) and deployment: Vercel, Neon, scheduled cycles in GitHub Actions
6. n8n workflows orchestrating the same steps, demo recording
