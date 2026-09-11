import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'gen_clangd_unity_db.py'


class UnityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='unity test ')
        self.root = Path(self.temp.name)
        self.put('src/main.c', '#define FROM_MAIN 7\ntypedef int number;\n#include "nested/project.h"\n')
        self.put('src/nested/project.h', '#ifndef PROJECT_H\n#define PROJECT_H\n#include "../common.h"\n#include "../a.c"\n/*\n#if IGNORED_COMMENT\n*/\n#if SELECT == 1\n#define NEXT "../b.c"\n#include \\\n NEXT\n#elif SELECT == 2\n#include "../c.c"\n#else\n#include "../d.c"\n#endif\n#endif\n')
        self.put('src/common.h', '#pragma once\nstruct item { number x; };\n')
        self.put('src/a.c', '#define SELECT 1\nstatic number prior(void) { return FROM_MAIN; }\n')
        self.put('src/b.c', '#include "common.h"\nnumber answer(void) { struct item i = {prior()}; return i.x; }\n')
        self.put('src/c.c', '#include "common.h"\nnumber alternate(void) { return prior(); }\n')
        self.put('src/d.c', 'number fallback(void) { return FROM_MAIN; }\n')

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, content):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def run_generator(self, *args, **kwargs):
        p = subprocess.run([sys.executable, str(SCRIPT)] + list(args), cwd=str(self.root),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if kwargs.get('fail'):
            self.assertNotEqual(p.returncode, 0, p.stdout)
        else:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def database(self):
        return json.loads((self.root / 'compile_commands.json').read_text())

    def check_clang(self):
        if not shutil.which('clang'):
            return
        for entry in self.database():
            p = subprocess.run(['clang'] + entry['arguments'][1:] + ['-fsyntax-only', '-Werror=implicit-function-declaration'],
                               cwd=entry['directory'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_nested_macros_and_original_paths(self):
        self.run_generator('src/main.c')
        self.assertEqual([Path(e['file']).name for e in self.database()], ['main.c', 'a.c', 'b.c'])
        self.check_clang()
        self.assertTrue((self.root / 'src/main.c').read_text().startswith('#define FROM_MAIN'))

    def test_elif_configuration(self):
        self.put('src/a.c', '#define SELECT 2\nstatic number prior(void) { return FROM_MAIN; }\n')
        self.run_generator('src/main.c')
        self.assertEqual([Path(e['file']).name for e in self.database()], ['main.c', 'a.c', 'c.c'])
        self.check_clang()

    def test_else_configuration(self):
        self.put('src/a.c', '#define SELECT 3\n')
        self.run_generator('src/main.c')
        self.assertEqual([Path(e['file']).name for e in self.database()], ['main.c', 'a.c', 'd.c'])
        self.check_clang()

    def test_failed_probe_preserves_database(self):
        self.run_generator('src/main.c')
        before = (self.root / 'compile_commands.json').read_bytes()
        self.put('src/a.c', '#include "missing.h"\n')
        self.run_generator('src/main.c', fail=True)
        self.assertEqual(before, (self.root / 'compile_commands.json').read_bytes())

    def test_duplicate_c_rejected(self):
        self.put('src/main.c', '#include "a.c"\n#include "a.c"\n')
        p = self.run_generator('src/main.c', fail=True)
        self.assertIn('ambiguous', p.stderr)

    def test_capture_make_and_reuse_flags(self):
        self.put('Makefile', 'all:\n\t$(CC) -std=gnu99 -DCAPTURED=1 -MMD -MF build.d -c src/main.c -o main.o\n')
        self.run_generator('--capture', 'build-commands.json', '--', 'make', '-B')
        self.run_generator('src/main.c', '--build-db', 'build-commands.json')
        for entry in self.database():
            self.assertIn('-DCAPTURED=1', entry['arguments'])
            self.assertNotIn('-MMD', entry['arguments'])
            self.assertNotIn('main.o', entry['arguments'])
        self.check_clang()

    def test_build_db_relative_directory_response_file(self):
        self.put('build/flags.rsp', '-std=gnu99 -DRESPONSE=1 -c ../src/main.c -o main.o')
        self.put('build/input.json', json.dumps([{'directory': '.', 'file': '../src/main.c',
                                                'arguments': ['gcc', '@flags.rsp']}]))
        self.run_generator('src/main.c', '--build-db', 'build/input.json')
        self.assertIn('-DRESPONSE=1', self.database()[1]['arguments'])
        self.check_clang()

    @unittest.skipUnless(shutil.which('clangd'), 'clangd unavailable')
    def test_clangd_real_error_still_reported(self):
        self.run_generator('src/main.c')
        def check():
            return subprocess.run(['clangd', '--check=' + str(self.root / 'src/b.c'),
                                   '--compile-commands-dir=' + str(self.root)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        ok = check()
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn('0 errors', ok.stderr)
        self.put('src/b.c', 'number answer(void) { return missing_symbol; }\n')
        bad = check()
        self.assertNotEqual(bad.returncode, 0, bad.stderr)
        self.assertIn('missing_symbol', bad.stderr)

    @unittest.skipUnless(shutil.which('clang'), 'clang unavailable')
    def test_real_error_not_suppressed(self):
        self.put('src/b.c', 'number answer(void) { return missing_symbol; }\n')
        self.run_generator('src/main.c')
        e = self.database()[-1]
        p = subprocess.run(['clang'] + e['arguments'][1:] + ['-fsyntax-only'],
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('missing_symbol', p.stderr)


if __name__ == '__main__':
    unittest.main()
