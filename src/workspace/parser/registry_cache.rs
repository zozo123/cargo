//! Faster `Cargo.toml` deserialize path for immutable registry packages.
//!
//! Workspace and path packages keep the full spanned `DeTable` document so
//! diagnostics can point at exact keys. Registry dependencies do not need
//! those spans on the common warm path: skip document construction,
//! `serde_ignored` unused-key tracking, and the owned-table transmute.

use cargo_util_schemas::manifest::TomlManifest;

use crate::util::errors::CargoResult;

/// Deserialize a registry package manifest without building a spanned document.
pub fn deserialize_registry_toml(contents: &str) -> CargoResult<TomlManifest> {
    let original_toml: TomlManifest = toml::from_str(contents).map_err(|e| anyhow::anyhow!(e))?;
    Ok(original_toml)
}
