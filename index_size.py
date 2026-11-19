#!/usr/bin/env python3
"""Measure persisted symbol indexes left by completed benchmark processes."""

import argparse
import json
from pathlib import Path


def measure(run_dir, tool):
    workspace = run_dir / "workspace"
    if tool == "clangd":
        files = list((workspace / ".cache/clangd/index").glob("*.idx"))
    elif tool == "clice":
        cache = workspace / ".clice/cache"
        files = list(cache.glob("v*/index/**/index.mdb"))
    else:
        raise ValueError(f"Unknown server {tool}")
    sizes = [file.stat() for file in files if file.is_file()]
    return {
        "index_files": len(sizes),
        "index_bytes": sum(info.st_size for info in sizes),
        "index_allocated_bytes": sum(info.st_blocks * 512 for info in sizes),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="The output directory from run.py")
    args = parser.parse_args()
    results = {}
    for path in sorted(args.output.glob("*/result.json")):
        summary = json.loads(path.read_text())["summary"]
        results[path.parent.name] = measure(path.parent, summary["tool"])
    if not results:
        parser.error("No finished run records found")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
