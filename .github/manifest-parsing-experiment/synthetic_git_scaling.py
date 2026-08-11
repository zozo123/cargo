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
full = pathlib.Path(sys.argv[3])
oracle = pathlib.Path(sys.argv[4])
out = pathlib.Path(sys.argv[5])
root.mkdir(parents=True, exist_ok=True)

ALL_CASES = {
    1: [1],
    10: [1],
    100: [1, 10],
    500: [1, 10],
    1000: [1, 10, 1000],
    2000: [1],
}
CONTROL_NS = [100, 500, 1000, 2000]


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def manifest_text(i):
    return (
        f'[package]\nname = "pkg{i:04d}"\nversion = "0.1.0"\nedition = "2021"\n'
    )


def make_repo(n, layout):
    repo = root / f"repo-{layout}-{n}"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir()
    run(["git", "init", "-q", str(repo)])
    run(["git", "-C", str(repo), "config", "user.email", "bench@example.invalid"])
    run(["git", "-C", str(repo), "config", "user.name", "Cargo perf bench"])
    payload_bytes = 0
    actual_manifests = 0
    for i in range(n):
        d = repo / f"pkg{i:04d}"
        (d / "src").mkdir(parents=True)
        text = manifest_text(i)
        payload_bytes += len(text.encode())
        if layout == "all" or i == 0:
            (d / "Cargo.toml").write_text(text)
            actual_manifests += 1
        else:
            # Same manifest payload and directory/file count, but inert filename.
            (d / "Cargo.toml.inert").write_text(text)
        (d / "src/lib.rs").write_text(f"pub const ID: usize = {i};\n")
    run(["git", "-C", str(repo), "add", "."])
    run(["git", "-C", str(repo), "commit", "-qm", f"synthetic {layout} {n} packages"])
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, sha, payload_bytes, actual_manifests


def make_consumer(repo, sha, n, u, layout):
    c = root / f"consumer-{layout}-n{n}-u{u}"
    shutil.rmtree(c, ignore_errors=True)
    (c / "src").mkdir(parents=True)
    url = repo.resolve().as_uri()
    lines = [
        '[package]',
        'name = "consumer"',
        'version = "0.1.0"',
        'edition = "2021"',
        '',
        '[dependencies]',
    ]
    for i in range(u):
        lines.append(f'pkg{i:04d} = {{ git = "{url}", rev = "{sha}" }}')
    (c / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (c / "src/lib.rs").write_text("pub fn x() {}\n")
    return c


def write_oracle_markers(cargo_home, u):
    roots = sorted(p.parent for p in cargo_home.rglob(".cargo-ok"))
    if not roots:
        raise SystemExit(f"no git checkout root found under {cargo_home}")
    paths = [f"pkg{i:04d}" for i in range(u)]
    for checkout in roots:
        (checkout / ".cargo-manifest-oracle").write_text("\n".join(paths) + "\n")
    return roots


def measure(cwd, env, rounds=8):
    bins = {"base": base, "full": full, "oracle": oracle}
    values = {name: [] for name in bins}
    orders = list(itertools.permutations(bins))
    for r in range(rounds):
        for name in orders[r % len(orders)]:
            e = env.copy()
            if name == "oracle":
                e["CARGO_GIT_MANIFEST_ORACLE"] = "1"
            start = time.perf_counter_ns()
            p = subprocess.run(
                [str(bins[name]), "metadata", "--format-version", "1", "--locked", "--offline"],
                cwd=cwd,
                env=e,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            ms = (time.perf_counter_ns() - start) / 1_000_000
            if p.returncode:
                raise SystemExit(f"{name} failed: {p.stderr[-5000:]}")
            values[name].append(ms)
    return values


def summarize(values):
    med = {name: statistics.median(xs) for name, xs in values.items()}
    return {
        "medians_ms": med,
        "runs_ms": values,
        "full_improvement_ms": med["base"] - med["full"],
        "full_improvement_percent": (med["base"] - med["full"]) / med["base"] * 100.0,
        "oracle_improvement_ms": med["base"] - med["oracle"],
        "oracle_improvement_percent": (med["base"] - med["oracle"]) / med["base"] * 100.0,
    }


def run_case(n, u, layout):
    repo, sha, payload_bytes, actual_manifests = make_repo(n, layout)
    c = make_consumer(repo, sha, n, u, layout)
    cargo_home = root / f"cargo-home-{layout}-n{n}-u{u}"
    shutil.rmtree(cargo_home, ignore_errors=True)
    env = os.environ.copy()
    env.update(
        {
            "CARGO_HOME": str(cargo_home),
            "CARGO_TERM_COLOR": "never",
            "RUSTUP_TOOLCHAIN": "1.95.0",
        }
    )

    # Untimed setup: lock, checkout, then build the oracle from known-needed roots.
    run([str(base), "generate-lockfile"], cwd=c, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    roots = write_oracle_markers(cargo_home, u)
    env["CARGO_NET_OFFLINE"] = "true"

    base_meta = subprocess.check_output(
        [str(base), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=env
    )
    full_meta = subprocess.check_output(
        [str(full), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=env
    )
    oracle_env = env.copy()
    oracle_env["CARGO_GIT_MANIFEST_ORACLE"] = "1"
    oracle_meta = subprocess.check_output(
        [str(oracle), "metadata", "--format-version", "1", "--locked", "--offline"], cwd=c, env=oracle_env
    )
    base_doc = json.loads(base_meta)
    if json.loads(full_meta) != base_doc or json.loads(oracle_meta) != base_doc:
        raise SystemExit(f"metadata mismatch layout={layout} n={n} u={u}")

    for name, b in (("base", base), ("full", full), ("oracle", oracle)):
        e = env.copy()
        if name == "oracle":
            e["CARGO_GIT_MANIFEST_ORACLE"] = "1"
        run(
            [str(b), "metadata", "--format-version", "1", "--locked", "--offline"],
            cwd=c,
            env=e,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    result = {
        "layout": layout,
        "n_directories": n,
        "u_directly_needed": u,
        "manifest_payload_bytes": payload_bytes,
        "actual_cargo_tomls": actual_manifests,
        "oracle_checkout_count": len(roots),
        "metadata_packages": len(base_doc.get("packages", [])),
    }
    result.update(summarize(measure(c, env)))
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


cases = []
for n, us in ALL_CASES.items():
    for u in us:
        cases.append(run_case(n, u, "all"))

controls = [run_case(n, 1, "inert") for n in CONTROL_NS]

all_u1 = {r["n_directories"]: r for r in cases if r["u_directly_needed"] == 1}
inert_u1 = {r["n_directories"]: r for r in controls}
ref = all_u1[1]["medians_ms"]["base"]
a2000 = all_u1[2000]["medians_ms"]
i2000 = inert_u1[2000]["medians_ms"]
interpretation = {
    "n1_u1_base_ms": ref,
    "n2000_u1_base_ms": a2000["base"],
    "n2000_u1_full_ms": a2000["full"],
    "n2000_u1_oracle_ms": a2000["oracle"],
    "n2000_inert_u1_base_ms": i2000["base"],
    "total_n_dependent_tax_ms": a2000["base"] - ref,
    "matched_tree_traversal_tax_ms": i2000["base"] - ref,
    "manifest_specific_tax_vs_matched_tree_ms": a2000["base"] - i2000["base"],
    "full_17296_recovered_ms": a2000["base"] - a2000["full"],
    "oracle_recovered_ms": a2000["base"] - a2000["oracle"],
    "oracle_remaining_vs_n1_ms": a2000["oracle"] - ref,
}
interpretation["oracle_fraction_of_total_n_tax_recovered"] = (
    interpretation["oracle_recovered_ms"] / interpretation["total_n_dependent_tax_ms"]
    if interpretation["total_n_dependent_tax_ms"]
    else 0.0
)
interpretation["manifest_specific_fraction_of_total_n_tax"] = (
    interpretation["manifest_specific_tax_vs_matched_tree_ms"] / interpretation["total_n_dependent_tax_ms"]
    if interpretation["total_n_dependent_tax_ms"]
    else 0.0
)

summary = {"cases": cases, "matched_tree_controls": controls, "interpretation": interpretation}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps(interpretation, indent=2, sort_keys=True))
