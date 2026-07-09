"""Count papers by has_pdf / has_latex flag combinations via query_iterator."""

import time

from scholight.store.client import QUERY_CONSISTENCY, get_client


def count_with_filter(client, filter_expr: str, label: str) -> int:
    """Count ALL papers matching filter_expr using query_iterator, return count."""
    it = client.query_iterator(
        collection_name="arxiv_papers",
        filter=filter_expr,
        output_fields=["arxiv_id"],
        batch_size=10000,
        consistency_level=QUERY_CONSISTENCY,
    )
    total = 0
    while True:
        page = it.next()
        if not page:
            break
        total += len(page)
    it.close()
    return total


def main() -> None:
    client = get_client()

    # Test: check total count
    t0 = time.perf_counter()
    total = count_with_filter(client, "arxiv_id != ''", "total")
    total_time = time.perf_counter() - t0
    print(f"Total papers: {total} ({total_time:.1f}s)\n")

    queries = [
        ("has_pdf == true  && has_latex == true", "has_pdf == true and has_latex == true"),
        ("has_pdf == true  && has_latex == false", "has_pdf == true and has_latex == false"),
        ("has_pdf == false && has_latex == true", "has_pdf == false and has_latex == true"),
        ("has_pdf == false && has_latex == false", "has_pdf == false and has_latex == false"),
    ]

    results = {}
    for label, expr in queries:
        t0 = time.perf_counter()
        count = count_with_filter(client, expr, label)
        elapsed = time.perf_counter() - t0
        results[label] = (count, elapsed)

    # Print table
    print(f"{'类别':<45} {'论文数':>8} {'占比':>8} {'耗时':>8}")
    print("-" * 72)
    for label, (count, elapsed) in results.items():
        pct = (count / total * 100) if total else 0
        print(f"{label:<45} {count:>8} {pct:>7.1f}% {elapsed:>6.1f}s")
    print("-" * 72)
    total_counted = sum(c for c, _ in results.values())
    print(f"{'总计':<45} {total_counted:>8}")
    print()

    # Derived stats
    has_latex = (
        results["has_pdf == true  && has_latex == true"][0]
        + results["has_pdf == false && has_latex == true"][0]
    )
    has_pdf = (
        results["has_pdf == true  && has_latex == true"][0]
        + results["has_pdf == true  && has_latex == false"][0]
    )
    print(f"含 LaTeX: {has_latex} ({has_latex / total * 100:.1f}%)")
    print(f"含 PDF:   {has_pdf} ({has_pdf / total * 100:.1f}%)")


if __name__ == "__main__":
    main()
