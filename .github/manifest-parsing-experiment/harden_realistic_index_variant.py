#!/usr/bin/env python3
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
here = pathlib.Path(__file__).resolve().parent
subprocess.run(
    [sys.executable, str(here / "make_realistic_index_variant.py"), str(root)],
    check=True,
)

path_rs = root / "src/sources/path.rs"
s = path_rs.read_text()

# Treat the sidecar as a complete snapshot and an optimization only. A torn or
# malformed index must never become a partial source of truth. The footer count
# lets a valid snapshot answer a missing package name authoritatively without
# falling back to a full recursive scan.
old_loader = '''            if self.manifest_index.borrow().is_none() {
                let index_path = self.path.join(".cargo-manifest-index-experiment");
                if let Ok(contents) = fs::read_to_string(&index_path) {
                    let mut index = HashMap::<String, Vec<PathBuf>>::default();
                    for line in contents.lines() {
                        let Some((name, rel)) = line.split_once('\\t') else {
                            continue;
                        };
                        if name.is_empty() || rel.is_empty() {
                            continue;
                        }
                        index
                            .entry(name.to_owned())
                            .or_default()
                            .push(PathBuf::from(rel));
                    }
                    if !index.is_empty() {
                        self.manifest_index.replace(Some(index));
                    }
                }
            }
'''
new_loader = '''            if self.manifest_index.borrow().is_none() {
                let index_path = self.path.join(".cargo-manifest-index-experiment");
                if let Ok(contents) = fs::read_to_string(&index_path) {
                    let lines = contents.lines().collect::<Vec<_>>();
                    let mut valid = lines.first().copied() == Some("cargo-manifest-index-v2");
                    let mut index = HashMap::<String, Vec<PathBuf>>::default();
                    if valid {
                        if lines.len() < 2 {
                            valid = false;
                        } else {
                            let body = &lines[1..lines.len() - 1];
                            let expected_entries = lines[lines.len() - 1]
                                .strip_prefix("cargo-manifest-index-end\\t")
                                .and_then(|value| value.parse::<usize>().ok());
                            if expected_entries != Some(body.len()) {
                                valid = false;
                            }
                            if valid {
                                for line in body {
                                    let Some((name, rel)) = line.split_once('\\t') else {
                                        valid = false;
                                        break;
                                    };
                                    if name.is_empty() || rel.is_empty() {
                                        valid = false;
                                        break;
                                    }
                                    let rel = PathBuf::from(rel);
                                    if rel.is_absolute()
                                        || rel.components().any(|component| {
                                            matches!(
                                                component,
                                                std::path::Component::ParentDir
                                                    | std::path::Component::RootDir
                                                    | std::path::Component::Prefix(_)
                                            )
                                        })
                                    {
                                        valid = false;
                                        break;
                                    }
                                    index.entry(name.to_owned()).or_default().push(rel);
                                }
                            }
                        }
                    }
                    if valid {
                        self.manifest_index.replace(Some(index));
                    }
                }
            }
'''
assert old_loader in s, "warm index loader shape changed"
s = s.replace(old_loader, new_loader, 1)

# A validated complete snapshot can answer a negative lookup. This is crucial:
# dependency resolution probes names that do not exist in a particular git
# source. Treating every miss as cache corruption collapses back to eager load.
old_lookup = '''            if let Some(candidates) = candidates {
                let candidates = candidates.unwrap_or_default();
'''
new_lookup = '''            if let Some(candidates) = candidates {
                let Some(candidates) = candidates else {
                    return Ok(());
                };
'''
assert old_lookup in s, "warm index lookup shape changed"
s = s.replace(old_lookup, new_lookup, 1)

# If an indexed name points at a missing path, or no longer materializes a
# package with that name, abandon the index and fall back to the existing full
# discovery path. Do not let a bad cache change resolution semantics.
old_candidates = '''                let mut discovered = HashMap::default();
                let mut errors = Vec::<anyhow::Error>::new();
                let before = self.indexed_visited.borrow().len();
                {
                    let mut visited = self.indexed_visited.borrow_mut();
                    for rel in &candidates {
                        let dir = if rel == Path::new(".") {
                            self.path.clone()
                        } else {
                            self.path.join(rel)
                        };
                        if has_manifest(&dir) {
                            read_nested_packages(
                                &dir,
                                &mut discovered,
                                self.source_id,
                                self.gctx,
                                &mut visited,
                                &mut errors,
                            )?;
                        }
                    }
                }

                if !discovered.is_empty() {
                    let mut packages = self.packages.borrow_mut();
                    for (pkg_id, mut found) in discovered {
                        packages.entry(pkg_id).or_default().append(&mut found);
                    }
                }

                if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                    eprintln!(
                        "cargo-manifest-index query={} candidates={} newly_parsed={} total_parsed={}",
                        package_name,
                        candidates.len(),
                        self.indexed_visited.borrow().len().saturating_sub(before),
                        self.indexed_visited.borrow().len(),
                    );
                }

                for s in self
                    .packages
                    .borrow()
                    .iter()
                    .filter(|(pkg_id, _)| pkg_id.name() == dep.package_name())
                    .map(|(pkg_id, pkgs)| {
                        first_package(
                            *pkg_id,
                            pkgs,
                            &mut self.warned_duplicate.borrow_mut(),
                            self.gctx,
                        )
                    })
                    .map(|p| p.summary())
                {
                    let matched = match kind {
                        QueryKind::Exact | QueryKind::RejectedVersions => dep.matches(s),
                        QueryKind::AlternativeNames => true,
                        QueryKind::Normalized => dep.matches(s),
                    };
                    if matched {
                        f(IndexSummary::Candidate(s.clone()))
                    }
                }
                return Ok(());
'''
new_candidates = '''                let mut discovered = HashMap::default();
                let mut errors = Vec::<anyhow::Error>::new();
                let before = self.indexed_visited.borrow().len();
                let mut index_entry_valid = !candidates.is_empty();
                {
                    let mut visited = self.indexed_visited.borrow_mut();
                    for rel in &candidates {
                        let dir = if rel == Path::new(".") {
                            self.path.clone()
                        } else {
                            self.path.join(rel)
                        };
                        if !has_manifest(&dir) {
                            index_entry_valid = false;
                            break;
                        }
                        read_nested_packages(
                            &dir,
                            &mut discovered,
                            self.source_id,
                            self.gctx,
                            &mut visited,
                            &mut errors,
                        )?;
                    }
                }

                if index_entry_valid
                    && !discovered
                        .keys()
                        .any(|pkg_id| pkg_id.name() == dep.package_name())
                {
                    index_entry_valid = false;
                }

                if index_entry_valid {
                    if !discovered.is_empty() {
                        let mut packages = self.packages.borrow_mut();
                        for (pkg_id, mut found) in discovered {
                            packages.entry(pkg_id).or_default().append(&mut found);
                        }
                    }

                    if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                        eprintln!(
                            "cargo-manifest-index query={} candidates={} newly_parsed={} total_parsed={}",
                            package_name,
                            candidates.len(),
                            self.indexed_visited.borrow().len().saturating_sub(before),
                            self.indexed_visited.borrow().len(),
                        );
                    }

                    for s in self
                        .packages
                        .borrow()
                        .iter()
                        .filter(|(pkg_id, _)| pkg_id.name() == dep.package_name())
                        .map(|(pkg_id, pkgs)| {
                            first_package(
                                *pkg_id,
                                pkgs,
                                &mut self.warned_duplicate.borrow_mut(),
                                self.gctx,
                            )
                        })
                        .map(|p| p.summary())
                    {
                        let matched = match kind {
                            QueryKind::Exact | QueryKind::RejectedVersions => dep.matches(s),
                            QueryKind::AlternativeNames => true,
                            QueryKind::Normalized => dep.matches(s),
                        };
                        if matched {
                            f(IndexSummary::Candidate(s.clone()))
                        }
                    }
                    return Ok(());
                }

                self.manifest_index.replace(None);
                self.indexed_visited.borrow_mut().clear();
'''
assert old_candidates in s, "warm index candidate path changed"
s = s.replace(old_candidates, new_candidates, 1)

# Preserve duplicate PackageId ordering from the authoritative full discovery.
# Sort package-id groups for deterministic output, but never sort paths inside a
# duplicate group. Publish an explicitly complete snapshot via temp + rename.
old_writer = '''            let mut lines = Vec::new();
            for (pkg_id, packages) in &all_packages {
                for pkg in packages {
                    if let Ok(rel) = pkg.root().strip_prefix(path) {
                        let rel = if rel.as_os_str().is_empty() {
                            ".".to_owned()
                        } else {
                            rel.to_string_lossy().into_owned()
                        };
                        lines.push(format!("{}\\t{}", pkg_id.name(), rel));
                    }
                }
            }
            lines.sort();
            lines.dedup();
            let index_path = path.join(".cargo-manifest-index-experiment");
            if let Err(err) = fs::write(&index_path, lines.join("\\n") + "\\n") {
                warn!("failed to write manifest index {}: {}", index_path.display(), err);
            }
'''
new_writer = '''            let mut groups = Vec::<(String, String, Vec<String>)>::new();
            for (pkg_id, packages) in &all_packages {
                let mut rels = Vec::new();
                for pkg in packages {
                    if let Ok(rel) = pkg.root().strip_prefix(path) {
                        let rel = if rel.as_os_str().is_empty() {
                            ".".to_owned()
                        } else {
                            rel.to_string_lossy().into_owned()
                        };
                        if !rels.contains(&rel) {
                            rels.push(rel);
                        }
                    }
                }
                groups.push((pkg_id.to_string(), pkg_id.name().to_string(), rels));
            }
            groups.sort_by(|a, b| a.0.cmp(&b.0));

            let mut lines = vec!["cargo-manifest-index-v2".to_owned()];
            for (_, name, rels) in groups {
                for rel in rels {
                    lines.push(format!("{}\\t{}", name, rel));
                }
            }
            let entry_count = lines.len() - 1;
            lines.push(format!("cargo-manifest-index-end\\t{}", entry_count));
            let contents = lines.join("\\n") + "\\n";
            let index_path = path.join(".cargo-manifest-index-experiment");
            let temp_path = path.join(format!(
                ".cargo-manifest-index-experiment.tmp-{}",
                std::process::id()
            ));
            match fs::write(&temp_path, contents) {
                Ok(()) => {
                    if let Err(err) = fs::rename(&temp_path, &index_path) {
                        if index_path.exists() {
                            let _ = fs::remove_file(&temp_path);
                        } else {
                            warn!(
                                "failed to publish manifest index {}: {}",
                                index_path.display(),
                                err
                            );
                        }
                    }
                }
                Err(err) => warn!(
                    "failed to write manifest index {}: {}",
                    temp_path.display(),
                    err
                ),
            }
'''
assert old_writer in s, "warm index writer shape changed"
s = s.replace(old_writer, new_writer, 1)

path_rs.write_text(s)
print("hardened realistic warm git manifest index variant v2")
