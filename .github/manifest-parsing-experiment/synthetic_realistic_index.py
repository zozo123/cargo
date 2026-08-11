#!/usr/bin/env python3
import itertools
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1])
base = pathlib.Path(sys.argv[2])
index_bin = pathlib.Path(sys.argv[3])
oracle = pathlib.Path(sys.argv[4])
out = pathlib.Path(sys.argv[5])
root.mkdir(parents=True, exist_ok=True)

CASES = {
    1: [1],
    100: [1, 10],
    500: [1, 10],
    1000: [1, 10, 1000],
    2000: [1, 10],
}


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def manifest_text(i):
    return f'[package]\nname = "pkg{i:04d}"\nversion = "0.1.0"\nedition = "2021"\n'


def make_repo(n):
    repo = root / f"repo-{n}"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir()
    run(["git", "init", "-q", str(repo)])
    run(["git", "-C", str(repo), "config", "user.email", "bench@example.invalid"])
    run(["git", "-C", str(repo), "config", "user.name", "Cargo perf bench"])
    for i in range(n):
        d = repo / f"pkg{i:04d}"
        (d / "src").mkdir(parents=True)
        (d / "Cargo.toml").write_text(manifest_text(i))
        (d / "src/lib.rs").write_text(f"pub const ID: usize = {i};\n")
    run(["git", "-C", str(repo), "add", "."])
    run(["git", "-C", str(repo), "commit", "-qm", f"synthetic {n} packages"])
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, sha


def make_consumer(repo, sha, n, u):
    c = root / f"consumer-n{n}-u{u}"
    shutil.rmtree(c, ignore_errors=True)
    (c / "src").mkdir(parents=True)
    lines = [
        '[package]', 'name = "consumer"', 'version = "0.1.0"', 'edition = "2021"', '', '[dependencies]'
    ]
    url = repo.resolve().as_uri()
    for i in range(u):
        lines.append(f'pkg{i:04d} = {{ git = "{url}", rev = "{sha}" }}')
    (c / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (c / "src/lib.rs").write_text("pub fn x() {}\n")
    return c


def checkout_roots(cargo_home):
    return sorted(p.parent for p in cargo_home.rglob(".cargo-ok"))


def write_oracle(roots, u):
    paths = [f"pkg{i:04d}" for i in range(u)]
    for checkout in roots:
        (checkout / ".cargo-manifest-oracle").write_text("\n".join(paths) + "\n")


def index_stats(roots):
    files = [r / ".cargo-manifest-index-experiment" for r in roots]
    files = [p for p in files if p.exists()]
    entries = 0
    names = set()
    total_bytes = 0
    for p in files:
        total_bytes += p.stat().st_size
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            entries += 1
            names.add(line.split("\t", 1)[0])
    return {"files": len(files), "entries": entries, "unique_names": len(names), "bytes": total_bytes}


def one_metadata(bin_path, cwd, env, extra_env=None):
    e = env.copy()
    if extra_env:
        e.update(extra_env)
    return subprocess.check_output(
        [str(bin_path), "metadata", "--format-version", "1", "--locked", "--offline"],
        cwd=cwd, env=e, stderr=subprocess.DEVNULL,
    )


def measure(cwd, env, rounds=8):
    variants = {
        "base": (base, {}),
        "index": (index_bin, {"CARGO_GIT_MANIFEST_INDEX_USE": "1"}),
        "oracle": (oracle, {"CARGO_GIT_MANIFEST_ORACLE": "1"}),
    }
    values = {k: [] for k in variants}
    orders = list(itertools.permutations(variants))
    for r in range(rounds):
        for name in orders[r % len(orders)]:
            bin_path, extra = variants[name]
            e = env.copy(); e.update(extra)
            start = time.perf_counter_ns()
            p = subprocess.run(
                [str(bin_path), "metadata", "--format-version", "1", "--locked", "--offline"],
                cwd=cwd, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            ms = (time.perf_counter_ns() - start) / 1_000_000
            if p.returncode:
                raise SystemExit(f"{name} failed: {p.stderr[-6000:]}")
            values[name].append(ms)
    med = {k: statistics.median(v) for k, v in values.items()}
    oracle_gain = med["base"] - med["oracle"]
    index_gain = med["base"] - med["index"]
    return {
        "medians_ms": med,
        "runs_ms": values,
        "index_gain_ms": index_gain,
        "oracle_gain_ms": oracle_gain,
        "fraction_oracle_captured": index_gain / oracle_gain if oracle_gain > 0 else None,
    }


def run_case(n, u):
    repo, sha = make_repo(n)
    c = make_consumer(repo, sha, n, u)
    cargo_home = root / f"cargo-home-n{n}-u{u}"
    shutil.rmtree(cargo_home, ignore_errors=True)
    env = os.environ.copy()
    env.update({
        "CARGO_HOME": str(cargo_home),
        "CARGO_TERM_COLOR": "never",
        "RUSTUP_TOOLCHAIN": "1.95.0",
    })

    # Fetch/lock first. Then build the warm index from Cargo's existing full
    # discovery in an untimed process. This does not use resolved metadata.
    run([str(base), "generate-lockfile"], cwd=c, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    build_env = env.copy()
    build_env.update({"CARGO_NET_OFFLINE": "true", "CARGO_GIT_MANIFEST_INDEX_BUILD": "1"})
    run(
        [str(index_bin), "metadata", "--format-version", "1", "--locked", "--offline"],
        cwd=c, env=build_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    roots = checkout_roots(cargo_home)
    if not roots:
        raise SystemExit(f"no checkout roots n={n} u={u}")
    stats = index_stats(roots)
    if stats["files"] != len(roots):
        raise SystemExit(f"index missing for checkout n={n} u={u}: {stats} roots={len(roots)}")
    write_oracle(roots, u)
    env["CARGO_NET_OFFLINE"] = "true"

    base_doc = json.loads(one_metadata(base, c, env))
    index_doc = json.loads(one_metadata(index_bin, c, env, {"CARGO_GIT_MANIFEST_INDEX_USE": "1"}))
    oracle_doc = json.loads(one_metadata(oracle, c, env, {"CARGO_GIT_MANIFEST_ORACLE": "1"}))
    if index_doc != base_doc or oracle_doc != base_doc:
        raise SystemExit(f"metadata mismatch n={n} u={u}")

    result = {
        "n": n,
        "u": u,
        "metadata_packages": len(base_doc.get("packages", [])),
        "checkout_count": len(roots),
        "index": stats,
    }
    result.update(measure(c, env))
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


results = []
for n, us in CASES.items():
    for u in us:
        results.append(run_case(n, u))

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"cases": results}, indent=2, sort_keys=True))

key = {(r["n"], r["u"]): r for r in results}
summary = {}
for case in ((1000, 1), (1000, 1000), (2000, 1), (2000, 10)):
    if case in key:
        r = key[case]
        summary[f"n{case[0]}_u{case[1]}"] = {
            "base_ms": r["medians_ms"]["base"],
            "index_ms": r["medians_ms"]["index"],
            "oracle_ms": r["medians_ms"]["oracle"],
            "index_gain_ms": r["index_gain_ms"],
            "oracle_gain_ms": r["oracle_gain_ms"],
            "fraction_oracle_captured": r["fraction_oracle_captured"],
        }
print(json.dumps(summary, indent=2, sort_keys=True))
