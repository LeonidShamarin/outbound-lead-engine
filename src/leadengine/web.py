"""The deployed app: a read-only dashboard over the v_* views, and the event webhook.

One ASGI app on purpose. The webhook reuses `webhook.handle`, the same function
the simulator's events go through in every test, and the dashboard reads the same
views the README numbers come from. Everything shown is synthetic.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from leadengine.webhook import MAX_BODY_BYTES, handle

app = FastAPI(title="Outbound Lead Engine", docs_url=None, redoc_url=None)


@contextmanager
def _db() -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=10) as conn:
        yield conn


def _classifier():
    from leadengine.campaign import keyword_classifier, llm_classifier
    from leadengine.llm import GroqClient

    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return keyword_classifier()
    return llm_classifier(GroqClient(key), os.environ.get("REPLY_MODEL", "openai/gpt-oss-20b"), [])


@app.get("/health")
def health() -> JSONResponse:
    try:
        with _db() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"ok": True, "db": True})
    except Exception as e:  # health must answer even when the DB does not
        return JSONResponse({"ok": False, "db": False, "error": type(e).__name__}, status_code=503)


@app.post("/api/events")
async def events(request: Request) -> JSONResponse:
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_BODY_BYTES:
        return JSONResponse({"error": "body too large"}, status_code=413)
    body = await request.body()
    with _db() as conn:
        status, out = handle(conn, os.environ.get("WEBHOOK_SECRET", ""), request.headers.get("x-signature"),
                             body, _classifier())
    return JSONResponse(out, status_code=status)


QUERIES = {
    "funnel": "SELECT * FROM v_funnel",
    "theories": """SELECT name, status, sent, replied, positive, positive_pct,
                          round(100 * wilson_lower, 2) AS "95% low, %", round(100 * wilson_upper, 2) AS "95% high, %"
                     FROM v_theories ORDER BY status, positive_pct DESC NULLS LAST, name""",
    "daily": "SELECT * FROM v_daily ORDER BY day DESC LIMIT 14",
    "costs": "SELECT item, round(usd, 4) AS usd, calls FROM v_costs ORDER BY usd DESC NULLS LAST",
    "feed": """SELECT occurred_at::date AS day, first_name, title, company, theory, variant, reply_text
                 FROM v_positive_feed ORDER BY occurred_at DESC LIMIT 15""",
    "replies": "SELECT * FROM v_reply_classes ORDER BY replies DESC",
    "mailboxes": "SELECT * FROM v_mailboxes ORDER BY email",
    "errors": "SELECT source, n, last_at::date AS last FROM v_errors ORDER BY n DESC",
    "drafts": "SELECT name, hypothesis, required_variables FROM theories WHERE status = 'draft' ORDER BY created_at DESC LIMIT 6",
}


def _rows(conn: psycopg.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    cur = conn.execute(sql)
    return [d.name for d in cur.description], cur.fetchall()


@app.get("/api/summary")
def summary() -> JSONResponse:
    with _db() as conn:
        data = {}
        for key, sql in QUERIES.items():
            cols, rows = _rows(conn, sql)
            data[key] = [{c: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for c, v in zip(cols, r)}
                         for r in rows]
    return JSONResponse(data)


def _table(cols: list[str], rows: list[tuple], empty: str = "nothing yet") -> str:
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape('' if v is None else str(v))}</td>" for v in r) + "</tr>"
                   for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    with _db() as conn:
        parts = {k: _rows(conn, q) for k, q in QUERIES.items()}
    fcols, frows = parts["funnel"]
    funnel = dict(zip(fcols, frows[0])) if frows else {}
    tiles = "".join(
        f'<div class="tile"><div class="n">{funnel.get(k, 0):,}</div><div class="l">{label}</div></div>'
        for k, label in (("companies", "companies"), ("verified", "verified contacts"), ("eligible", "scored 3+"),
                         ("with_theory", "with a theory"), ("sent", "emails sent"), ("replied", "replied"),
                         ("positive", "positive")))
    sections = [
        ("Theories", "95% Wilson interval of the positive reply rate. A theory is paused when the high end "
                     "drops below 1%.", "theories"),
        ("Last 14 days", "By event day.", "daily"),
        ("Positive replies", "Classified by the LLM when a Groq key is configured, by keyword rules otherwise; "
                             "a failed LLM call falls back to the rules and is counted below.", "feed"),
        ("Where the money went", "Mock provider prices from the design's cost table; LLM at Groq list prices.", "costs"),
        ("Reply classes", "", "replies"),
        ("Mailboxes", "", "mailboxes"),
        ("Errors", "Dead letters, rejected webhooks, rejected LLM answers.", "errors"),
        ("Draft theories", "Proposed by the LLM from segment reply rates; a human switches them on.", "drafts"),
    ]
    body = "".join(f"<section><h2>{t}</h2><p class='muted'>{html.escape(d)}</p>{_table(*parts[k])}</section>"
                   for t, d, k in sections)
    return HTMLResponse(PAGE.format(tiles=tiles, body=body))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outbound Lead Engine</title>
<meta name="description" content="Outbound lead pipeline on synthetic data: enrichment cascade, LLM emails, signed webhooks, kill switch.">
<style>
:root{{--bg:#f7f7f5;--fg:#1b1b1a;--muted:#6b6b66;--card:#fff;--line:#e4e4df;--accent:#2f6f4f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#ecece8;--muted:#9a9a93;--card:#1d1d1b;--line:#2e2e2b;--accent:#7cc39c}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:24px;margin:0 0 4px}}
h2{{font-size:17px;margin:32px 0 2px}}.muted{{color:var(--muted);margin:0 0 10px;font-size:13px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:20px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.tile .n{{font-size:22px;font-weight:600;color:var(--accent)}}.tile .l{{color:var(--muted);font-size:13px}}
.scroll{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:500;white-space:nowrap}}tr:last-child td{{border-bottom:0}}a{{color:var(--accent)}}
</style></head><body><main>
<h1>Outbound Lead Engine</h1>
<p class="muted">Synthetic data only: fictional companies on <code>.example</code> domains, mock data providers,
a simulated sender. <a href="https://github.com/LeonidShamarin/outbound-lead-engine">Source and measured results</a>.</p>
<div class="tiles">{tiles}</div>{body}
</main></body></html>"""
