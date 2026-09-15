"""CLI: python -m leadengine {migrate|generate|seed}"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from leadengine import db
from leadengine.seed.load import load_intake
from leadengine.seed.world import build_world


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
