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

# The warm index is a complete snapshot produced by Cargo's existing full
# discovery. Track names already materialized in this process separately from
# paths visited while expanding nested path dependencies. Without this, a
# repeated query sees an empty `discovered` map and incorrectly falls back to a
# full recursive scan.
old_fields = '''    /// Paths already parsed through the warm index, including nested path deps.
    indexed_visited: RefCell<HashSet<PathBuf>>,
    gctx: &'gctx GlobalContext,
}'''
new_fields = '''    /// Paths already parsed through the warm index, including nested path deps.
    indexed_visited: RefCell<HashSet<PathBuf>>,
    /// Package names whose complete candidate set has already been materialized.
    indexed_materialized_names: RefCell<HashSet<String>>,
    gctx: &'gctx GlobalContext,
}'''
assert old_fields in s, "warm index fields changed"
s = s.replace(old_fields, new_fields, 1)

old_new = '''            manifest_index: Default::default(),
            indexed_visited: Default::default(),
            gctx,
'''
new_new = '''            manifest_index: Default::default(),
            indexed_visited: Default::default(),
            indexed_materialized_names: Default::default(),
            gctx,
'''
assert old_new in s, "warm index constructor changed"
s = s.replace(old_new, new_new, 1)

# Replace the entire experimental query fast path. The important state machine
# is: complete negative hit -> return; already materialized -> reuse packages;
# first positive hit -> parse every candidate for that name and mark every name
# discovered through nested path deps; bad candidate/index -> current full scan.
query_anchor = "impl<'gctx> Source for RecursivePathSource<'gctx> {"
qpos = s.index(query_anchor)
warm_start = s.index(
    '''        if !self.loaded.get()
            && self.source_id.is_git()
            && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_USE").is_some()
        {''',
    qpos,
)
full_load = s.index("        self.load()?;\n", warm_start)

new_warm = r'''        if !self.loaded.get()
            && self.source_id.is_git()
            && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_USE").is_some()
        {
            if self.manifest_index.borrow().is_none() {
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
                                .strip_prefix("cargo-manifest-index-end\t")
                                .and_then(|value| value.parse::<usize>().ok());
                            if expected_entries != Some(body.len()) {
                                valid = false;
                            }
                            if valid {
                                for line in body {
                                    let Some((name, rel)) = line.split_once('\t') else {
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
                        if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                            eprintln!(
                                "cargo-manifest-index index_load path={} names={}",
                                index_path.display(),
                                index.len(),
                            );
                        }
                        self.manifest_index.replace(Some(index));
                    } else if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                        eprintln!(
                            "cargo-manifest-index invalid_index path={}",
                            index_path.display(),
                        );
                    }
                }
            }

            let package_name = dep.package_name().to_string();

            if self
                .indexed_materialized_names
                .borrow()
                .contains(&package_name)
            {
                if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                    eprintln!(
                        "cargo-manifest-index repeat_materialized_hit query={}",
                        package_name,
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

            let candidates = self
                .manifest_index
                .borrow()
                .as_ref()
                .map(|index| index.get(&package_name).cloned());

            if let Some(candidates) = candidates {
                let Some(candidates) = candidates else {
                    if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                        eprintln!(
                            "cargo-manifest-index negative_hit query={}",
                            package_name,
                        );
                    }
                    return Ok(());
                };

                let mut discovered = HashMap::default();
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

                let discovered_names = discovered
                    .keys()
                    .map(|pkg_id| pkg_id.name().to_string())
                    .collect::<Vec<_>>();
                if index_entry_valid
                    && !discovered_names.iter().any(|name| name == &package_name)
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
                    {
                        let mut materialized = self.indexed_materialized_names.borrow_mut();
                        for name in discovered_names {
                            materialized.insert(name);
                        }
                    }

                    if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                        eprintln!(
                            "cargo-manifest-index candidate_hit query={} candidates={} newly_parsed={} total_parsed={}",
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

                if std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some() {
                    eprintln!(
                        "cargo-manifest-index fallback_bad_candidate query={}",
                        package_name,
                    );
                }
                self.manifest_index.replace(None);
                self.indexed_visited.borrow_mut().clear();
                self.indexed_materialized_names.borrow_mut().clear();
            }
        }

'''
s = s[:warm_start] + new_warm + s[full_load:]

# Make unexpected escalation to the authoritative O(N) source walk directly
# observable in diagnostic runs. This is intentionally behind the stats env var
# so it cannot affect timed measurements.
old_load = '''    pub fn load(&self) -> CargoResult<()> {
        if !self.loaded.get() {
            self.packages
                .replace(read_packages(&self.path, self.source_id, self.gctx)?);
            self.loaded.set(true);
        }

        Ok(())
    }
'''
new_load = '''    pub fn load(&self) -> CargoResult<()> {
        if !self.loaded.get() {
            if self.source_id.is_git()
                && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_USE").is_some()
                && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_STATS").is_some()
            {
                eprintln!(
                    "cargo-manifest-index full_recursive_load path={}",
                    self.path.display(),
                );
            }
            self.packages
                .replace(read_packages(&self.path, self.source_id, self.gctx)?);
            self.loaded.set(true);
        }

        Ok(())
    }
'''
assert old_load in s, "recursive load shape changed"
s = s.replace(old_load, new_load, 1)

# Replace the simple writer inserted by the base experiment with a complete,
# versioned snapshot. Preserve duplicate PackageId ordering within each group
# and publish atomically with a per-process temporary file.
writer_start = s.index(
    '''        if source_id.is_git()
            && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_BUILD").is_some()
        {'''
)
writer_end = s.index("        Ok(all_packages)\n", writer_start)
new_writer = r'''        if source_id.is_git()
            && std::env::var_os("CARGO_GIT_MANIFEST_INDEX_BUILD").is_some()
        {
            let mut groups = Vec::<(String, String, Vec<String>)>::new();
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
                    lines.push(format!("{}\t{}", name, rel));
                }
            }
            let entry_count = lines.len() - 1;
            lines.push(format!("cargo-manifest-index-end\t{}", entry_count));
            let contents = lines.join("\n") + "\n";
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
        }
'''
s = s[:writer_start] + new_writer + s[writer_end:]

path_rs.write_text(s)
print("hardened realistic warm git manifest index variant v3")
