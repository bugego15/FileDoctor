"""运行 tests 目录下所有 test_*.py；任一失败则退出码非 0。"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"Tests: {len(TESTS)}\n")
    failed = []
    for path in TESTS:
        name = os.path.basename(path)
        print(f"=== {name} ===")
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=ROOT)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if line.strip():
                print("  " + line)
        if proc.returncode != 0:
            failed.append(name)
            print(f"  -> FAILED (exit {proc.returncode})\n")
        else:
            print("  -> PASS\n")
    print("=" * 60)
    if failed:
        print(f"RESULT: {len(failed)} FAILED -> {failed}")
        return 1
    print(f"RESULT: ALL {len(TESTS)} TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
