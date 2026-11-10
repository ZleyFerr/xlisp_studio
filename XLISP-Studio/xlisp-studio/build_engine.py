"""Build the genuine XLISP interpreter. Python 3.10+, GCC or Zig required."""
from pathlib import Path
import argparse
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument('--cc', default='gcc', help='Compiler command, e.g. "zig cc -target x86-windows-gnu"')
p.add_argument('--windows', action='store_true')
a = p.parse_args()
sources = sorted((ROOT / 'source/src').glob('*.c'))
sources = [s for s in sources if s.name not in ('msstuff.c',)]
output = ROOT / 'engine' / ('xlisp.exe' if a.windows or sys.platform == 'win32' else 'xlisp')
subprocess.run(shlex.split(a.cc) + ['-O2', '-Wno-incompatible-pointer-types',
    '-Wno-int-conversion', '-I' + str(ROOT / 'source/include'),
    str(ROOT / 'source/engine_main.c')] + [str(s) for s in sources] + ['-lm', '-o', str(output)], check=True)
print(output)
