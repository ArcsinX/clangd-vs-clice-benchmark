#!/usr/bin/env python3
"""Measure fresh LSP indexes for the LLVM Clang Tools compilation database entries on macOS."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import re
import shlex
import sys
import shutil
import signal
import statistics
import subprocess
import threading
import time

MIB = 1024**2
SCOPES = {
    "clangd-tidy": (Path("clang-tools-extra/clangd"), Path("clang-tools-extra/clang-tidy")),
    "clangd": (Path("clang-tools-extra/clangd"),),
    "extra-tools": (Path("clang-tools-extra"), Path("llvm/lib/Support")),
    "support": (Path("llvm/lib/Support"),),
}
TOKENS = {"clangd": "backgroundIndexProgress", "clice": "clice/backgroundIndex"}

def inputs(llvm, database, scope, limit):
    commands = json.loads(database.read_text())
    rows = {}
    for row in commands:
        file = (Path(row["directory"]) / row["file"]).resolve()
        if any(file.is_relative_to(llvm / prefix) for prefix in SCOPES[scope]) and file.is_file():
            rows.setdefault(file, row)
    return sorted(rows.items())[:limit]

def setup(llvm, build, rows, workspace, jobs, sdk, compiler):
    staged_build = workspace / "build"
    for folder in ("llvm/lib/Support", "llvm/include", "clang/include",
                   "clang-tools-extra", "libc", "third-party"):
        source = llvm / folder
        if source.is_dir():
            shutil.copytree(source, workspace / folder)
    for folder in ("include", "tools/clang/include"):
        source = build / folder
        if source.is_dir():
            shutil.copytree(source, staged_build / folder)
    for folder in ("lib/Support", "tools/clang/tools/extra"):
        generated = build / folder
        (staged_build / folder).mkdir(parents=True)
        for header in generated.rglob("*"):
            if header.is_file() and header.suffix in {".h", ".inc", ".def", ".hxx"}:
                target = staged_build / header.relative_to(build)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(header, target)
    commands = []
    for file, row in rows:
        target = workspace / file.relative_to(llvm)
        words = row.get("arguments") or shlex.split(row["command"])
        words = words[1:]
        if "-include-pch" in words:
            index = words.index("-include-pch")
            # CMake's precompiled header was built by a different Clang. Keep
            # the related textual forced include, which both servers can read.
            if words[index - 1] == "-Xclang" and words[index + 1] == "-Xclang":
                del words[index - 1:index + 3]
            else:
                del words[index:index + 2]
        flags = [word.replace(str(build) + "/", str(staged_build) + "/")
                 .replace(str(llvm) + "/", str(workspace) + "/") for word in words]
        directory = Path(row["directory"]).resolve()
        if directory.is_relative_to(build):
            directory = staged_build / directory.relative_to(build)
        commands.append({"directory": str(directory), "file": str(target),
                         "arguments": [str(compiler), "-isysroot", sdk, *flags]})
    (workspace / "compile_commands.json").write_text(json.dumps(commands))
    (workspace / "clice.toml").write_text(
        f"[workers]\nbackground = {jobs}\nmax = {jobs}\n"
        "foreground = 1\nbackground_delay_ms = 0\n"
    )
    return Path(commands[0]["file"])

def libproc():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    return lib

def process_tree(lib, root):
    pending = [root]
    samples = []
    visited = set()
    while pending:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        data = (ctypes.c_uint64 * 64)()
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(data)) == 0:
            # rusage_info_v2 starts with a 16-byte UUID and six uint64 fields.
            samples.append((pid, data[8], data[9]))
        children = (ctypes.c_int * 256)()
        count = lib.proc_listchildpids(pid, children, ctypes.sizeof(children))
        pending.extend(child for child in children[:max(count, 0)] if child)
    return samples

def receive(pipe, messages):
    try:
        while line := pipe.readline():
            headers = {}
            while line.strip():
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
                line = pipe.readline()
            messages.put(json.loads(pipe.read(int(headers["content-length"]))))
    finally:
        messages.put(None)

def run(tool, binary, workspace, source, jobs, out, expected):
    cmd = [str(binary), "serve"] if tool == "clice" else [
        str(binary), f"-j={jobs}", "--enable-config=false", "--background-index",
        "--background-index-priority=normal", "--log=info",
        f"--compile-commands-dir={workspace}",
    ]
    tmp = workspace / "scratch"
    tmp.mkdir()
    env = dict(os.environ, TMPDIR=str(tmp), XDG_CACHE_HOME=str(workspace / "xdg-cache"),
               CLANG_NO_DEFAULT_CONFIG="1")
    env.pop("CLANGD_FLAGS", None)
    env["CLICE_LOG_LEVEL"] = "info"
    started = time.monotonic()
    samples = []
    events = queue.Queue()
    stop = threading.Event()
    lib = libproc()
    with (out / "stderr.log").open("wb") as log, (out / "progress.jsonl").open("w") as progress:
        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        reader = threading.Thread(target=receive, args=(proc.stdout, events), daemon=True)
        reader.start()

        def sample():
            while not stop.is_set():
                at = time.monotonic() - started
                tree = process_tree(lib, proc.pid)
                if tree and tree[0][0] == proc.pid:
                    samples.append([at, sum(x[1] for x in tree), sum(x[2] for x in tree),
                                    tree[0][1], len(tree) - 1])
                stop.wait(0.2)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()

        def send(**message):
            body = json.dumps(dict(jsonrpc="2.0", **message)).encode()
            proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            proc.stdin.flush()

        def message():
            packet = events.get(timeout=1800)
            if packet is None:
                raise RuntimeError(f"{tool} closed LSP stdout. See {out / 'stderr.log'}")
            return packet

        try:
            send(id=1, method="initialize", params={
                "processId": os.getpid(), "rootUri": workspace.as_uri(),
                "workspaceFolders": [{"uri": workspace.as_uri(), "name": "LLVM indexing benchmark"}],
                "capabilities": {"window": {"workDoneProgress": True}},
            })
            while (packet := message()).get("id") != 1:
                pass
            if "error" in packet:
                raise RuntimeError(packet)
            trigger = time.monotonic() - started
            send(method="initialized", params={})
            send(method="textDocument/didOpen", params={"textDocument": {
                "uri": source.as_uri(), "languageId": "cpp" if source.suffix != ".c" else "c",
                "version": 1, "text": source.read_text(),
            }})
            observed_total = last_report = milestone = 0
            while True:
                packet = message()
                if "method" in packet and "id" in packet:
                    send(id=packet["id"], result=None)
                if packet.get("method") != "$/progress":
                    continue
                params = packet["params"]
                if params["token"] != TOKENS[tool]:
                    continue
                value = params["value"]
                at = time.monotonic() - started
                progress.write(json.dumps([at, value]) + "\n")
                progress.flush()
                if match := re.search(r"(\d+)/(\d+)", value.get("message", "")):
                    last_report, total = map(int, match.groups())
                    observed_total = max(total, observed_total)
                    if last_report >= milestone + max(1, len(expected) // 5):
                        milestone = last_report
                        print(f"{tool} {last_report}/{total} after {at-trigger:.1f} s", flush=True)
                if value["kind"] == "end":
                    end = at
                    break
            stop.set()
            sampler.join()
            send(id=2, method="shutdown")
            while (packet := message()).get("id") != 2:
                if "method" in packet and "id" in packet:
                    send(id=packet["id"], result=None)
            send(method="exit")
            if proc.wait(timeout=30):
                raise RuntimeError(f"{tool} exited with {proc.returncode}")
        finally:
            stop.set()
            sampler.join()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    indexing = [x for x in samples if trigger <= x[0] <= end]
    if not indexing:
        raise RuntimeError("No valid memory samples")
    logs = (out / "stderr.log").read_text(errors="replace")
    if tool == "clangd":
        ok = re.findall(r"\] Indexed (.*?) \(\d+ symbols, \d+ refs, \d+ files\)", logs)
        bad = re.findall(r"\] Indexing .*? failed:", logs)
    else:
        ok = re.findall(r"\] Indexed (.*?) in \d+ ms", logs)
        bad = re.findall(r"\] (?:.*does not fit in memory|Index failed|Indexing .*? failed|Failed to index).*", logs, re.I)
    indexed_units = {
        str(Path(item).relative_to(workspace)) if Path(item).is_absolute()
        and Path(item).is_relative_to(workspace) else item for item in ok
    }
    result = {
        "tool": tool, "seconds": round(end - trigger, 3), "requested_units": len(expected),
        "completed_units_in_log": len(set(expected) & indexed_units), "failed_units_in_log": len(bad),
        "shard_write_errors": logs.count("Failed to write background-index shard"),
        "memory_worker_kills": logs.count("killed to free memory"),
        "missing_units": sorted(set(expected) - indexed_units),
        "lsp_progress_total": observed_total, "lsp_final_report_completed": last_report,
        "lsp_end": value.get("message"), "sample_count": len(indexing),
        "peak_rss_mib": round(max(x[1] for x in indexing) / MIB, 2),
        "average_rss_mib": round(statistics.mean(x[1] for x in indexing) / MIB, 2),
        "peak_footprint_mib": round(max(x[2] for x in indexing) / MIB, 2),
        "average_footprint_mib": round(statistics.mean(x[2] for x in indexing) / MIB, 2),
        "peak_parent_rss_mib": round(max(x[3] for x in indexing) / MIB, 2),
        "max_descendants": max(x[4] for x in indexing),
    }
    records = {"summary": result, "clock_start": trigger, "clock_end": end,
               "sample_columns": ["monotonic_s", "tree_rss_bytes", "tree_footprint_bytes",
                                  "server_rss_bytes", "descendants"], "samples": samples}
    (out / "result.json").write_text(json.dumps(records, separators=(",", ":")) + "\n")
    print(json.dumps(result), flush=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--llvm", type=Path, default=Path("../llvm-project"))
    p.add_argument("--build-dir", type=Path, help="Defaults to <llvm>/build")
    p.add_argument("--database", type=Path, help="Defaults to <build-dir>/compile_commands.json")
    p.add_argument("--compiler", type=Path, help="Defaults to <build-dir>/bin/clang++")
    p.add_argument("--clangd", type=Path, required=True)
    p.add_argument("--clice", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--scope", choices=SCOPES, default="clangd-tidy")
    p.add_argument("--limit", type=int)
    p.add_argument("--order", nargs="+", choices=TOKENS, default=["clangd", "clice", "clice", "clangd", "clangd", "clice"])
    args = p.parse_args()
    if sys.platform != "darwin":
        p.error("This harness uses macOS libproc")
    llvm = args.llvm.resolve()
    build = (args.build_dir or llvm / "build").resolve()
    out = args.out.resolve()
    if args.jobs < 1 or (args.limit is not None and args.limit < 1):
        p.error("Jobs and any input limit must be positive")
    out.mkdir(parents=True)
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    compiler = (args.compiler or build / "bin/clang++").resolve()
    database = (args.database or build / "compile_commands.json").resolve()
    rows = inputs(llvm, database, args.scope, args.limit)
    if not rows:
        p.error("The LLVM compilation database has no matching source files")
    expected = [str(file.relative_to(llvm)) for file, _ in rows]
    (out / "translation-units.txt").write_text("\n".join(expected) + "\n")
    for i, tool in enumerate(args.order, 1):
        directory = out / f"{i}-{tool}"
        directory.mkdir()
        workspace = directory / "workspace"
        source = setup(llvm, build, rows, workspace, args.jobs, sdk, compiler)
        run(tool, getattr(args, tool).resolve(), workspace, source, args.jobs, directory, expected)

if __name__ == "__main__":
    main()
