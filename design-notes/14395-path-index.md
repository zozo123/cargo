# cargo#14395: path-only git manifest index

This branch is a clean design/review surface for the path-index direction explored in `experiment/14395-index-feasibility-e2e`.

## Goal

Avoid the eager O(N) recursive manifest discovery for git sources when resolution only asks for a small subset of package names.

## Proposed cache boundary

Persist only a complete mapping for one git checkout:

```text
package name -> all candidate manifest roots
```

The cache does **not** persist parsed TOML, `Manifest`, `Summary`, dependency/version data, or warning text.

On a positive lookup, the running Cargo parses every candidate manifest for that package name through the normal manifest-reading path, including recursive path-dependency discovery. Duplicate candidates are retained so current duplicate-package selection/warning behavior can still be produced by the running Cargo.

On a complete negative lookup, Cargo can return no candidates without a full scan.

If the index is missing, malformed, incomplete, contains unsafe paths, points at a missing manifest, or the parsed candidates do not actually produce the requested package name, Cargo falls back to the existing full recursive discovery path.

## Why path-only

Keeping only locations makes the persisted data mostly independent of Cargo's manifest parser and diagnostics. Parser behavior and warnings stay with the Cargo binary that is running rather than being serialized into the cache.

The remaining compatibility key is the discovery semantics themselves. A production format should therefore have a small format/discovery epoch that can be bumped when Cargo's recursive discovery rules change, instead of fingerprinting the entire Cargo build.

## Revision scope

Cargo's git checkout cache is already revision-scoped: checkouts live under a short commit-id directory and Cargo validates that the checkout HEAD matches the expected revision before treating it as fresh. A path index stored with that checkout is therefore naturally isolated by revision.

`.cargo-ok` is currently Cargo's checkout-readiness marker. This note intentionally does not prescribe reusing its contents; the production storage location should be agreed separately. The experiment used a separate sidecar so storage policy did not affect the feasibility result.

## Publication / safety

The feasibility implementation writes a versioned complete snapshot and publishes it via temp-file + rename. Readers validate the header/footer entry count and reject absolute/parent/root/prefix paths. Invalid or inconsistent entries fall back to the existing scan.

## Correctness cases already exercised in the feasibility branch

- duplicate package IDs / multiple candidate paths
- hidden packages reachable through normal, build, dev, target-specific, and sub-repository path dependencies
- missing, malformed, truncated, unsafe, partial, wrong-path, and wrong-name cache data
- concurrent cold builders with atomic publication
- revision isolation across two commits
- metadata equivalence against baseline Cargo

Warning experiments also covered selected/unused manifests and duplicate-package warnings. Those tests are evidence for the path-only boundary, but should not be read as proof of byte-for-byte equivalence for every possible diagnostic.

## Performance evidence already collected

For pinned Zed, warm `cargo fetch --locked --offline` saved roughly 46-63 ms on runs around 0.78-1.00 s across three runners (148/150 wins), around 5.5-6.3% end-to-end. The synthetic 2000-manifest / 1-needed case moved from about 143 ms to about 52 ms, close to the exact-path oracle.

The experiment branch contains the benchmark harness and adversarial validation. They are intentionally not copied into this clean review surface.

## Design question for maintainers

Is this checkout-local, path-only cache boundary worth pursuing for cargo#14395, or should the optimization instead be coupled to the broader Cargo.lock / git-subpath design space?
