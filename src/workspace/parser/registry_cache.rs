//! On-disk cache of deserialized registry `Cargo.toml` files.
//!
//! Registry crates under `~/.cargo/registry/src` are immutable once unpacked.
//! Warm Cargo invocations still re-parse every dependency manifest. This cache
//! stores the deserialized [`TomlManifest`] beside the package so subsequent
//! runs can skip TOML document parse + serde deserialization.
//!
//! Safety properties:
//! - Only used when [`SourceId::is_registry`].
//! - Validated by a content hash of the on-disk `Cargo.toml`.
//! - Versioned so schema/logic changes invalidate entries.
//! - Corrupt or mismatched entries are ignored and the normal parse path runs.
//! - Failure to write the cache is ignored (optimization only).

use std::path::Path;

use cargo_util::paths;
use cargo_util_schemas::manifest::TomlManifest;
use serde::{Deserialize, Serialize};

use crate::util::hex::hash_u64;

/// Bump when the cache payload format or interpret rules change.
const CACHE_VERSION: u32 = 1;

const CACHE_FILE_NAME: &str = ".cargo-toml-cache";

#[derive(Serialize, Deserialize)]
struct RegistryTomlCache {
    v: u32,
    /// `hash_u64` of the UTF-8 `Cargo.toml` contents.
    content_hash: u64,
    original_toml: TomlManifest,
}

fn cache_path(manifest_path: &Path) -> Option<std::path::PathBuf> {
    Some(manifest_path.parent()?.join(CACHE_FILE_NAME))
}

/// Attempt to load a previously cached deserialized registry manifest.
///
/// Returns `None` when no usable cache entry exists.
pub fn load_original_toml(manifest_path: &Path, contents: &str) -> Option<TomlManifest> {
    let path = cache_path(manifest_path)?;
    let bytes = paths::read_bytes(&path).ok()?;
    let cached: RegistryTomlCache = serde_json::from_slice(&bytes).ok()?;
    if cached.v != CACHE_VERSION {
        return None;
    }
    if cached.content_hash != hash_u64(contents) {
        return None;
    }
    Some(cached.original_toml)
}

/// Persist a deserialized registry manifest for future runs.
pub fn store_original_toml(manifest_path: &Path, contents: &str, original_toml: &TomlManifest) {
    let Some(path) = cache_path(manifest_path) else {
        return;
    };
    let payload = RegistryTomlCache {
        v: CACHE_VERSION,
        content_hash: hash_u64(contents),
        original_toml: original_toml.clone(),
    };
    let Ok(bytes) = serde_json::to_vec(&payload) else {
        return;
    };
    // Best-effort: cache misses are always correct.
    let _ = paths::write_atomic(path, bytes);
}
