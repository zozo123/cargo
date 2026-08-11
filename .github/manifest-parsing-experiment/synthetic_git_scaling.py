#!/usr/bin/env python3
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
full = pathlib.Path(sys.argv[3])
out = pathlib.Path(sys.argv[4])
root.mkdir(parents=True, exist_ok=True)

cases_by_n = {
    1: [1],
    10: [1],
    100: [1, 10],
    500: [1, 10],
    1000: [1, 10, 1000],
    2000: [1],
}

def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)

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
        (d / "Cargo.toml").write_text(
            f'[package]\nname = "pkg{i:04d}"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (d / "src/lib.rs").write_text(f"pub const ID: usize = {i};\n")
    run(["git", "-C", str(repo), "add", "."])
    run(["git", "-C", str(repo), "commit", "-qm", f"synthetic {n} packages"])
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, sha

def make_consumer(repo, sha, n, u):
    c = root / f"consumer-n{n}-u{u}"
    shutil.rmtree(c, ignore_errors=True)
    (c / "src").mkdir(parents=True)
    url = repo.resolve().as_uri()
    lines = ['[package]', 'name = "consumer"', 'version = "0.1.0"', 'edition = "2021"', '', '[dependencies]']
    for i in range(u):
        lines.append(f'pkg{i:04d} = {{ git = "{url}", rev = "{sha}" }}')
    (c / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (c / "src/lib.rs").write_text("pub fn x() {}\n")
    return c

def measure_pair(cwd, env, rounds=6):
    values = {"base": [], "full": []}
    bins = {"base": base, "full": full}
    for r in range(rounds):
        order = ["base", "full"] if r % 2 == 0 else ["full", "base"]
        for name in order:
            start = time.perf_counter_ns()
            p = subprocess.run(
                [str(bins[name]), "metadata", "--format-version", "1", "--locked", "--offline"],
                cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            ms = (time.perf_counter_ns() - start) / 1_000_000
            if p.returncode:
                raise SystemExit(f"{name} failed n/u case: {p.stderr[-5000:]}")
            values[name].append(ms)
    return values

results = []
for n, us in cases_by_n.items():
    repo, sha = make_repo(n)
    manifest_bytes = sum(p.stat().st_size for p in repo.rglob("Cargo.toml"))
    for u in us:
        c = make_consumer(repo, sha, n, u)
        cargo_home = root / f"cargo-home-n{n}-u{u}"
        shutil.rmtree(cargo_home, ignore_errors=True)
        env = os.environ.copy()
        env.update({
            "CARGO_HOME": str(cargo_home),
            "CARGO_TERM_COLOR": "never",
            "RUSTUP_TOOLCHAIN": "1.95.0",
        })
        # Untimed setup: lock, checkout, and naturally warm the source files.
        run([str(base), "generate-lockfile"], cwd=c, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env["CARGO_NET_OFFLINE"] = "true"
        base_meta = subprocess.check_output([str(base), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=env)
        full_meta = subprocess.check_output([str(full), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=env)
        if json.loads(base_meta) != json.loads(full_meta):
            raise SystemExit(f"metadata mismatch n={n} u={u}")
        # One warmup each, then paired timings.
        for b in (base, full):
            run([str(b), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vals = measure_pair(c, env)
        bmed = statistics.median(vals["base"])
        fmed = statistics.median(vals["full"])
        row = {
            "n_manifests": n,
            "u_directly_needed": u,
            "manifest_bytes": manifest_bytes,
            "base_median_ms": bmed,
            "full_17296_median_ms": fmed,
            "full_improvement_ms": bmed - fmed,
            "full_improvement_percent": (bmed - fmed) / bmed * 100.0,
            "base_runs_ms": vals["base"],
            "full_runs_ms": vals["full"],
        }
        results.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

# Counterfactual upper-bound proxy: cost of irrelevant manifests at U=1.
u1 = {r["n_manifests"]: r for r in results if r["u_directly_needed"] == 1}
ref = u1[1]["base_median_ms"]
for n, r in u1.items():
    r["irrelevant_manifest_tax_vs_n1_ms"] = r["base_median_ms"] - ref

summary = {
    "cases": results,
    "interpretation": {
        "n1_u1_base_ms": ref,
        "n2000_u1_extra_ms": u1[2000]["base_median_ms"] - ref,
        "n2000_u1_extra_percent_of_base": (u1[2000]["base_median_ms"] - ref) / u1[2000]["base_median_ms"] * 100.0,
    },
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps(summary["interpretation"], indent=2, sort_keys=True))
