"""CLI: python -m leadengine {migrate|generate|seed|enrich}"""

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
