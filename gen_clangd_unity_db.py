#!/usr/bin/env python3
"""Generate clangd compile_commands.json for unity-style C projects.

This targets projects where a master C/header file directly includes implementation
files, e.g. #include "foo.c". clangd normally treats foo.c as an independent
translation unit when opened, which can cause many false diagnostics. This tool
creates a per-file forced-include preamble containing the master's source prefix
before each active .c include, so clangd sees roughly the same context as the real
unity build.

Conditional compilation is evaluated by the real compiler preprocessor (gcc/cc),
not by a Python reimplementation. Pass the same -D/-I flags used by the build.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

INCLUDE_C_RE = re.compile(r'^\s*#\s*include\s*["<]([^">]+\.c)[">]')
COND_OPEN_RE = re.compile(r'^\s*#\s*(if|ifdef|ifndef)\b')
COND_CLOSE_RE = re.compile(r'^\s*#\s*endif\b')
SENTINEL_RE = re.compile(r'__CLANGD_UNITY_INCLUDE_(\d+)__')


def eprint(*args):
    print(*args, file=sys.stderr)


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate compile_commands.json for C unity builds that #include .c files."
    )
    p.add_argument("master", help="Master .c/.h file containing direct #include \"*.c\" lines")
    p.add_argument("--root", default=".", help="Project root / compilation directory (default: cwd)")
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"), help="Compiler/preprocessor command (default: $CC or gcc)")
    p.add_argument("--cflags", default="", help="Common build flags as one shell-style string, e.g. '-Iinc -DFEATURE=1 -std=gnu99'")
    p.add_argument("--flag", action="append", default=[], help="Additional compiler flag; repeat as needed")
    p.add_argument("--output", default="compile_commands.json", help="Output compilation database path, relative to root unless absolute")
    p.add_argument("--context-dir", default=".clangd-unity", help="Generated context directory, relative to root unless absolute")
    p.add_argument("--all", action="store_true", help="Also generate entries for inactive conditional .c includes. These entries use textual context and may not match the selected build configuration.")
    p.add_argument("--keep-probe", action="store_true", help="Keep the temporary preprocessor probe file for debugging")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def collect_candidates(lines):
    out = []
    for lineno, line in enumerate(lines, 1):
        m = INCLUDE_C_RE.match(line)
        if m:
            out.append({"index": len(out), "line": lineno, "include": m.group(1), "text": line.rstrip("\n")})
    return out


def make_probe(lines, candidates):
    by_line = {c["line"]: c for c in candidates}
    result = []
    for lineno, line in enumerate(lines, 1):
        c = by_line.get(lineno)
        if c is None:
            result.append(line)
        else:
            # #warning is processed only in active preprocessor branches and is
            # supported by old GCC versions including CentOS 7-era GCC.
            result.append('#warning __CLANGD_UNITY_INCLUDE_%d__\n' % c["index"])
    return "".join(result)


def detect_active(cc, flags, master, root, probe_path, probe_text, verbose=False):
    probe_path.write_text(probe_text)
    cmd = [cc, "-E", "-x", "c"] + flags + [str(probe_path)]
    if verbose:
        eprint("probe:", " ".join(shlex.quote(x) for x in cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    active = set(int(m.group(1)) for m in SENTINEL_RE.finditer(proc.stderr or ""))
    if proc.returncode != 0:
        eprint("warning: preprocessor returned %d while detecting active includes." % proc.returncode)
        eprint("         Active includes seen before the error will still be used.")
        if proc.stderr:
            eprint(proc.stderr.rstrip())
    return active


def conditional_depth_before(lines, target_line):
    depth = 0
    for lineno, line in enumerate(lines, 1):
        if lineno >= target_line:
            break
        if COND_OPEN_RE.match(line):
            depth += 1
        elif COND_CLOSE_RE.match(line):
            depth = max(0, depth - 1)
    return depth


def safe_name(master, include_path, index):
    raw = "%04d_%s_%s" % (index, master.stem, include_path)
    return re.sub(r'[^A-Za-z0-9_.-]+', '__', raw) + ".h"


def extract_include_dirs(flags, root):
    dirs = []
    i = 0
    while i < len(flags):
        f = flags[i]
        val = None
        if f == "-I" and i + 1 < len(flags):
            i += 1
            val = flags[i]
        elif f.startswith("-I") and len(f) > 2:
            val = f[2:]
        if val:
            p = Path(val)
            if not p.is_absolute():
                p = (root / p).resolve()
            dirs.append(p)
        i += 1
    return dirs


def resolve_include(master, include_path, root, include_dirs):
    p = Path(include_path)
    if p.is_absolute() and p.exists():
        return p.resolve()
    candidates = [(master.parent / p).resolve(), (root / p).resolve()]
    candidates.extend((d / p).resolve() for d in include_dirs)
    for c in candidates:
        if c.exists():
            return c
    # Preserve a deterministic path even if generated later by the build.
    return (master.parent / p).resolve()


def write_context(path, lines, target_line, master):
    prefix = lines[: target_line - 1]
    depth = conditional_depth_before(lines, target_line)
    with path.open("w") as f:
        f.write("/* AUTO-GENERATED by gen_clangd_unity_db.py; do not edit. */\n")
        f.write("/* Context before %s:%d */\n\n" % (master, target_line))
        f.writelines(prefix)
        if prefix and not prefix[-1].endswith("\n"):
            f.write("\n")
        if depth:
            f.write("\n/* Close conditionals opened in the source prefix. */\n")
            for _ in range(depth):
                f.write("#endif\n")


def main():
    args = parse_args()
    root = Path(args.root).resolve()
    master = Path(args.master)
    if not master.is_absolute():
        master = (root / master).resolve()
    if not master.exists():
        eprint("error: master file not found:", master)
        return 2

    flags = shlex.split(args.cflags) + list(args.flag)
    lines = master.read_text(errors="replace").splitlines(True)
    candidates = collect_candidates(lines)
    if not candidates:
        eprint('error: no direct #include "*.c" candidates found in', master)
        return 2

    context_dir = Path(args.context_dir)
    if not context_dir.is_absolute():
        context_dir = (root / context_dir).resolve()
    context_dir.mkdir(parents=True, exist_ok=True)

    probe_path = context_dir / "__probe__.c"
    probe_text = make_probe(lines, candidates)
    active = detect_active(args.cc, flags, master, root, probe_path, probe_text, args.verbose)
    if not args.keep_probe:
        try:
            probe_path.unlink()
        except OSError:
            pass

    include_dirs = extract_include_dirs(flags, root)
    selected = candidates if args.all else [c for c in candidates if c["index"] in active]

    if not selected:
        eprint("error: no active .c includes were detected.")
        eprint("       Pass the same -D/-I flags as the real build via --cflags/--flag.")
        return 3

    commands = []
    manifest = []
    for c in selected:
        source = resolve_include(master, c["include"], root, include_dirs)
        ctx = context_dir / safe_name(master, c["include"], c["index"])
        write_context(ctx, lines, c["line"], master)

        command_args = [args.cc] + flags + ["-I" + str(master.parent), "-include", str(ctx), "-c", str(source)]
        commands.append({
            "directory": str(root),
            "file": str(source),
            "arguments": command_args,
        })
        manifest.append({
            "source": str(source),
            "include": c["include"],
            "master_line": c["line"],
            "active": c["index"] in active,
            "context": str(ctx),
        })

    output = Path(args.output)
    if not output.is_absolute():
        output = (root / output).resolve()
    with output.open("w") as f:
        json.dump(commands, f, indent=2)
        f.write("\n")

    manifest_path = context_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump({
            "master": str(master),
            "compiler": args.cc,
            "flags": flags,
            "active_candidate_indexes": sorted(active),
            "entries": manifest,
        }, f, indent=2)
        f.write("\n")

    print("Generated %d clangd entries" % len(commands))
    print("  database:", output)
    print("  contexts:", context_dir)
    print("  manifest:", manifest_path)
    inactive = len(candidates) - len(active)
    if inactive:
        print("  inactive conditional .c includes:", inactive)
        if not args.all:
            print("  (inactive entries omitted; use --all only for diagnostic experiments)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
