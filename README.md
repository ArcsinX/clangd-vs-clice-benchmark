# clangd vs clice indexing benchmark

This macOS harness runs two language servers on the translation units in
`clang-tools-extra/clangd` and `clang-tools-extra/clang-tidy` from an existing
LLVM CMake compilation database. It stages the source files,
headers, compilation database, and per-run caches in an output directory you
choose. The LLVM checkout and build can live anywhere.

The accompanying personal comparison is at <https://arcsinx.github.io/>.

## Requirements

- macOS and Python 3.10 or newer. The sampler uses macOS libproc.
- Xcode Command Line Tools or Xcode with an active macOS SDK.
- A configured and built LLVM checkout with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`,
  including generated Clang and Clang Tools Extra headers. For the published
  input snapshot, use LLVM revision
  [bd49d77ae3c0](https://github.com/llvm/llvm-project/commit/bd49d77ae3c07c30bcceff27d27b4ef756f8b1b5).
- The official macOS builds of [clangd 23.1.0](https://github.com/clangd/clangd/releases/tag/23.1.0)
  and [clice 1.0.0](https://github.com/clice-io/clice/releases/tag/v1.0.0).

## Run

```sh
python3 run.py \
  --llvm ../llvm-project \
  --clangd ./bin/clangd \
  --clice ./bin/clice \
  --out ./tmp/clangd-tidy
```

The output directory must be new. By default the harness expects
`build/compile_commands.json` and `build/bin/clang++` inside the LLVM checkout.
Use `--build-dir` for a build elsewhere, or `--database` and `--compiler` to
override those paths individually. The generated LLVM headers are staged from
the build directory. The harness removes binary PCH inputs from the CMake
commands and keeps the corresponding forced textual includes. The SDK path comes from `xcrun`. Use `--jobs` to change the worker
ceiling, `--limit` for a smoke run and `--order` to set the order of the tools.
The default order is clangd, clice, clice, clangd, clangd, clice.

The published workload has 723 existing unique source paths, with 245 from
clangd and 478 from clang-tidy. One stale compilation database row refers to a
missing clangd source file. The translation-unit manifest records the exact
selection.

## Read the results

- Each elapsed time covers the LSP session from sending `initialized` and
  `didOpen` through the background-index progress end. Fresh on-disk caches are
  used for every run. Operating-system file caches are left alone.
- Every approximately 200 ms, the harness walks the server's process tree and
  adds up resident memory across live processes. It stores the sum,
  a separate physical-footprint sum, the parent RSS and the descendant count.
  A shared page can appear in multiple processes' RSS. The sampled peak can
  miss a brief spike.
- The per-run mean and peak come from the samples inside the timing window.
  Arithmetic means use the samples taken at approximately uniform intervals.
- Compare the unique completed unit count and missing-unit list against the
  manifest even if the tool emits a progress end. clice's host-memory policy
  can throttle workers or skip a unit. clangd's progress also counts a
  compilation-database loading task.
- `stderr.log` and `progress.jsonl` remain local, and the published
  `result.json` traces contain relative unit names and numerical samples.
