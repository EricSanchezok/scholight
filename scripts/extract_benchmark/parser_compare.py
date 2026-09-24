"""Five paired full/fast rounds in native workers; no runtime activation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from corpus import Case, corpus
from quality_corpus import annotated_cases

from scholight.web_extract.process_family import enable_subreaping
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def tokens(text: str) -> Counter[str]:
    return Counter(re.findall(r"[\u3400-\u9fff]|[\w]+", text.casefold()))


def score(actual: str, gold: str) -> dict[str, float]:
    predicted, reference = tokens(actual), tokens(gold)
    overlap = sum((predicted & reference).values())
    return {
        "precision": overlap / max(1, predicted.total()),
        "recall": overlap / max(1, reference.total()),
    }


def parser_case(case: Case) -> tuple[bytes, bool]:
    if case.category != "javascript":
        return case.body, False
    # Exact authored hydration payload; real browser behavior is tested separately.
    script = case.body.decode().split(".innerHTML = ", 1)[1].split(";}, 40)", 1)[0]
    body = "<html><body><div id='root'>" + json.loads(script) + "</div></body></html>"
    return body.encode(), True


def comparison(records: list[dict]) -> dict:
    pairs: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    regressions = []
    for row in records:
        pairs[row["case"], row["round"]][row["mode"]] = row
        groups[row["category"]][row["mode"]].append(row)
    for (case, trial), pair in pairs.items():
        full, fast = pair["full"], pair["fast"]
        # A deliberately strict loss gate: retain every nonempty full-mode line.
        fast_text = " ".join(fast["content"].split())
        missing = [
            line
            for line in full["content"].splitlines()
            if line.strip() and " ".join(line.split()) not in fast_text
        ]
        if missing or (full["quality"] and not fast["quality"]):
            regressions.append({"case": case, "round": trial, "missing_lines": missing})
    categories = {}
    for category, modes in sorted(groups.items()):
        categories[category] = {}
        for mode, rows in modes.items():
            annotated = [row for row in rows if row["score"] is not None]
            categories[category][mode] = {
                "samples": len(rows),
                "failures": sum(not row["quality"] for row in rows),
                "cpu_ms_median": statistics.median(row["cpu_ms"] for row in rows),
                "precision": statistics.mean(row["score"]["precision"] for row in annotated)
                if annotated
                else None,
                "recall": statistics.mean(row["score"]["recall"] for row in annotated)
                if annotated
                else None,
            }
    cpu = {
        mode: sum(row["cpu_ms"] for row in records if row["mode"] == mode)
        for mode in ("full", "fast")
    }
    html_cpu = {
        mode: sum(
            row["cpu_ms"]
            for row in records
            if row["mode"] == mode and row["category"] not in {"pdf", "structured"}
        )
        for mode in ("full", "fast")
    }
    reduction = 1 - html_cpu["fast"] / max(html_cpu["full"], 1e-9)
    quality_gate = not regressions and all(
        row["quality"] for row in records if row["mode"] == "fast"
    )
    for modes in categories.values():
        full, fast = modes["full"], modes["fast"]
        quality_gate &= fast["failures"] <= full["failures"]
        for metric in ("precision", "recall"):
            if full[metric] is not None:
                quality_gate &= fast[metric] >= full[metric]
    return {
        "categories": categories,
        "cpu_ms_total": cpu,
        "html_cpu_ms_total": html_cpu,
        "cpu_reduction": reduction,
        "regressions": regressions,
        "quality_gate": bool(quality_gate),
        "cpu_gate": reduction >= 0.2,
        "candidate_pass": bool(quality_gate and reduction >= 0.2),
        "scope": "Authored parser fixtures; not a field-quality guarantee. Runtime remains full.",
    }


async def run(output: Path, rounds: int, seed: int, allow_host: bool) -> None:
    native = sys.platform == "linux" and platform.machine() in {"aarch64", "arm64"}
    native &= sys.version_info[:2] == (3, 11)
    if not native and not allow_host:
        raise RuntimeError("Acceptance measurements require Linux ARM64 / Python 3.11")
    output.mkdir(parents=True, exist_ok=False)
    annotated = annotated_cases()
    gold = {c.case.name: c.gold for c in annotated}
    cases = corpus() + [c.case for c in annotated]
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
                "native": native,
                "seed": seed,
                "rounds": rounds,
                "corpus": {c.name: hashlib.sha256(c.body).hexdigest() for c in cases},
            },
            indent=2,
        )
    )
    enable_subreaping()
    worker = WorkerSupervisor("parser")
    spool = Spool(output / "scratch")
    spool.start()
    records = []
    rng = random.Random(seed)  # nosec B311
    try:
        await worker.warmup()
        with (output / "records.jsonl").open("w") as log:
            for trial in range(rounds):
                order = list(cases)
                rng.shuffle(order)
                for index, case in enumerate(order):
                    body, rendered = parser_case(case)
                    modes = ("full", "fast") if (index + trial) % 2 else ("fast", "full")
                    for mode in modes:
                        with (
                            spool.allocate(len(body)) as source,
                            spool.allocate(50_000_000) as result,
                        ):
                            source.write(body)
                            url = "https://example.org/" + case.name
                            reply = await worker.call(
                                {
                                    "request": {
                                        "url": url,
                                        "render": "auto",
                                        "output": "main_markdown",
                                    },
                                    "body_path": str(source.path),
                                    "result_path": str(result.path),
                                    "result_limit": result.limit,
                                    "rendered": rendered,
                                    "fast_html": mode == "fast",
                                    "fetched": {
                                        "requested_url": url,
                                        "final_url": url,
                                        "status_code": 200,
                                        "content_type": case.mime,
                                        "charset": "utf-8",
                                    },
                                }
                            )
                            failure = reply.get("error")
                            parsed = json.loads(result.path.read_bytes()) if failure is None else {}
                            content = (parsed.get("extracted") or {}).get("content", "")
                            status = failure["status_code"] if failure else 200
                            row = {
                                "case": case.name,
                                "category": case.category,
                                "round": trial,
                                "mode": mode,
                                "status": status,
                                "error": failure,
                                "quality": status == case.status
                                and all(t in content for t in case.expected),
                                "cpu_ms": reply.get("cpu_ms", 0),
                                "content": content,
                                "score": score(content, gold[case.name])
                                if case.name in gold
                                else None,
                            }
                            records.append(row)
                            log.write(json.dumps(row, ensure_ascii=False) + "\n")
                            log.flush()
    finally:
        await worker.close()
        spool.close()
    report = comparison(records)
    report["acceptance_environment"] = native and rounds >= 5
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k not in {"categories", "regressions"}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--allow-host", action="store_true", help="Non-acceptance development smoke"
    )
    args = parser.parse_args()
    asyncio.run(run(args.output, args.rounds, args.seed, args.allow_host))
