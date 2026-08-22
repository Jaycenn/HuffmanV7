#!/usr/bin/env python3
"""Paired file-level analysis for AFC experiment summaries.

Repeated timing trials are first reduced to one median per file/configuration.
This script then compares configurations across the same files, avoiding the
pseudoreplication caused by treating repeated runs as independent datasets.
"""

import argparse
import csv
import itertools
import json
import math
import statistics


def rank_absolute(values):
    order = sorted(range(len(values)), key=lambda i: abs(values[i]))
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and abs(values[order[end]]) == abs(values[order[pos]]):
            end += 1
        rank = ((pos + 1) + end) / 2.0
        for index in order[pos:end]:
            ranks[index] = rank
        pos = end
    return ranks


def exact_two_sided_p(ranks, observed):
    # Multiply ranks by two so tied half-ranks remain integral.
    scaled = [int(round(rank * 2)) for rank in ranks]
    observed = int(round(observed * 2))
    counts = {0: 1}
    for rank in scaled:
        nxt = dict(counts)
        for total, count in counts.items():
            nxt[total + rank] = nxt.get(total + rank, 0) + count
        counts = nxt
    full = sum(scaled)
    tail = min(observed, full - observed)
    extreme = sum(count for total, count in counts.items()
                  if total <= tail or total >= full - tail)
    return min(1.0, extreme / (2 ** len(scaled)))


def wilcoxon(differences):
    nonzero = [value for value in differences if value != 0]
    if not nonzero:
        return {"n": 0, "w_plus": 0.0, "p": 1.0,
                "rank_biserial": 0.0, "method": "all ties"}
    ranks = rank_absolute(nonzero)
    w_plus = sum(rank for rank, value in zip(ranks, nonzero) if value > 0)
    w_minus = sum(rank for rank, value in zip(ranks, nonzero) if value < 0)
    total = w_plus + w_minus
    effect = (w_plus - w_minus) / total if total else 0.0
    if len(nonzero) <= 25:
        p = exact_two_sided_p(ranks, w_plus)
        method = "exact signed-rank distribution"
    else:
        mean = total / 2.0
        variance = sum(rank * rank for rank in ranks) / 4.0
        correction = 0.5 if w_plus > mean else (-0.5 if w_plus < mean else 0.0)
        z = (w_plus - mean - correction) / math.sqrt(variance)
        p = math.erfc(abs(z) / math.sqrt(2.0))
        method = "normal approximation with continuity correction"
    return {"n": len(nonzero), "w_plus": w_plus, "w_minus": w_minus,
            "p": p, "rank_biserial": effect, "method": method}


def holm(results):
    ordered = sorted(enumerate(results), key=lambda item: item[1]["p"])
    running = 0.0
    total = len(results)
    for position, (index, row) in enumerate(ordered):
        adjusted = min(1.0, (total - position) * row["p"])
        running = max(running, adjusted)
        results[index]["p_holm"] = running


def load_rows(path, metric, backend):
    values = {}
    with open(path, newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("status") != "ok" or row.get("backend") != backend:
                continue
            key = (row["file"], row["preset"])
            values[key] = float(row[metric])
    return values


def analyze(path, metric, backend):
    values = load_rows(path, metric, backend)
    files = sorted({file for file, _ in values})
    groups = sorted({group for _, group in values})
    results = []
    for left, right in itertools.combinations(groups, 2):
        paired = [(values[(file, left)], values[(file, right)])
                  for file in files
                  if (file, left) in values and (file, right) in values]
        differences = [a - b for a, b in paired]
        result = wilcoxon(differences)
        result.update({
            "left": left, "right": right, "metric": metric,
            "backend": backend, "paired_files": len(paired),
            "median_left": statistics.median(a for a, _ in paired) if paired else None,
            "median_right": statistics.median(b for _, b in paired) if paired else None,
            "median_difference_left_minus_right":
                statistics.median(differences) if differences else None,
        })
        results.append(result)
    holm(results)
    return {"unit_of_analysis": "file", "metric": metric,
            "backend": backend, "groups": groups, "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("summary_csv")
    parser.add_argument("--metric", default="compressed_bytes")
    parser.add_argument("--backend", default="native")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()
    result = analyze(args.summary_csv, args.metric, args.backend)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
