#!/usr/bin/env python3
"""Generate a clangd-only database from real GCC unity include traces (Python 3.6+)."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

MARKER = re.compile(r'^# (\d+) ("(?:[^"\\]|\\.)*")(.*)$')
DIRECTIVE = re.compile(r'^\s*#\s*(\w+)\b')
PAIRED = {'-I', '-D', '-U', '-include', '-imacros', '-isystem', '-iquote',
          '-idirafter', '-isysroot', '--sysroot', '-iprefix', '-iwithprefix',
          '-iwithprefixbefore', '-B', '-x', '-std', '-target', '--target',
          '-Xpreprocessor', '-Xclang', '-arch'}


def absolute(value, root):
    # Keep lexical paths: resolving symlinks can change quoted include lookup.
    return Path(os.path.abspath(os.path.join(str(root), str(value))))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=str(path.parent), delete=False) as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        temp = f.name
    os.replace(temp, str(path))


def response_args(args, root, depth=0):
    if depth > 10:
        raise ValueError('Response files nested too deeply')
    result = []
    for arg in args:
        if arg.startswith('@'):
            result.extend(response_args(shlex.split(absolute(arg[1:], root).read_text()), root, depth + 1))
        else:
            result.append(arg)
    return result


def clean_flags(args, source, root):
    """Remove build outputs; never invoke a shell or retain dependency side effects."""
    args = response_args(args, root)
    result = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in ('-o', '-MF', '-MT', '-MQ', '-MJ'):
            if i == len(args):
                raise ValueError('Missing argument for ' + a)
            i += 1
        elif a in ('-c', '-S', '-E', '-M', '-MM', '-MD', '-MMD', '-MP', '-MG', '-P', '-dI', '-dD'):
            continue
        elif any(a.startswith(p) and a != p for p in ('-o', '-MF', '-MT', '-MQ', '-MJ')):
            continue
        elif a.startswith(('-Wp,', '-save-temps', '-fpreprocessed', '-fdirectives-only', '-ivfsoverlay', '-include-pch')):
            raise ValueError('Unsupported preprocessing/output option: ' + a)
        elif a in PAIRED:
            if i == len(args):
                raise ValueError('Missing argument for ' + a)
            result.extend((a, args[i]))
            i += 1
        elif not a.startswith('-'):
            if absolute(a, root) != source:
                raise ValueError('Unexpected input (one C source per command required): ' + a)
        else:
            result.append(a)
    return result


def logical_lines(text):
    """C line splicing and comment removal, retaining physical line ranges."""
    parts = text.splitlines(True)
    block = False
    i = 0
    while i < len(parts):
        start = i + 1
        s = parts[i]
        i += 1
        while s.endswith('\\\n') and i < len(parts):
            s = s[:-2] + parts[i]
            i += 1
        out = []
        j = 0
        quote = None
        while j < len(s):
            if block:
                end = s.find('*/', j)
                if end < 0:
                    break
                block = False
                out.append(' ')
                j = end + 2
            elif quote:
                out.append(s[j])
                if s[j] == '\\' and j + 1 < len(s):
                    j += 1
                    out.append(s[j])
                elif s[j] == quote:
                    quote = None
                j += 1
            elif s.startswith('/*', j):
                block = True
                out.append(' ')
                j += 2
            elif s.startswith('//', j):
                break
            else:
                if s[j] in ('"', "'"):
                    quote = s[j]
                out.append(s[j])
                j += 1
        yield start, i, ''.join(out)


def prefix(path, include_line, keep_include):
    text = path.read_text(encoding='utf-8')
    depth = 0
    for start, end, logical in logical_lines(text):
        m = DIRECTIVE.match(logical)
        name = m.group(1) if m else ''
        if name == 'line' or logical.lstrip().startswith('# '):
            raise ValueError('Explicit line remapping is unsupported: ' + str(path))
        if start <= include_line <= end:
            if name not in ('include', 'include_next'):
                raise ValueError('Trace does not match source include at %s:%s' % (path, include_line))
            cut = end if keep_include else start - 1
            return ''.join(text.splitlines(True)[:cut]) + '\n' + '#endif\n' * depth
        if name in ('if', 'ifdef', 'ifndef'):
            depth += 1
        elif name == 'endif':
            depth -= 1
    raise ValueError('Cannot locate include at %s:%s' % (path, include_line))


def trace_includes(output, root, source):
    current = None
    line = 0
    stack = []
    pending = {}
    targets = []
    entered = {}
    for raw in output.splitlines():
        m = MARKER.match(raw)
        if not m:
            if re.match(r'^\s*#\s*include(?:_next)?\b', raw):
                pending[current] = line
            line += 1
            continue
        number = int(m.group(1))
        name = json.loads(m.group(2))
        path = None if name.startswith('<') else str(absolute(name, root))
        flags = m.group(3).split()
        if '1' in flags and path:
            entered[path] = entered.get(path, 0) + 1
            edge = (current, pending.pop(current, None), path)
            stack.append(edge)
            if path.endswith('.c') and path != str(source):
                chain = list(stack)
                # Compiler-injected headers have no source include location.
                if not chain or chain[0][0] != str(source) or any(e[1] is None for e in chain):
                    raise ValueError('C include outside the root source include chain: ' + path)
                if entered[path] > 1 or any(entered.get(e[0], 0) > 1 for e in chain):
                    raise ValueError('Repeated include context is ambiguous: ' + path)
                targets.append((path, chain))
        elif '2' in flags:
            if stack:
                stack.pop()
        current, line = path, number
    return targets


def capture(args):
    command = args.build_command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise ValueError('Use --capture build-commands.json -- make -B [target]')
    root = Path(args.root).resolve()
    output = absolute(args.capture, root)
    with tempfile.TemporaryDirectory(prefix='unity-capture-') as log:
        wrapper = [sys.executable, str(Path(__file__).resolve()), '--record', log, '--'] + shlex.split(args.cc)
        cc = ' '.join(shlex.quote(s) for s in wrapper)
        # GNU make command-line assignments reach recursive make invocations.
        build = command + ['CC=' + cc]
        print('Running build:', ' '.join(shlex.quote(s) for s in build), flush=True)
        result = subprocess.call(build, cwd=str(root))
        if result:
            raise ValueError('Build failed (%d); capture database was not replaced' % result)
        records = []
        for p in sorted(Path(log).glob('*.json')):
            records.extend(json.loads(p.read_text()))
        if not records:
            raise ValueError('No C compilations captured. Use make -B; Makefile must honor $(CC).')
        write_json(output, records)
    print('Captured %d compilation command(s): %s' % (len(records), output))


def record():
    log = Path(sys.argv[2])
    command = sys.argv[4:]
    status = subprocess.call(command)
    if status == 0:
        expanded = response_args(command[1:], Path.cwd())
        if '-c' in expanded:
            sources = []
            skip = False
            for a in expanded:
                if skip:
                    skip = False
                elif a in PAIRED or a in ('-o', '-MF', '-MT', '-MQ'):
                    skip = True
                elif not a.startswith('-') and a.endswith('.c'):
                    sources.append(a)
            write_json(log / (uuid.uuid4().hex + '.json'), [
                {'directory': str(Path.cwd()), 'file': str(absolute(s, Path.cwd())),
                 'arguments': command} for s in sources])
    return status


def generate(args):
    root = Path(args.root).resolve()
    source = absolute(args.master, root) if args.master else None
    compiler = shlex.split(args.cc)
    flags = shlex.split(args.cflags) + args.flag
    if args.build_db:
        db = absolute(args.build_db, root)
        if db == absolute(args.output, root):
            raise ValueError('Input build database and clangd output must be different files')
        entries = json.loads(db.read_text())
        selected = []
        for entry in entries:
            directory = absolute(entry['directory'], db.parent)
            file = absolute(entry['file'], directory)
            if source is None or file == source:
                selected.append((entry, directory, file))
        if len(selected) != 1:
            raise ValueError('Select exactly one build command with the root .c path; found %d' % len(selected))
        entry, root, source = selected[0]
        command = entry.get('arguments') or shlex.split(entry['command'])
        compiler = [command[0]]
        # Captures use the real compiler, never the recording wrapper.
        flags = command[1:] + flags
    if source is None or not source.is_file():
        raise ValueError('Provide the actual compiled root .c file (e.g. main.c)')
    flags = clean_flags(flags, source, root)
    if not compiler:
        raise ValueError('Compiler command is empty')
    command = compiler + flags + ['-E', '-dI', '-x', 'c', str(source)]
    if args.verbose:
        print('Trace:', ' '.join(shlex.quote(s) for s in command))
    proc = subprocess.run(command, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    if proc.returncode:
        raise ValueError('GCC preprocessing failed; database not replaced:\n' + proc.stderr)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end='')
    targets = trace_includes(proc.stdout, root, source)
    if not targets:
        raise ValueError('No active nested .c includes found')
    # Validate all source prefixes before publishing any new database.
    prepared = []
    for target, chain in targets:
        prepared.append((target, [(parent, prefix(Path(parent), line, i < len(chain) - 1))
                                  for i, (parent, line, child) in enumerate(chain)]))
    destination = absolute(args.context_dir, Path(args.root).resolve())
    destination.mkdir(parents=True, exist_ok=True)
    # Immutable per-run paths keep the previous database valid on failure.
    run = Path(tempfile.mkdtemp(prefix='run-', dir=str(destination)))
    commands = [{'directory': str(root), 'file': str(source),
                 'arguments': compiler + flags + ['-x', 'c', '-c', str(source)]}]
    manifest = []
    for index, (target, contexts) in enumerate(prepared):
        mappings = []
        for level, (original, content) in enumerate(contexts):
            external = run / ('%04d-%02d.h' % (index, level))
            external.write_text(content, encoding='utf-8')
            mappings.append({'type': 'file', 'name': original, 'external-contents': str(external)})
        overlay = run / ('%04d-overlay.json' % index)
        write_json(overlay, {'version': 0, 'use-external-names': False, 'roots': mappings})
        commands.append({'directory': str(root), 'file': target,
                         'arguments': compiler + flags + ['-ivfsoverlay', str(overlay),
                         '-include', str(source), '-x', 'c', '-c', target]})
        manifest.append({'source': target, 'overlay': str(overlay),
                         'include_chain': [list(e) for e in targets[index][1]]})
    write_json(run / 'manifest.json', {'root_source': str(source), 'entries': manifest})
    output = absolute(args.output, Path(args.root).resolve())
    write_json(output, commands)
    print('Generated %d included-C entries plus the root entry: %s' % (len(targets), output))
    print('Contexts:', run)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('master', nargs='?', help='Actual compiled root source, e.g. main.c')
    p.add_argument('--root', default='.', help='Project/build working directory')
    p.add_argument('--cc', default=os.environ.get('CC', 'gcc'), help='GCC compiler command')
    p.add_argument('--cflags', default='')
    p.add_argument('--flag', action='append', default=[])
    p.add_argument('--build-db', help='Input compilation database with real build commands')
    p.add_argument('--output', default='compile_commands.json')
    p.add_argument('--context-dir', default='.clangd-unity')
    p.add_argument('--capture', help='Capture GNU make C compiler calls to this database')
    p.add_argument('--verbose', action='store_true')
    argv = sys.argv[1:]
    build_command = []
    if '--' in argv:
        split = argv.index('--')
        argv, build_command = argv[:split], argv[split + 1:]
    args = p.parse_args(argv)
    args.build_command = build_command
    try:
        if args.capture:
            capture(args)
        else:
            if build_command:
                raise ValueError('Trailing build command requires --capture')
            generate(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('error:', exc, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(record() if sys.argv[1:2] == ['--record'] else main())
