# cargo#14395 manifest-loading experiment results

This is an experiment record, not a proposed Cargo design.

## Reproducibility

- Fork branch: `experiment/14395-grid-e2e`
- Experiment code commit: `40ec20532709a9d9943e1f158f303fe7b640a480`
- GitHub Actions run: `31485032354`
- Pinned Cargo upstream: `9184583d2dc29ac1e23c6304f7281fd3941bb1bb`
- Pinned Zed: `5e03f2d387e629237c10a4e4fed881b64abd8fea`
- Cargo binaries built in release mode with Rust `1.97.0`
- Zed workload uses Rust `1.95.0`
- Timed command: `cargo metadata --format-version 1 --locked --offline`
- Network fetch and source checkout happen before the timed region.
- All compared variants produced semantically identical metadata in their correctness gates.

The previous grid failure was a harness error: `cargo fetch` ran from the Cargo checkout rather than the Zed checkout. This run fixes the cwd and all jobs complete successfully.

Artifacts from run `31485032354`:

- `oracle-index-zed`: `9099453903`
- `source-kind-1`: `9099444074`
- `source-kind-2`: `9099453353`
- `source-kind-3`: `9099450794`
- `parser-work`: `9099450213`
- `cache-ablation`: `9099449795`
- `synthetic-scaling`: `9099334357`
- frozen variant binaries/diffs: `9099288693`

## Experiment variants

- `base`: pinned upstream Cargo.
- `full`: the parser behavior represented by cargo#17296: keep the owned spanned document for local/user-controlled source kinds, direct-deserialize remote git/registry/sparse-registry manifests without retaining it.
- `git-only`: apply that direct-deserialize behavior to git only.
- `registry-only`: apply it to remote registry/sparse registry only.
- `spanned-no-retain`: still build the spanned TOML representation but do not retain an owned copy for remote sources.
- `hash-only`: `full` plus content hashing for every eligible manifest.
- `hash-read-direct`: `full` plus hashing and a per-manifest cache-file read, while still parsing TOML normally.
- `cache-json`: the first persistent per-manifest cache prototype: content hash + one JSON file containing serialized `TomlManifest` per manifest.
- `oracle`: an intentionally unrealistic upper-bound experiment. An external oracle writes the exact git package roots needed by the already-resolved metadata. Cargo then skips recursive git-source discovery and fully parses only those roots with the normal manifest code. It is not a production cache design.

## 1. Synthetic git-source scaling

The synthetic repository has `N` package-shaped directories. `U` is how many packages the consumer actually asks for.

| N | U | base median | cargo#17296/full | oracle | oracle vs base |
|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 68.965 ms | 67.940 ms | 67.722 ms | -1.8% |
| 100 | 1 | 79.949 ms | 79.576 ms | 68.866 ms | -13.9% |
| 500 | 1 | 122.310 ms | 119.814 ms | 68.006 ms | -44.4% |
| 1000 | 1 | 174.700 ms | 171.810 ms | 68.206 ms | -61.0% |
| 2000 | 1 | **286.004 ms** | **279.787 ms** | **69.399 ms** | **-75.7%** |
| 1000 | 1000 | 255.214 ms | 249.121 ms | 207.195 ms | -18.8% |

At `N=2000, U=1`:

- total N-dependent tax vs `N=1`: **217.040 ms**
- cargo#17296/full recovers **6.218 ms** (**2.17%** of total runtime)
- oracle recovers **216.605 ms** (**75.73%** of total runtime and 99.8% of the N-dependent tax)
- oracle is only **0.435 ms** above the `N=1` baseline

### Matched-tree control

To separate directory traversal from manifest processing, the control keeps the same directory/file count and the same manifest-like payload bytes, but only one file is actually named `Cargo.toml`; the other files are inert.

| directories | base, all are Cargo.toml | base, only one Cargo.toml |
|---:|---:|---:|
| 100 | 79.949 ms | 73.704 ms |
| 500 | 122.310 ms | 94.936 ms |
| 1000 | 174.700 ms | 120.390 ms |
| 2000 | **286.004 ms** | **169.757 ms** |

At N=2000 the decomposition is approximately:

- generic recursive tree/discovery tax: **100.792 ms**
- additional manifest-specific tax: **116.248 ms**
- manifest-specific share of the N-dependent tax: **53.6%**

So cargo#14395 is not only a TOML parser problem. Recursive discovery itself is a large part of the cost. Exact package paths remove effectively both components in this low-U synthetic case.

## 2. Pinned Zed: oracle upper bound

30 balanced rounds, rotating all ordering permutations.

| variant | median |
|---|---:|
| base | **1289.483 ms** |
| cargo#17296/full | **1236.499 ms** |
| oracle git roots | **1227.492 ms** |

Paired results:

- base - full: **57.236 ms mean**, 95% bootstrap CI **[50.220, 64.133] ms**, full faster in **30/30** rounds
- base - oracle: **67.346 ms mean**, 95% CI **[60.163, 75.000] ms**, oracle faster in **30/30** rounds
- full - oracle: oracle ahead by **10.111 ms mean**, 95% CI **[1.898, 18.605] ms**, oracle faster in **20/30** rounds

Important: `full` and `oracle` are separate variants, not stacked. The 10 ms comparison is not a combined-design estimate.

### How much can Zed skip?

From baseline metadata, the oracle identified:

- **31 git checkouts**
- **127 raw `Cargo.toml` files** across those checkouts
- **73 selected manifest roots** needed by the resolved metadata
- raw skip fraction: **42.5%**

Examples with substantial slack:

- `livekit-rust-sdks`: 30 raw manifests, 7 selected
- `notify`: 8 -> 2
- `proptest`: 8 -> 2
- `alacritty`: 5 -> 1
- `python-environment-tools`: 27 -> 26, so not every source benefits

`127` is a raw filesystem count, not Cargo's exact discoverable-manifest set; use it as workload context rather than a semantic Cargo count.

### Zed syscall shape

One `strace -f -c` sample for base vs oracle:

- total syscalls: **74,475 -> 66,753** (~10.4% fewer)
- `getdents64`: **3,193 -> 1,138** (~64% fewer)
- `statx`: **28,077 -> 25,841**
- `openat`: **11,456 -> 10,257**
- `read`: **9,894 -> 9,861** (nearly unchanged)

The oracle benefit is therefore strongly associated with avoiding recursive directory enumeration / metadata / open work, not just reducing bytes read.

## 3. What cargo#17296 is actually improving on Zed

Three independent runners, each 24 balanced rounds.

Paired mean `base - variant`:

| runner | full | git-only | registry-only |
|---:|---:|---:|---:|
| 1 | 44.431 ms | 3.922 ms | 44.030 ms |
| 2 | 54.410 ms | 4.193 ms | 49.733 ms |
| 3 | 55.471 ms | 3.558 ms | 55.810 ms |
| simple mean | **51.44 ms** | **3.89 ms** | **49.86 ms** |

The git-only confidence intervals cross zero on these runners. The registry-only result tracks the full result closely.

Conclusion: **the Zed win from cargo#17296 is overwhelmingly a remote-registry improvement, not the answer to cargo#14395's git-source problem.**

## 4. Parser / retained-document decomposition

24 balanced Zed rounds:

| variant | median | paired mean saving vs base | 95% CI |
|---|---:|---:|---:|
| base | 1357.586 ms | - | - |
| spanned-no-retain | 1323.338 ms | 35.116 ms | [22.728, 47.571] ms |
| full | 1310.195 ms | 53.600 ms | [44.584, 62.087] ms |

5 RSS samples per variant, median maximum RSS:

- base: **334,844 KiB (~327.0 MiB)**
- spanned-no-retain: **294,912 KiB (~288.0 MiB)**
- full: **294,760 KiB (~287.9 MiB)**

So dropping the retained owned spanned TOML representation is worth roughly **39 MiB RSS** on this Zed metadata workload. Direct parsing gives additional CPU improvement but essentially no further RSS reduction.

This is the clean story for cargo#17296: remote registry manifests do not need retained owned spanned documents, and avoiding them has both time and memory impact.

## 5. Persistent per-manifest cache ablation

20 balanced Zed rounds, with `full` as the baseline:

| variant | median | paired mean overhead vs full |
|---|---:|---:|
| full | **1242.982 ms** | - |
| hash-only | 1252.137 ms | **+11.892 ms**, CI [2.799, 21.451] |
| hash + cache-file read + direct TOML parse | 1273.090 ms | **+26.558 ms**, CI [17.957, 35.071] |
| warm JSON parsed-manifest cache | 1296.642 ms | **+54.548 ms**, CI [43.017, 65.563] |

Warm JSON cache footprint:

- **1,652 files**
- **7,519,915 bytes (~7.52 MB)**

The syscall delta explains much of the loss:

- total syscalls: 73,891 -> 82,529
- `statx`: +1,652
- `openat`: +1,652
- `read`: +3,304

That corresponds directly to one extra per-manifest cache lookup/open and two reads per entry.

Conclusion: **stop the content-addressed one-file-per-manifest cache direction.** Hashing already has measurable cost; individual cache files add substantial filesystem overhead; JSON decoding makes it worse; and this design still keeps the eager discovery walk that cargo#14395 needs to avoid.

This result does not prove that every possible binary manifest representation loses. It does show that optimizing representation while preserving eager per-manifest discovery attacks the smaller axis of the problem on these workloads.

## Decision

### Separate the two performance questions

1. **cargo#17296: remote registry representation / ownership**
   - repeatable ~4% Zed metadata improvement across these runs
   - almost all of that benefit is registry-side
   - about 39 MiB lower maximum RSS in the parser decomposition
   - should be discussed as its own optimization, not as the solution to cargo#14395

2. **cargo#14395: eager git-source discovery / indexing**
   - synthetic `N=2000,U=1`: exact package roots remove 216.6 ms of a 217.0 ms N-dependent tax
   - pinned Zed: exact package roots save 67.3 ms paired mean, CI 60.2-75.0 ms
   - Zed has meaningful skip opportunity: 73 selected roots among 127 raw git `Cargo.toml`s
   - syscall evidence shows recursive directory enumeration is a major part of the opportunity

### Actual next design question

The experiments justify discussing a **revision-scoped git package name/path index that can avoid eager recursive discovery**.

The best next *prototype* if maintainers agree is a warm index attached to the git checkout (the `.cargo-ok` direction already listed in cargo#14395):

- after a normal successful full scan, store `package name -> [relative manifest roots]`
- preserve duplicate names
- key validity to the immutable git revision plus an explicit index-format/behavior version
- on a warm `query(dep)`, look up candidate paths and run the normal full manifest parser only on those candidates
- retain those loaded `Package`s for later `download(PackageId)`
- APIs that really need every package can fall back to the normal full scan
- corrupt/missing/stale index must transparently fall back to full discovery
- do not hash every manifest; the git revision already identifies checkout contents
- benchmark cold index construction and warm reuse separately

Do **not** implement that as an upstream PR yet. cargo#14395 is `S-needs-design`; the oracle is only an upper-bound result. First show maintainers the evidence and ask whether they want the next experiment to use a checkout-local `.cargo-ok` index or the broader Cargo.lock/subpath direction being discussed in cargo#11858.

### Why not jump directly to Cargo.lock?

Cargo.lock/subpath may be the cleaner long-term architecture and overlaps with cargo#11858, but it changes SourceId / PackageId / package-id-spec semantics and may require lockfile format work. That is a much larger design commitment than is needed to validate the warm-index path for existing projects.

### Stop conditions

- No more CBOR / JSON per-manifest cache work unless maintainers specifically ask for it.
- No production `.cargo-ok` implementation until maintainers choose the direction.
- Do not present cargo#17296 as the fix for cargo#14395.
- Keep cargo#17296 and cargo#14395 evidence separate when discussing results.
