from pathlib import Path

path = Path("src/workspace/parser/mod.rs")
text = path.read_text()

old_import = """use crate::workspace::{\n    GitReference, PackageIdSpec, SourceId, WorkspaceConfig, WorkspaceRootConfig,\n};"""
new_import = """use crate::workspace::{\n    GitReference, PackageIdSpec, SourceId, SourceKind, WorkspaceConfig, WorkspaceRootConfig,\n};"""
assert old_import in text
text = text.replace(old_import, new_import, 1)

old_parse = """    let is_embedded = is_embedded(path);\n    let contents = read_toml_string(path, is_embedded, gctx)?;\n    let document = parse_document(&contents)\n        .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n    let original_toml = deserialize_toml(&document)\n        .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n\n    let document = make_document_owned(document);"""
new_parse = """    let is_embedded = is_embedded(path);\n    let contents = read_toml_string(path, is_embedded, gctx)?;\n\n    let keep_document = is_embedded\n        || match source_id.kind() {\n            SourceKind::Path | SourceKind::LocalRegistry | SourceKind::Directory => true,\n            SourceKind::Git(_) | SourceKind::Registry | SourceKind::SparseRegistry => false,\n        };\n    let cache_experiment = std::env::var_os(\"CARGO_MANIFEST_CACHE_EXPERIMENT\").is_some();\n    let (original_toml, document) = if keep_document {\n        let document = parse_document(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(toml::de::Deserializer::from(document.clone()))\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, Some(make_document_owned(document)))\n    } else if cache_experiment {\n        let original_toml = deserialize_cached_toml(&contents, gctx)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)\n    } else {\n        let deserializer = toml::de::Deserializer::parse(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(deserializer)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)\n    };"""
assert old_parse in text
text = text.replace(old_parse, new_parse, 1)

text = text.replace("                Some(document),\n", "                document,\n", 2)

old_deser = """fn deserialize_toml(\n    document: &toml::Spanned<toml::de::DeTable<'_>>,\n) -> Result<manifest::TomlManifest, toml::de::Error> {\n    let mut unused = BTreeSet::new();\n    let deserializer = toml::de::Deserializer::from(document.clone());\n    let mut document: manifest::TomlManifest = serde_ignored::deserialize(deserializer, |path| {"""
new_deser = """fn deserialize_toml(\n    deserializer: toml::de::Deserializer<'_>,\n) -> Result<manifest::TomlManifest, toml::de::Error> {\n    let mut unused = BTreeSet::new();\n    let mut document: manifest::TomlManifest = serde_ignored::deserialize(deserializer, |path| {"""
assert old_deser in text
text = text.replace(old_deser, new_deser, 1)

needle = """    document._unused_keys = unused;\n    Ok(document)\n}\n\nfn stringify"""
replacement = """    document._unused_keys = unused;\n    Ok(document)\n}\n\n#[derive(serde::Serialize, serde::Deserialize)]\nstruct CachedTomlManifest {\n    manifest: manifest::TomlManifest,\n    unused_keys: BTreeSet<String>,\n}\n\n#[tracing::instrument(skip_all)]\nfn deserialize_cached_toml(\n    contents: &str,\n    gctx: &GlobalContext,\n) -> Result<manifest::TomlManifest, toml::de::Error> {\n    let hash = blake3::hash(contents.as_bytes()).to_hex().to_string();\n    let cache_dir = gctx.home().join(\"manifest-cache-experiment\");\n    let cache_file = cache_dir.join(format!(\"{hash}.json\"));\n\n    if let Ok(bytes) = std::fs::read(cache_file.as_path_unlocked()) {\n        if let Ok(mut cached) = serde_json::from_slice::<CachedTomlManifest>(&bytes) {\n            cached.manifest._unused_keys = cached.unused_keys;\n            return Ok(cached.manifest);\n        }\n    }\n\n    let deserializer = toml::de::Deserializer::parse(contents)?;\n    let manifest = deserialize_toml(deserializer)?;\n    let cached = CachedTomlManifest {\n        unused_keys: manifest._unused_keys.clone(),\n        manifest: manifest.clone(),\n    };\n    if let Ok(bytes) = serde_json::to_vec(&cached) {\n        if std::fs::create_dir_all(cache_dir.as_path_unlocked()).is_ok() {\n            let _ = std::fs::write(cache_file.as_path_unlocked(), bytes);\n        }\n    }\n    Ok(manifest)\n}\n\nfn stringify"""
assert needle in text
text = text.replace(needle, replacement, 1)

path.write_text(text)
