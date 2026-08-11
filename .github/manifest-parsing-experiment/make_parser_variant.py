#!/usr/bin/env python3
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
path = root / "src/workspace/parser/mod.rs"
s = path.read_text()

if mode == "base":
    raise SystemExit(0)

old_import = """use crate::workspace::{\n    GitReference, PackageIdSpec, SourceId, WorkspaceConfig, WorkspaceRootConfig,\n};"""
new_import = """use crate::workspace::{\n    GitReference, PackageIdSpec, SourceId, SourceKind, WorkspaceConfig, WorkspaceRootConfig,\n};"""
assert old_import in s
s = s.replace(old_import, new_import, 1)

old_read = """    let is_embedded = is_embedded(path);\n    let contents = read_toml_string(path, is_embedded, gctx)?;\n    let document = parse_document(&contents)\n        .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n    let original_toml = deserialize_toml(&document)\n        .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n\n    let document = make_document_owned(document);"""

if mode == "git-only":
    match_arms = """            SourceKind::Path\n            | SourceKind::LocalRegistry\n            | SourceKind::Directory\n            | SourceKind::Registry\n            | SourceKind::SparseRegistry => true,\n            SourceKind::Git(_) => false,"""
elif mode == "registry-only":
    match_arms = """            SourceKind::Path\n            | SourceKind::LocalRegistry\n            | SourceKind::Directory\n            | SourceKind::Git(_) => true,\n            SourceKind::Registry | SourceKind::SparseRegistry => false,"""
else:
    match_arms = """            SourceKind::Path | SourceKind::LocalRegistry | SourceKind::Directory => true,\n            SourceKind::Git(_) | SourceKind::Registry | SourceKind::SparseRegistry => false,"""

if mode == "spanned-no-retain":
    remote = """        let document = parse_document(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(toml::de::Deserializer::from(document.clone()))\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)"""
elif mode == "hash-only":
    remote = """        let _cache_key = blake3::hash(contents.as_bytes());\n        let deserializer = toml::de::Deserializer::parse(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(deserializer)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)"""
elif mode == "hash-read-direct":
    remote = """        let hash = blake3::hash(contents.as_bytes()).to_hex().to_string();\n        let cache_dir = gctx.home().join(\"manifest-cache-experiment\");\n        let cache_file = cache_dir.join(format!(\"{hash}.json\"));\n        if let Ok(bytes) = std::fs::read(cache_file.as_path_unlocked()) {\n            std::hint::black_box(bytes);\n        }\n        let deserializer = toml::de::Deserializer::parse(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(deserializer)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)"""
else:
    remote = """        let deserializer = toml::de::Deserializer::parse(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(deserializer)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, None)"""

new_read = f"""    let is_embedded = is_embedded(path);\n    let contents = read_toml_string(path, is_embedded, gctx)?;\n\n    let keep_document = is_embedded\n        || match source_id.kind() {{\n{match_arms}\n        }};\n    let (original_toml, document) = if keep_document {{\n        let document = parse_document(&contents)\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        let original_toml = deserialize_toml(toml::de::Deserializer::from(document.clone()))\n            .map_err(|e| emit_toml_diagnostic(e.into(), &contents, path, gctx))?;\n        (original_toml, Some(make_document_owned(document)))\n    }} else {{\n{remote}\n    }};"""
assert old_read in s
s = s.replace(old_read, new_read, 1)

assert s.count("                Some(document),") >= 2
s = s.replace("                Some(document),", "                document,", 2)

old_deser = """fn deserialize_toml(\n    document: &toml::Spanned<toml::de::DeTable<'_>>,\n) -> Result<manifest::TomlManifest, toml::de::Error> {\n    let mut unused = BTreeSet::new();\n    let deserializer = toml::de::Deserializer::from(document.clone());"""
new_deser = """fn deserialize_toml(\n    deserializer: toml::de::Deserializer<'_>,\n) -> Result<manifest::TomlManifest, toml::de::Error> {\n    let mut unused = BTreeSet::new();"""
assert old_deser in s
s = s.replace(old_deser, new_deser, 1)

path.write_text(s)
print(f"generated parser variant: {mode}")
