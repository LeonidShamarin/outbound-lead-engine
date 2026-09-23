"""CLI: python -m leadengine {migrate|generate|seed|enrich|prepare|eval-replies|eval-scoring}"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from leadengine import db
from leadengine.seed.load import load_intake
from leadengine.seed.world import build_world


def _enrich(args: argparse.Namespace) -> None:
    from leadengine.enrich import mock_clients, run_until_done
    from leadengine.providers.mock import Faults, MockProviders

    world = json.loads(args.world.read_text(encoding="utf-8"))
    faults = Faults(rate_429=args.rate_429, rate_5xx=args.rate_5xx, phantom_silent_empty=args.phantom_empty)
    mock = MockProviders(world, faults, seed=args.fault_seed)
    # Mock mode: retry and polling waits are added up instead of slept, so a
    # measurement run takes seconds. The total is reported as simulated time.
    waited: list[float] = []
    clients = mock_clients(mock, sleep=waited.append)

    with db.connect() as conn:
        s = run_until_done(conn, clients, batch=args.batch, target=args.target, min_confidence=args.min_confidence)

    per_1000 = s.cost_usd / s.verify["verified"] * 1000 if s.verify["verified"] else 0.0
    print(f"companies {s.enriched + s.excluded + s.failed}: enriched {s.enriched}, excluded {s.excluded}, "
          f"failed {s.failed} (provider errors retried {s.retry} times)")
    print(f"leads {s.leads}: verified {s.verify['verified']}, risky {s.verify['risky']}, "
          f"invalid {s.verify['invalid']}")
    print(f"skipped: {dict(s.skipped)}")
    print("provider        called for  contacts kept")
    for p in ("apollo", "hunter", "snov", "phantombuster"):
        print(f"  {p:<14} {s.called[p]:>10}  {s.kept_by[p]:>13}")
    print(f"calls {s.calls}, cost ${s.cost_usd:.2f}, ${per_1000:.2f} per 1000 verified leads, "
          f"phantom empty runs {clients.phantom_empty_runs}, simulated waiting {sum(waited):.0f} s")


def _groq(args: argparse.Namespace):
    import os

    from leadengine.llm import GroqClient, groq_key_from_file

    key = os.environ.get("GROQ_API_KEY") or (groq_key_from_file(args.key_file) if args.key_file else None)
    if not key:
        raise SystemExit("set GROQ_API_KEY or pass --key-file")
    return GroqClient(key)


def _prepare(args: argparse.Namespace) -> None:
    from leadengine.drafting import prepare
    from leadengine.seed.theories import load_theories

    llm = _groq(args) if (args.scoring == "llm" or args.copy_limit > 0) else None
    with db.connect() as conn:
        load_theories(conn)
        s = prepare(conn, llm, scoring=args.scoring, score_model=args.score_model, copy_model=args.copy_model,
                    copy_limit=args.copy_limit)
        cost = conn.execute("SELECT step, count(*), sum(cost_usd)::float FROM llm_calls GROUP BY step").fetchall()
        signal_cost = conn.execute("SELECT coalesce(sum(cost_usd), 0)::float FROM company_signals").fetchone()[0]
    print(f"scoring: {s.score}")
    print(f"signals fetched for {s.signals} companies (${signal_cost:.2f}); theories: {s.assign}")
    print(f"copy: {s.copy}")
    for step, n, usd in cost:
        print(f"llm {step}: {n} requests, ${usd:.4f}")
    if args.dump and args.copy_limit > 0:
        from leadengine.evaluate import dump_copy

        with db.connect() as conn:
            print(json.dumps(dump_copy(conn, args.copy_model), indent=1))


def _eval(args: argparse.Namespace) -> None:
    from leadengine.evaluate import eval_replies, eval_scoring

    llm = _groq(args)
    if args.command == "eval-replies":
        s = eval_replies(llm, args.model, pause_s=args.pause)
    else:
        world = json.loads(args.world.read_text(encoding="utf-8"))
        s = eval_scoring(llm, args.model, world, n=args.n, pause_s=args.pause)
    print(json.dumps(s, indent=1, ensure_ascii=False))


def _killswitch_mc(args: argparse.Namespace) -> None:
    from leadengine.evaluate import _save
    from leadengine.killswitch import DESIGN_RULE, UPPER_RULE, simulate_rule

    rows = [simulate_rule(rule, rate, runs=args.runs, daily=args.daily, max_sent=args.max_sent)
            for rule in (DESIGN_RULE, UPPER_RULE) for rate in (0.0025, 0.005, 0.01, 0.02, 0.03)]
    for r in rows:
        print(f"{r['rule']:<42} true {r['true_rate']:.2%}  paused {r['paused_share']:>6.1%}  "
              f"median sends to pause {r['median_sends_to_pause']}")
    _save("killswitch-montecarlo.json", {"daily": args.daily, "max_sent": args.max_sent, "rows": rows})


def _simulate(args: argparse.Namespace) -> None:
    import secrets as pysecrets

    from leadengine.campaign import drain_copy, keyword_classifier, llm_classifier, run_simulation
    from leadengine.copywriter import TemplateWriter
    from leadengine.evaluate import _save
    from leadengine.killswitch import DESIGN_RULE, UPPER_RULE
    from leadengine.llm import LLMCall
    from leadengine.simulator import Simulator
    from leadengine.theorygen import propose

    log: list[LLMCall] = []
    llm = _groq(args) if (args.classifier == "llm" or args.propose) else None
    classify = llm_classifier(llm, args.reply_model, log) if args.classifier == "llm" else keyword_classifier()
    rule = DESIGN_RULE if args.rule == "design" else UPPER_RULE
    sim = Simulator(secret=pysecrets.token_hex(16), seed=args.seed)
    with db.connect() as conn:
        drafted = drain_copy(conn, TemplateWriter(), "template")
        res = run_simulation(conn, sim, classify, days=args.days, writer=TemplateWriter(), rule=rule,
                             mailboxes=args.mailboxes, daily_limit=args.daily_limit)
        theories = conn.execute(
            """SELECT s.name, s.status, s.sent, s.replied, s.positive, s.bounced, s.unsubscribed
                 FROM theory_stats s ORDER BY s.name""").fetchall()
        got = dict(conn.execute("SELECT external_id, reply_class FROM events WHERE type = 'reply'").fetchall())
        how = dict(conn.execute("SELECT payload->>'classifier', count(*) FROM events WHERE type = 'reply' "
                                "GROUP BY 1").fetchall())
        rejected = conn.execute("SELECT count(*) FROM webhook_rejections").fetchone()[0]
        proposals = propose(conn, llm, args.theory_model, log) if args.propose else []
    from leadengine.killswitch import wilson

    intended = sim.intended_class
    agree = sum(got.get(k) == v for k, v in intended.items())
    wrong = [{"intended": v, "got": got.get(k)} for k, v in intended.items() if got.get(k) != v]
    table = []
    for name, status, sent, replied, positive, bounced, unsub in theories:
        lo, hi = wilson(positive, sent)
        table.append({"theory": name, "status": status, "true_positive_rate": sim.true_rates.get(name),
                      "sent": sent, "replied": replied, "positive": positive, "bounced": bounced,
                      "unsubscribed": unsub, "observed_rate": round(positive / sent, 4) if sent else None,
                      "wilson": [round(lo, 4), round(hi, 4)], "paused_on_day": res.paused_on_day.get(name)})
    summary = {
        "rule": rule.name, "days": args.days, "drafted_before": drafted,
        "sent_total": sum(d.queued for d in res.days), "webhooks": sum(d.events for d in res.days),
        "webhook_rejections": rejected, "duplicates": sum(d.duplicates for d in res.days),
        "theories": table,
        "classifier": {"mode": args.classifier, "replies": len(intended), "agree_with_simulator": agree,
                       "accuracy": round(agree / max(len(intended), 1), 3), "how": how, "mistakes": wrong[:20],
                       "llm_cost_usd": round(sum(c.cost_usd for c in log if c.step == "reply"), 6)},
        "proposed_theories": proposals,
        "theory_llm_cost_usd": round(sum(c.cost_usd for c in log if c.step == "theory"), 6),
    }
    print(json.dumps(summary, indent=1))
    _save(f"simulation-{args.rule}.json", {"summary": summary, "days": [d.__dict__ for d in res.days]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadengine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending SQL migrations")

    gen = sub.add_parser("generate", help="write a synthetic world to JSON")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--companies", type=int, default=250)
    gen.add_argument("--out", type=Path, default=Path("data/world.json"))

    seed = sub.add_parser("seed", help="load the intake list from a world file")
    seed.add_argument("--world", type=Path, default=Path("data/world.json"))

    enr = sub.add_parser("enrich", help="run the enrichment cascade against the mock providers until the queue is empty")
    enr.add_argument("--world", type=Path, default=Path("data/world.json"))
    enr.add_argument("--batch", type=int, default=25)
    enr.add_argument("--target", type=int, default=3, help="verified contacts per company before stopping")
    enr.add_argument("--min-confidence", type=int, default=80)
    enr.add_argument("--rate-429", type=float, default=0.0)
    enr.add_argument("--rate-5xx", type=float, default=0.0)
    enr.add_argument("--phantom-empty", type=float, default=0.0, help="share of PhantomBuster runs that finish empty")
    enr.add_argument("--fault-seed", type=int, default=1)

    prep = sub.add_parser("prepare", help="enriched leads -> scored -> theory -> copy_ready")
    prep.add_argument("--scoring", choices=("rules", "llm"), default="rules")
    prep.add_argument("--score-model", default="openai/gpt-oss-20b")
    prep.add_argument("--copy-model", default="openai/gpt-oss-120b")
    prep.add_argument("--copy-limit", type=int, default=0, help="how many emails to write (0 = stop before copy)")
    prep.add_argument("--key-file")
    prep.add_argument("--dump", action="store_true", help="write every email and generation stats to eval/results/")

    for name, default_model in (("eval-replies", "openai/gpt-oss-20b"), ("eval-scoring", "openai/gpt-oss-20b")):
        ev = sub.add_parser(name, help="run an eval against the real LLM and save results to eval/results/")
        ev.add_argument("--model", default=default_model)
        ev.add_argument("--key-file")
        ev.add_argument("--pause", type=float, default=0.0, help="seconds between requests (free-tier rate limits)")
        if name == "eval-scoring":
            ev.add_argument("--world", type=Path, default=Path("data/world.json"))
            ev.add_argument("--n", type=int, default=60)

    mc = sub.add_parser("killswitch-mc", help="Monte Carlo: how often each pause rule stops a theory")
    mc.add_argument("--runs", type=int, default=2000)
    mc.add_argument("--daily", type=int, default=50)
    mc.add_argument("--max-sent", type=int, default=2000)

    sm = sub.add_parser("simulate", help="send copy-ready leads to the simulator for N days")
    sm.add_argument("--days", type=int, default=30)
    sm.add_argument("--rule", choices=("upper", "design"), default="upper")
    sm.add_argument("--classifier", choices=("llm", "keywords"), default="keywords")
    sm.add_argument("--reply-model", default="openai/gpt-oss-20b")
    sm.add_argument("--theory-model", default="openai/gpt-oss-120b")
    sm.add_argument("--propose", action="store_true", help="ask the LLM for 3 new draft theories at the end")
    sm.add_argument("--mailboxes", type=int, default=5)
    sm.add_argument("--daily-limit", type=int, default=30)
    sm.add_argument("--seed", type=int, default=0)
    sm.add_argument("--key-file")

    args = parser.parse_args(argv)

    if args.command == "migrate":
        with db.connect() as conn:
            applied = db.migrate(conn)
        print(f"applied {len(applied)}: {', '.join(applied) or 'nothing pending'}")
    elif args.command == "generate":
        world = build_world(seed=args.seed, n_companies=args.companies)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(world, indent=1), encoding="utf-8")
        print(f"{len(world['companies'])} companies, {len(world['people'])} people, "
              f"{len(world['intake'])} intake rows -> {args.out}")
    elif args.command == "seed":
        world = json.loads(args.world.read_text(encoding="utf-8"))
        with db.connect() as conn:
            r = load_intake(conn, world)
        print(f"intake rows {r.rows}: inserted {r.inserted}, duplicates {r.duplicates}, rejected {r.rejected}")
    elif args.command == "enrich":
        _enrich(args)
    elif args.command == "prepare":
        _prepare(args)
    elif args.command in ("eval-replies", "eval-scoring"):
        _eval(args)
    elif args.command == "killswitch-mc":
        _killswitch_mc(args)
    elif args.command == "simulate":
        _simulate(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
