# clangd-unity-db

Generate `compile_commands.json` for C projects that build a **unity/amalgamated translation unit** by directly including implementation files:

```c
/* master.c or master.h */
#include "common.c"
#include "device.c"
#include "test.c"
```

When `clangd` opens `test.c` directly, it normally parses it as an independent translation unit. Declarations or macros established by earlier included `.c` files are then missing, which can cause many false `undeclared function` diagnostics.

`clangd-unity-db` creates a clangd-only preamble for each included `.c` file and a matching `compile_commands.json` entry. Production sources are not modified.

## Requirements

- Python 3.6+
- GCC/compatible C preprocessor available as `gcc` (or select another command with `--cc`)
- clangd

No Python packages are required.

## Quick start

Run from the project root:

```bash
python3 gen_clangd_unity_db.py path/to/master.h \
  --cflags='-std=gnu99 -Iinclude -I/path/to/vendor/include -DFEATURE_A=1'
```

Generated files:

```text
compile_commands.json
.clangd-unity/
  0000_...h
  0001_...h
  manifest.json
```

Restart clangd/Neovim after generation:

```vim
:LspRestart
```

Check a problematic source directly:

```bash
clangd --check=/absolute/path/to/problem.c
```

The log should say `Compile command from CDB` rather than `Generic fallback command`.

## Conditional includes

Conditional compilation is evaluated using the **real compiler preprocessor**. The script does not try to reimplement `#if` expressions in Python.

Example:

```c
#include "common.c"

#ifdef FEATURE_A
#include "feature_a.c"
#else
#include "feature_b.c"
#endif
```

Generate the database for `FEATURE_A`:

```bash
python3 gen_clangd_unity_db.py master.c --cflags='-DFEATURE_A -Iinclude'
```

Generate it for the other configuration:

```bash
python3 gen_clangd_unity_db.py master.c --cflags='-Iinclude'
```

Only `.c` includes active in the selected preprocessor configuration are emitted by default. This is intentional: a compilation database represents one build configuration.

`--all` can emit inactive entries too, but their context may not represent a real build and is mainly useful for experiments.

## Why the generated context works

For:

```c
#include "common.c"
#include "device.c"
#include "test.c"
```

`test.c` gets a generated preamble equivalent to the master source **up to, but not including,** `#include "test.c"`:

```c
#include "common.c"
#include "device.c"
```

Its database entry then effectively tells clangd:

```bash
gcc <build flags> -include .clangd-unity/<context>.h -c test.c
```

This lets `test.c` see declarations, macros, headers, and definitions that exist earlier in the real unity translation unit.

The generator also adds the master file directory to the clangd command so relative quoted includes copied into the generated preamble keep resolving as they did from the original master file.

The generated preamble also closes any still-open `#if/#ifdef/#ifndef` nesting at the target line so a target inside an active conditional branch remains preprocessable.

## Build flags

Pass the flags that affect preprocessing and parsing, especially:

- `-I...`
- `-D...`
- `-include ...`
- `-std=...`

Examples:

```bash
python3 gen_clangd_unity_db.py src/all.h \
  --cflags='-std=gnu99 -Iinc -I../common/inc -DDEVICE_NAND -DTESTER_T5835'
```

Or repeat `--flag` when shell quoting is inconvenient:

```bash
python3 gen_clangd_unity_db.py src/all.h \
  --flag=-std=gnu99 \
  --flag=-Iinc \
  --flag=-DDEVICE_NAND
```

## Makefile integration

A simple target can regenerate the database whenever build flags change:

```make
clangd-db:
	python3 tools/gen_clangd_unity_db.py src/all.h \
	  --cflags='$(CPPFLAGS) $(CFLAGS)'
```

If `CFLAGS` contains linker-only flags, remove them or pass only preprocessing/compile flags.

## `.clangd`

Keep `.clangd` for clangd-specific adjustments rather than duplicating all Makefile flags. For example:

```yaml
CompileFlags:
  Remove:
    - -W*
```

The generated compilation database should carry the real `-I` and `-D` options whenever possible.

## Limitations

- Direct literal includes such as `#include "foo.c"` and `#include <foo.c>` are detected. Macro-generated include names are not currently mapped to source paths.
- The selected configuration must preprocess far enough to evaluate the master file. Missing headers should be fixed by supplying the same `-I`/`-D` flags as the actual build.
- A compilation database models one build configuration at a time. Mutually exclusive conditional branches generally require regenerating the DB with the corresponding defines.
- This is designed for clangd analysis, not to replace the real build system.

## Useful options

```text
--root DIR          project root / compile directory
--cc COMMAND        compiler/preprocessor command (default: $CC or gcc)
--cflags STRING     common build flags
--flag FLAG         additional flag; repeatable
--output FILE       compile_commands.json destination
--context-dir DIR   generated preamble directory
--all               include inactive .c candidates too
--keep-probe        retain preprocessor probe for debugging
--verbose           print the probe command
```

## License

MIT
