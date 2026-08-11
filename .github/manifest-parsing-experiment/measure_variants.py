#!/usr/bin/env python3
import csv
import itertools
import json
import os
import pathlib
import random
import statistics
import subprocess
import sys
import time

# VARIANTS_JSON: {name: {"path": "/.../cargo", "env": {...}}}
# COMMAND_JSON: ["metadata", ...]
# argv: cwd output_json rounds
cwd = pathlib.Path(sys.argv[1])
out_json = pathlib.Path(sys.argv[2])
rounds = int(sys.argv[3])
variants = json.loads(os.environ["VARIANTS_JSON"])
command = json.loads(os.environ["COMMAND_JSON"])
names = list(variants)

# Balanced rotations in both directions to spread position and drift.
orders = []
for seq in (names, list(reversed(names))):
    for i in range(len(seq)):
        orders.append(tuple(seq[i:] + seq[:i]))

rows = []
base_env = os.environ.copy()
for r in range(rounds):
    order = orders[r % len(orders)]
    for pos, name in enumerate(order):
        spec = variants[name]
        env = base_env.copy()
        env.update(spec.get("env", {}))
        start = time.perf_counter_ns()
        p = subprocess.run(
            [spec["path"], *command], cwd=cwd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        ms = (time.perf_counter_ns() - start) / 1_000_000
        if p.returncode:
            raise SystemExit(f"{name} failed: {p.stderr[-6000:]}")
        rows.append({"round": r, "position": pos, "variant": name, "elapsed_ms": ms})
        print(f"round={r:02d} pos={pos} variant={name} ms={ms:.3f}", flush=True)

out_json.parent.mkdir(parents=True, exist_ok=True)
csv_path = out_json.with_suffix(".csv")
with csv_path.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader(); w.writerows(rows)

def percentile(values, q):
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(pos); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)

summary = {"rounds": rounds, "variants": {}}
for name in names:
    xs = [r["elapsed_ms"] for r in rows if r["variant"] == name]
    summary["variants"][name] = {
        "n": len(xs), "mean_ms": statistics.mean(xs),
        "median_ms": statistics.median(xs),
        "stdev_ms": statistics.stdev(xs) if len(xs) > 1 else 0.0,
        "p05_ms": percentile(xs, .05), "p95_ms": percentile(xs, .95),
        "min_ms": min(xs), "max_ms": max(xs),
    }

per_round = {}
for row in rows:
    per_round.setdefault(row["round"], {})[row["variant"]] = row["elapsed_ms"]

baseline = names[0]
random.seed(14395)
summary["comparisons_vs_first"] = {}
for name in names[1:]:
    diffs = [v[baseline] - v[name] for v in per_round.values()]
    ratios = [v[baseline] / v[name] for v in per_round.values()]
    boots = []
    for _ in range(10000):
        boots.append(statistics.mean(diffs[random.randrange(len(diffs))] for _ in diffs))
    summary["comparisons_vs_first"][name] = {
        "baseline": baseline,
        "paired_mean_baseline_minus_variant_ms": statistics.mean(diffs),
        "paired_median_baseline_minus_variant_ms": statistics.median(diffs),
        "paired_mean_95ci_ms": [percentile(boots, .025), percentile(boots, .975)],
        "median_ratio_baseline_over_variant": statistics.median(ratios),
        "wins_variant_faster": sum(d > 0 for d in diffs),
        "pairs": len(diffs),
    }

out_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps(summary, indent=2, sort_keys=True))
