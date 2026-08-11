#!/usr/bin/env python3
import json
import pathlib
import sys

metadata_path = pathlib.Path(sys.argv[1])
out_path = pathlib.Path(sys.argv[2])
metadata = json.loads(metadata_path.read_text())


def find_checkout_root(manifest_path: pathlib.Path):
    cur = manifest_path.parent
    for parent in (cur, *cur.parents):
        if (parent / ".cargo-ok").is_file():
            return parent
    return None


groups = {}
missing_roots = []
for pkg in metadata.get("packages", []):
    source = pkg.get("source") or ""
    if not source.startswith("git+"):
        continue
    manifest = pathlib.Path(pkg["manifest_path"])
    root = find_checkout_root(manifest)
    if root is None:
        missing_roots.append(str(manifest))
        continue
    rel = manifest.parent.relative_to(root)
    rel_text = "." if str(rel) == "." else rel.as_posix()
    entry = groups.setdefault(root, {"paths": set(), "sources": set(), "packages": []})
    entry["paths"].add(rel_text)
    entry["sources"].add(source)
    entry["packages"].append(pkg.get("name"))

if missing_roots:
    raise SystemExit("could not locate git checkout roots for:\n" + "\n".join(missing_roots))

per_checkout = []
for root, entry in sorted(groups.items(), key=lambda kv: str(kv[0])):
    paths = sorted(entry["paths"])
    marker = root / ".cargo-manifest-oracle"
    marker.write_text("\n".join(paths) + "\n")
    raw_tomls = sum(1 for _ in root.rglob("Cargo.toml"))
    per_checkout.append(
        {
            "root": str(root),
            "source_ids": sorted(entry["sources"]),
            "selected_package_rows": len(entry["packages"]),
            "oracle_manifest_paths": len(paths),
            "raw_cargo_toml_count": raw_tomls,
            "raw_skip_fraction": (1.0 - len(paths) / raw_tomls) if raw_tomls else 0.0,
        }
    )

summary = {
    "git_checkout_count": len(per_checkout),
    "git_package_rows": sum(x["selected_package_rows"] for x in per_checkout),
    "oracle_manifest_paths": sum(x["oracle_manifest_paths"] for x in per_checkout),
    "raw_cargo_toml_count": sum(x["raw_cargo_toml_count"] for x in per_checkout),
    "per_checkout": per_checkout,
}
if summary["raw_cargo_toml_count"]:
    summary["raw_skip_fraction"] = 1.0 - summary["oracle_manifest_paths"] / summary["raw_cargo_toml_count"]
else:
    summary["raw_skip_fraction"] = 0.0

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
print(json.dumps(summary, indent=2, sort_keys=True))
