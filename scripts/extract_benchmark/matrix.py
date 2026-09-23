"""Sequential alternating cold/warm comparison; no parallel variants or production."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run import ABLATIONS, run

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--reliability", required=True)
    parser.add_argument("--efficiency")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--requests", type=int, default=96)
    parser.add_argument("--ablations", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    variants = [("baseline", args.baseline, None), ("A", args.reliability, None)]
    if args.efficiency:
        variants.append(("B", args.efficiency, None))
    if args.ablations:
        if not args.efficiency:
            raise ValueError("Ablations require a B image")
        variants += [("B-no-" + key, args.efficiency, key) for key in ABLATIONS]
    plan = []
    for trial in range(args.rounds):
        rotated = variants[trial % len(variants) :] + variants[: trial % len(variants)]
        for mode in ("cold", "warm"):
            for name, image, disable in rotated:
                plan.append(
                    {
                        "name": f"round-{trial + 1}-{mode}-{name}",
                        "variant": name,
                        "image": image,
                        "disable": disable,
                        "mode": mode,
                        "seed": 42 + trial,
                        "concurrency": 1,
                    }
                )
    if args.efficiency:
        for trial in range(args.rounds):
            for concurrency in (2, 4, 8, 16):
                for mode in ("cold", "duplicate"):
                    for name, image, disable in variants:
                        plan.append(
                            {
                                "name": f"burst-{trial + 1}-{mode}-{concurrency}-{name}",
                                "variant": name,
                                "image": image,
                                "disable": disable,
                                "mode": mode,
                                "seed": 42 + trial,
                                "concurrency": concurrency,
                            }
                        )
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2))
    for item in plan:
        print(json.dumps({"starting": item["name"]}), flush=True)
        run(
            item["image"],
            args.output / item["name"],
            0,
            args.requests if item["concurrency"] == 1 else 48,
            item["seed"],
            item["mode"],
            item["concurrency"],
            item["disable"],
            "1.1",
        )
        print(json.dumps({"completed": item["name"]}), flush=True)
