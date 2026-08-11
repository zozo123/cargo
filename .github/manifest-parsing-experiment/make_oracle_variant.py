#!/usr/bin/env python3
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
path = root / "src/sources/path.rs"
s = path.read_text()

needle = """    let mut all_packages = HashMap::default();
    let mut visited = HashSet::<PathBuf>::default();
    let mut errors = Vec::<anyhow::Error>::new();
"""
assert needle in s

replacement = needle + """

    // Experimental upper bound for cargo#14395: if an external oracle has
    // already told us which package roots matter for this immutable git
    // checkout, skip the recursive discovery walk and fully parse only those
    // manifests. This is deliberately not a production cache design.
    if source_id.is_git() && std::env::var_os("CARGO_GIT_MANIFEST_ORACLE").is_some() {
        let oracle_path = path.join(".cargo-manifest-oracle");
        if let Ok(contents) = fs::read_to_string(&oracle_path) {
            for raw in contents.lines() {
                let rel = raw.trim();
                if rel.is_empty() || rel.starts_with('#') {
                    continue;
                }
                let dir = if rel == "." {
                    path.to_path_buf()
                } else {
                    path.join(rel)
                };
                if has_manifest(&dir) {
                    read_nested_packages(
                        &dir,
                        &mut all_packages,
                        source_id,
                        gctx,
                        &mut visited,
                        &mut errors,
                    )?;
                }
            }
            if !all_packages.is_empty() {
                trace!(
                    "manifest oracle loaded {} package ids from {}",
                    all_packages.len(),
                    oracle_path.display()
                );
                return Ok(all_packages);
            }
        }
    }
"""

s = s.replace(needle, replacement, 1)
path.write_text(s)
print("generated git manifest oracle variant")
