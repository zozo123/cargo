#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import subprocess
import sys
from urllib.parse import quote

root = pathlib.Path(sys.argv[1]).resolve()
cargo_base = pathlib.Path(sys.argv[2]).resolve()
cargo_index = pathlib.Path(sys.argv[3]).resolve()
out_path = pathlib.Path(sys.argv[4]).resolve()
root.mkdir(parents=True, exist_ok=True)


def run(argv, cwd, env, check=True):
    p = subprocess.run(
        [str(x) for x in argv],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and p.returncode:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(map(str, argv))}\n{p.stderr[-8000:]}"
        )
    return p


def git(repo, *args):
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git(path, "config", "user.email", "cargo-index@example.invalid")
    git(path, "config", "user.name", "cargo index experiment")


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def write_package(path, name, version="0.1.0", extra=""):
    path.mkdir(parents=True, exist_ok=True)
    (path / "src").mkdir(exist_ok=True)
    (path / "src/lib.rs").write_text(f"pub fn {name.replace('-', '_')}() {{}}\n")
    text = f'[package]\nname = "{name}"\nversion = "{version}"\nedition = "2021"\n'
    if extra:
        text += "\n" + extra.strip() + "\n"
    (path / "Cargo.toml").write_text(text)


def file_url(path):
    # Git accepts file:// URLs. quote keeps spaces deterministic.
    return "file://" + quote(str(path))


def make_consumer(path, source, rev, package="foo"):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "src").mkdir()
    (path / "src/lib.rs").write_text("pub fn consumer() {}\n")
    (path / "Cargo.toml").write_text(
        '[package]\nname = "consumer"\nversion = "0.1.0"\nedition = "2021"\n\n'
        f'[dependencies]\n{package} = {{ git = "{file_url(source)}", rev = "{rev}" }}\n'
    )


def env_for(home, **extra):
    e = os.environ.copy()
    e["CARGO_HOME"] = str(home)
    e["CARGO_NET_OFFLINE"] = "true"
    e.update(extra)
    return e


def bootstrap(consumer, home):
    home.mkdir(parents=True, exist_ok=True)
    e = os.environ.copy()
    e["CARGO_HOME"] = str(home)
    # First run creates Cargo.lock and fetches the local git source.
    run([cargo_base, "metadata", "--format-version", "1"], consumer, e)


def metadata(cargo, consumer, home, **extra):
    e = env_for(home, **extra)
    p = run(
        [cargo, "metadata", "--format-version", "1", "--locked", "--offline"],
        consumer,
        e,
    )
    return json.loads(p.stdout), p.stderr


def sidecars(home):
    return sorted((home / "git" / "checkouts").rglob(".cargo-manifest-index-experiment"))


def assert_equal(label, want, got):
    if got != want:
        raise AssertionError(f"{label}: metadata differs")


results = []


def record(name, **data):
    row = {"case": name, "ok": True, **data}
    results.append(row)
    print(json.dumps(row, sort_keys=True), flush=True)


# 1. Duplicate PackageId ordering. The index must not change which duplicate
# current Cargo selects or the resulting metadata.
case = root / "duplicate"
source = case / "source"
consumer = case / "consumer"
home = case / "cargo-home"
init_repo(source)
write_package(source / "z-first", "foo")
write_package(source / "a-second", "foo")
rev = commit(source, "duplicates")
make_consumer(consumer, source, rev)
bootstrap(consumer, home)
base_doc, base_err = metadata(cargo_base, consumer, home)
build_doc, _ = metadata(
    cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_BUILD="1"
)
assert_equal("duplicate build", base_doc, build_doc)
idx_doc, idx_err = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
assert_equal("duplicate warm", base_doc, idx_doc)
base_git = sorted(
    p["manifest_path"] for p in base_doc["packages"] if (p.get("source") or "").startswith("git+")
)
idx_git = sorted(
    p["manifest_path"] for p in idx_doc["packages"] if (p.get("source") or "").startswith("git+")
)
assert base_git == idx_git
record("duplicate-package-id", selected=base_git)

# 2. Hidden/sub-repository packages reachable only through path dependencies.
case = root / "path-deps"
source = case / "source"
consumer = case / "consumer"
home = case / "cargo-home"
init_repo(source)
for name in ("normal_hidden", "build_hidden", "dev_hidden", "target_hidden"):
    write_package(source / f".{name}", name.replace("_", "-"))
write_package(source / "subrepo" / "dep", "subrepo-dep")
(source / "subrepo" / "dep" / ".git").mkdir()
extra = '''
[dependencies]
normal-hidden = { path = "../.normal_hidden" }
subrepo-dep = { path = "../subrepo/dep" }
[build-dependencies]
build-hidden = { path = "../.build_hidden" }
[dev-dependencies]
dev-hidden = { path = "../.dev_hidden" }
[target.'cfg(unix)'.dependencies]
target-hidden = { path = "../.target_hidden" }
'''
write_package(source / "rootpkg", "foo", extra=extra)
rev = commit(source, "path dependency closure")
make_consumer(consumer, source, rev)
bootstrap(consumer, home)
base_doc, _ = metadata(cargo_base, consumer, home)
metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_BUILD="1")
idx_doc, _ = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
assert_equal("path-dependency closure", base_doc, idx_doc)
names = sorted(p["name"] for p in base_doc["packages"])
for expected in ("foo", "normal-hidden", "build-hidden", "dev-hidden", "target-hidden", "subrepo-dep"):
    assert expected in names, (expected, names)
record("hidden-and-path-dependency-closure", packages=names)

# 3. Cache failure modes. Any malformed, partial, unsafe, or stale-looking
# sidecar must behave exactly like no cache.
case = root / "fallback"
source = case / "source"
consumer = case / "consumer"
home = case / "cargo-home"
init_repo(source)
write_package(source / "foo-dir", "foo")
write_package(source / "bar-dir", "bar")
rev = commit(source, "fallback source")
make_consumer(consumer, source, rev)
bootstrap(consumer, home)
base_doc, _ = metadata(cargo_base, consumer, home)
metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_BUILD="1")
paths = sidecars(home)
assert len(paths) == 1, paths
index_path = paths[0]
valid = index_path.read_text()
assert valid.startswith("cargo-manifest-index-v1\n")
mutations = {
    "missing": None,
    "bad-header": "not-our-format\nfoo\tfoo-dir\n",
    "truncated": "cargo-manifest-index-v1\nbroken-line\n",
    "unsafe-parent": "cargo-manifest-index-v1\nfoo\t../../escape\n",
    "wrong-path": "cargo-manifest-index-v1\nfoo\tmissing\n",
    "wrong-name": "cargo-manifest-index-v1\nfoo\tbar-dir\n",
    "partial-no-key": "cargo-manifest-index-v1\nbar\tbar-dir\n",
}
for label, contents in mutations.items():
    if index_path.exists():
        index_path.unlink()
    if contents is not None:
        index_path.write_text(contents)
    doc, _ = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
    assert_equal(f"fallback {label}", base_doc, doc)
index_path.write_text(valid)
record("missing-corrupt-stale-fallback", variants=sorted(mutations))

# 4. Concurrent cold builders. Cargo may serialize some work internally; this
# still verifies that multiple real processes cannot publish a torn sidecar.
case = root / "concurrent"
source = case / "source"
consumer = case / "consumer"
home = case / "cargo-home"
init_repo(source)
write_package(source / "wanted", "foo")
for i in range(80):
    write_package(source / f"other-{i:03d}", f"other-{i:03d}")
rev = commit(source, "many packages")
make_consumer(consumer, source, rev)
bootstrap(consumer, home)
base_doc, _ = metadata(cargo_base, consumer, home)
for p in sidecars(home):
    p.unlink()
e = env_for(home, CARGO_GIT_MANIFEST_INDEX_BUILD="1")
procs = [
    subprocess.Popen(
        [str(cargo_index), "metadata", "--format-version", "1", "--locked", "--offline"],
        cwd=consumer,
        env=e,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(8)
]
errs = []
for p in procs:
    _, err = p.communicate()
    if p.returncode:
        errs.append(err[-4000:])
assert not errs, errs
paths = sidecars(home)
assert len(paths) == 1, paths
text = paths[0].read_text()
assert text.startswith("cargo-manifest-index-v1\n")
assert not list(paths[0].parent.glob(".cargo-manifest-index-experiment.tmp-*"))
doc, _ = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
assert_equal("concurrent builders warm", base_doc, doc)
record("concurrent-builders", processes=8, index_bytes=paths[0].stat().st_size)

# 5. Revision isolation. An index from commit A stays in A's exact checkout;
# commit B must not consume it. USE without a B index must fall back correctly.
case = root / "revision"
source = case / "source"
consumer = case / "consumer"
home = case / "cargo-home"
init_repo(source)
write_package(source / "old-location", "foo")
rev_a = commit(source, "revision a")
make_consumer(consumer, source, rev_a)
bootstrap(consumer, home)
base_a, _ = metadata(cargo_base, consumer, home)
metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_BUILD="1")
indices_a = sidecars(home)
assert len(indices_a) == 1
shutil.move(str(source / "old-location"), str(source / "new-location"))
rev_b = commit(source, "revision b")
make_consumer(consumer, source, rev_b)
bootstrap(consumer, home)
base_b, _ = metadata(cargo_base, consumer, home)
# No build for B: warm-index mode must fall back to current discovery.
idx_b, _ = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
assert_equal("revision fallback", base_b, idx_b)
assert base_a != base_b, "revision fixture did not change metadata"
metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_BUILD="1")
idx_b_warm, _ = metadata(cargo_index, consumer, home, CARGO_GIT_MANIFEST_INDEX_USE="1")
assert_equal("revision b warm", base_b, idx_b_warm)
record("revision-isolation", rev_a=rev_a, rev_b=rev_b, index_files=len(sidecars(home)))

summary = {"ok": True, "cases": results, "case_count": len(results)}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, indent=2, sort_keys=True))
