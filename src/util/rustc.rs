use crate::util::data_structures::HashMap;
use std::env;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::Context as _;
use cargo_util::{ProcessBuilder, ProcessError, paths};
use filetime::FileTime;
use serde::{Deserialize, Serialize};
use tracing::{debug, info, warn};

use crate::compiler::apply_env_config;
use crate::util::interning::InternedString;
use crate::util::{CargoResult, GlobalContext, StableHasher};

/// Information on the `rustc` executable
#[derive(Debug)]
pub struct Rustc {
    /// The location of the exe
    pub path: PathBuf,
    /// An optional program that will be passed the path of the rust exe as its first argument, and
    /// rustc args following this.
    pub wrapper: Option<PathBuf>,
    /// An optional wrapper to be used in addition to `rustc.wrapper` for workspace crates
    pub workspace_wrapper: Option<PathBuf>,
    /// Verbose version information (the output of `rustc -vV`)
    pub verbose_version: String,
    /// The rustc version (`1.23.4-beta.2`), this comes from `verbose_version`.
    pub version: semver::Version,
    /// The host triple (arch-platform-OS), this comes from `verbose_version`.
    pub host: InternedString,
    /// The rustc full commit hash, this comes from `verbose_version`.
    pub commit_hash: Option<String>,
    cache: Mutex<Cache>,
}

impl Rustc {
    /// Runs the compiler at `path` to learn various pieces of information about
    /// it, with an optional wrapper.
    ///
    /// If successful this function returns a description of the compiler along
    /// with a list of its capabilities.
    #[tracing::instrument(skip(gctx))]
    pub fn new(
        path: PathBuf,
        wrapper: Option<PathBuf>,
        workspace_wrapper: Option<PathBuf>,
        rustup_rustc: &Path,
        cache_location: Option<PathBuf>,
        shared_cache_location: Option<PathBuf>,
        gctx: &GlobalContext,
    ) -> CargoResult<Rustc> {
        let mut cache = Cache::load(
            wrapper.as_deref(),
            workspace_wrapper.as_deref(),
            &path,
            rustup_rustc,
            cache_location,
            shared_cache_location,
            gctx,
        );

        let mut cmd = ProcessBuilder::new(&path)
            .wrapped(workspace_wrapper.as_ref())
            .wrapped(wrapper.as_deref());
        apply_env_config(gctx, &mut cmd)?;
        cmd.env(crate::CARGO_ENV, gctx.cargo_exe()?);
        cmd.arg("-vV");
        // Unlike target-info probes, this output cannot depend on workspace-relative flags.
        let verbose_version = cache.cached_output(&cmd, 0, true)?.0;

        let extract = |field: &str| -> CargoResult<&str> {
            verbose_version
                .lines()
                .find_map(|l| l.strip_prefix(field))
                .ok_or_else(|| {
                    anyhow::format_err!(
                        "`rustc -vV` didn't have a line for `{}`, got:\n{}",
                        field.trim(),
                        verbose_version
                    )
                })
        };

        let host = extract("host: ")?.into();
        let version = semver::Version::parse(extract("release: ")?).with_context(|| {
            format!(
                "rustc version does not appear to be a valid semver version, from:\n{}",
                verbose_version
            )
        })?;
        let commit_hash = extract("commit-hash: ").ok().map(|hash| {
            // Possible commit-hash values from rustc are SHA hex string and "unknown". See:
            // * https://github.com/rust-lang/rust/blob/531cb83fc/src/bootstrap/src/utils/channel.rs#L73
            // * https://github.com/rust-lang/rust/blob/531cb83fc/compiler/rustc_driver_impl/src/lib.rs#L911-L913
            #[cfg(debug_assertions)]
            if hash != "unknown" {
                debug_assert!(
                    hash.chars().all(|ch| ch.is_ascii_hexdigit()),
                    "commit hash must be a hex string, got: {hash:?}"
                );
                debug_assert!(
                    hash.len() == 40 || hash.len() == 64,
                    "hex string must be generated from sha1 or sha256 (i.e., it must be 40 or 64 characters long)\ngot: {hash:?}"
                );
            }
            hash.to_string()
        });

        Ok(Rustc {
            path,
            wrapper,
            workspace_wrapper,
            verbose_version,
            version,
            host,
            commit_hash,
            cache: Mutex::new(cache),
        })
    }

    /// Gets a process builder set up to use the found rustc version, with a wrapper if `Some`.
    pub fn process(&self) -> ProcessBuilder {
        let mut cmd = ProcessBuilder::new(self.path.as_path()).wrapped(self.wrapper.as_ref());
        cmd.retry_with_argfile(true);
        cmd
    }

    /// Gets a process builder set up to use the found rustc version, with a wrapper if `Some`.
    pub fn workspace_process(&self) -> ProcessBuilder {
        let mut cmd = ProcessBuilder::new(self.path.as_path())
            .wrapped(self.workspace_wrapper.as_ref())
            .wrapped(self.wrapper.as_ref());
        cmd.retry_with_argfile(true);
        cmd
    }

    pub fn process_no_wrapper(&self) -> ProcessBuilder {
        let mut cmd = ProcessBuilder::new(&self.path);
        cmd.retry_with_argfile(true);
        cmd
    }

    /// Gets the output for the given command.
    ///
    /// This will return the cached value if available, otherwise it will run
    /// the command and cache the output.
    ///
    /// `extra_fingerprint` is extra data to include in the cache fingerprint.
    /// Use this if there is other information about the environment that may
    /// affect the output that is not part of `cmd`.
    ///
    /// Returns a tuple of strings `(stdout, stderr)`.
    pub fn cached_output(
        &self,
        cmd: &ProcessBuilder,
        extra_fingerprint: u64,
    ) -> CargoResult<(String, String)> {
        self.cache
            .lock()
            .unwrap()
            .cached_output(cmd, extra_fingerprint, false)
    }
}

/// It is a well known fact that `rustc` is not the fastest compiler in the
/// world.  What is less known is that even `rustc --version --verbose` takes
/// about a hundred milliseconds! Because we need compiler version info even
/// for no-op builds, we cache it here, based on compiler's mtime and rustup's
/// current toolchain.
///
/// <https://github.com/rust-lang/cargo/issues/5315>
/// <https://github.com/rust-lang/rust/issues/49761>
#[derive(Debug)]
struct Cache {
    cache_location: Option<PathBuf>,
    shared: Option<SharedCache>,
    dirty: bool,
    data: CacheData,
}

#[derive(Debug)]
struct SharedCache {
    root: PathBuf,
    rustc_fingerprint: u64,
}

#[derive(Serialize, Deserialize, Debug, Default)]
struct CacheData {
    rustc_fingerprint: u64,
    outputs: HashMap<u64, Output>,
    successes: HashMap<u64, bool>,
}

#[derive(Serialize, Deserialize, Debug)]
struct Output {
    success: bool,
    status: String,
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

impl Cache {
    fn load(
        wrapper: Option<&Path>,
        workspace_wrapper: Option<&Path>,
        rustc: &Path,
        rustup_rustc: &Path,
        cache_location: Option<PathBuf>,
        shared_cache_location: Option<PathBuf>,
        gctx: &GlobalContext,
    ) -> Cache {
        match rustc_fingerprint(wrapper, workspace_wrapper, rustc, rustup_rustc, gctx) {
            Ok(fingerprint) if cache_location.is_some() || shared_cache_location.is_some() => {
                let shared = shared_cache_location
                    .filter(|_| fingerprint.shareable)
                    .map(|root| SharedCache {
                        root,
                        rustc_fingerprint: fingerprint.hash,
                    });
                let empty = CacheData {
                    rustc_fingerprint: fingerprint.hash,
                    outputs: HashMap::default(),
                    successes: HashMap::default(),
                };
                let mut dirty = true;
                let data = match cache_location.as_deref() {
                    Some(cache_location) => match read(cache_location) {
                        Ok(data) => {
                            if data.rustc_fingerprint == fingerprint.hash {
                                debug!("reusing existing rustc info cache");
                                dirty = false;
                                data
                            } else {
                                debug!("different compiler, creating new rustc info cache");
                                empty
                            }
                        }
                        Err(e) => {
                            debug!("failed to read rustc info cache: {}", e);
                            empty
                        }
                    },
                    None => empty,
                };
                return Cache {
                    cache_location,
                    shared,
                    dirty,
                    data,
                };

                fn read(path: &Path) -> CargoResult<CacheData> {
                    let json = paths::read(path)?;
                    Ok(serde_json::from_str(&json)?)
                }
            }
            fingerprint => {
                if let Err(e) = fingerprint {
                    warn!("failed to calculate rustc fingerprint: {}", e);
                }
                debug!("rustc info cache disabled");
                Cache {
                    cache_location: None,
                    shared: None,
                    dirty: false,
                    data: CacheData::default(),
                }
            }
        }
    }

    fn cached_output(
        &mut self,
        cmd: &ProcessBuilder,
        extra_fingerprint: u64,
        use_shared: bool,
    ) -> CargoResult<(String, String)> {
        let key = process_fingerprint(cmd, extra_fingerprint);
        if let std::collections::hash_map::Entry::Vacant(e) = self.data.outputs.entry(key) {
            let shared_output = if use_shared {
                self.shared.as_ref().and_then(|shared| shared.read(key))
            } else {
                None
            };
            let (output, cache_shared) = if let Some(output) = shared_output {
                (output, false)
            } else {
                debug!("rustc info cache miss");
                debug!("running {}", cmd);
                let output = cmd.output()?;
                let stdout = String::from_utf8(output.stdout)
                    .map_err(|e| anyhow::anyhow!("{}: {:?}", e, e.as_bytes()))
                    .with_context(|| format!("`{}` didn't return utf8 output", cmd))?;
                let stderr = String::from_utf8(output.stderr)
                    .map_err(|e| anyhow::anyhow!("{}: {:?}", e, e.as_bytes()))
                    .with_context(|| format!("`{}` didn't return utf8 output", cmd))?;
                (
                    Output {
                        success: output.status.success(),
                        status: if output.status.success() {
                            String::new()
                        } else {
                            cargo_util::exit_status_to_string(output.status)
                        },
                        code: output.status.code(),
                        stdout,
                        stderr,
                    },
                    use_shared,
                )
            };
            if cache_shared && output.success {
                if let Some(shared) = &self.shared {
                    shared.write(key, &output);
                }
            }
            e.insert(output);
            self.dirty = true;
        } else {
            debug!("rustc info cache hit");
        }
        let output = &self.data.outputs[&key];
        if output.success {
            Ok((output.stdout.clone(), output.stderr.clone()))
        } else {
            Err(ProcessError::new_raw(
                &format!("process didn't exit successfully: {}", cmd),
                output.code,
                &output.status,
                Some(output.stdout.as_ref()),
                Some(output.stderr.as_ref()),
            )
            .into())
        }
    }
}

impl SharedCache {
    fn path(&self, key: u64) -> PathBuf {
        self.root
            .join("v1")
            .join(format!("{:016x}", self.rustc_fingerprint))
            .join(format!("{key:016x}.json"))
    }

    fn read(&self, key: u64) -> Option<Output> {
        let path = self.path(key);
        match paths::read(&path).and_then(|json| Ok(serde_json::from_str(&json)?)) {
            Ok(output @ Output { success: true, .. }) => {
                debug!("shared rustc probe cache hit");
                Some(output)
            }
            Ok(_) => {
                debug!("ignoring failed output in shared rustc probe cache");
                None
            }
            Err(e) => {
                debug!("shared rustc probe cache miss: {}", e);
                None
            }
        }
    }

    fn write(&self, key: u64, output: &Output) {
        debug_assert!(output.success);
        let path = self.path(key);
        let result = (|| -> CargoResult<()> {
            paths::create_dir_all(path.parent().unwrap())?;
            let json = serde_json::to_string(output)?;
            paths::write_atomic(&path, json)
        })();
        match result {
            Ok(()) => info!("updated shared rustc probe cache"),
            Err(e) => warn!("failed to update shared rustc probe cache: {}", e),
        }
    }
}

impl Drop for Cache {
    fn drop(&mut self) {
        if !self.dirty {
            return;
        }
        if let Some(ref path) = self.cache_location {
            let json = serde_json::to_string(&self.data).unwrap();
            match paths::write(path, json.as_bytes()) {
                Ok(()) => info!("updated rustc info cache"),
                Err(e) => warn!("failed to update rustc info cache: {}", e),
            }
        }
    }
}

struct RustcFingerprint {
    hash: u64,
    shareable: bool,
}

fn rustc_fingerprint(
    wrapper: Option<&Path>,
    workspace_wrapper: Option<&Path>,
    rustc: &Path,
    rustup_rustc: &Path,
    gctx: &GlobalContext,
) -> CargoResult<RustcFingerprint> {
    let mut hasher = StableHasher::new();

    let hash_exe = |hasher: &mut _, path| -> CargoResult<()> {
        let path = paths::resolve_executable(path)?;
        path.hash(hasher);

        let meta = paths::metadata(&path)?;
        meta.len().hash(hasher);

        // Often created and modified are the same, but not all filesystems support the former,
        // and distro reproducible builds may clamp the latter, so we try to use both.
        FileTime::from_creation_time(&meta).hash(hasher);
        FileTime::from_last_modification_time(&meta).hash(hasher);
        Ok(())
    };

    hash_exe(&mut hasher, rustc)?;
    if let Some(wrapper) = wrapper {
        hash_exe(&mut hasher, wrapper)?;
    }
    if let Some(workspace_wrapper) = workspace_wrapper {
        hash_exe(&mut hasher, workspace_wrapper)?;
    }

    // Rustup can change the effective compiler without touching
    // the `rustc` binary, so we try to account for this here.
    // If we see rustup's env vars, we mix them into the fingerprint,
    // but we also mix in the mtime of the actual compiler (and not
    // the rustup shim at `~/.cargo/bin/rustup`), because `RUSTUP_TOOLCHAIN`
    // could be just `stable-x86_64-unknown-linux-gnu`, i.e, it could
    // not mention the version of Rust at all, which changes after
    // `rustup update`.
    //
    // If we don't see rustup env vars, but it looks like the compiler
    // is managed by rustup, we conservatively bail out.
    let resolved_rustc = paths::resolve_executable(rustc)?;
    let maybe_rustup = rustup_rustc == resolved_rustc;
    let mut shareable = false;
    match (
        maybe_rustup,
        gctx.get_env("RUSTUP_HOME"),
        gctx.get_env("RUSTUP_TOOLCHAIN"),
    ) {
        (_, Ok(rustup_home), Ok(rustup_toolchain)) => {
            debug!("adding rustup info to rustc fingerprint");
            rustup_toolchain.hash(&mut hasher);
            rustup_home.hash(&mut hasher);
            let real_rustc = Path::new(&rustup_home)
                .join("toolchains")
                .join(rustup_toolchain)
                .join("bin")
                .join("rustc")
                .with_extension(env::consts::EXE_EXTENSION);
            paths::mtime(&real_rustc)?.hash(&mut hasher);
            shareable = wrapper.is_none()
                && workspace_wrapper.is_none()
                && (maybe_rustup || resolved_rustc == real_rustc);
        }
        (true, _, _) => anyhow::bail!("probably rustup rustc, but without rustup's env vars"),
        _ => (),
    }

    Ok(RustcFingerprint {
        hash: Hasher::finish(&hasher),
        shareable,
    })
}

fn process_fingerprint(cmd: &ProcessBuilder, extra_fingerprint: u64) -> u64 {
    let mut hasher = StableHasher::new();
    extra_fingerprint.hash(&mut hasher);
    cmd.get_args().for_each(|arg| arg.hash(&mut hasher));
    let mut env = cmd.get_envs().iter().collect::<Vec<_>>();
    env.sort_unstable();
    env.hash(&mut hasher);
    Hasher::finish(&hasher)
}

#[cfg(test)]
mod tests {
    use super::{Output, SharedCache};

    #[test]
    fn shared_cache_ignores_failed_output() {
        let temp = tempfile::tempdir().unwrap();
        let cache = SharedCache {
            root: temp.path().to_owned(),
            rustc_fingerprint: 1,
        };
        let path = cache.path(2);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            path,
            serde_json::to_string(&Output {
                success: false,
                status: "exit status: 1".into(),
                code: Some(1),
                stdout: String::new(),
                stderr: "transient failure".into(),
            })
            .unwrap(),
        )
        .unwrap();

        assert!(cache.read(2).is_none());
    }

    #[test]
    fn shared_cache_concurrent_writes_are_valid() {
        let temp = tempfile::tempdir().unwrap();
        std::thread::scope(|scope| {
            for _ in 0..8 {
                scope.spawn(|| {
                    SharedCache {
                        root: temp.path().to_owned(),
                        rustc_fingerprint: 1,
                    }
                    .write(
                        2,
                        &Output {
                            success: true,
                            status: String::new(),
                            code: Some(0),
                            stdout: "rustc output".into(),
                            stderr: String::new(),
                        },
                    );
                });
            }
        });

        let output = SharedCache {
            root: temp.path().to_owned(),
            rustc_fingerprint: 1,
        }
        .read(2)
        .unwrap();
        assert_eq!(output.stdout, "rustc output");
    }
}
