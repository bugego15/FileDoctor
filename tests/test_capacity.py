"""capacity 模块测试：小容量写入/读回校验（16MB）。"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fdlib import capacity

FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("== test_capacity ==")
    tmp = tempfile.mkdtemp(prefix="fdtest_cap_")
    report = capacity.test_capacity(tmp, write_bytes=16 * 1024 * 1024, block_size=4 * 1024 * 1024)
    print("  report:", json.dumps({k: report.get(k) for k in
                                   ("ok", "write_bytes", "verified_bytes", "first_bad_offset",
                                    "write_speed_mbps", "read_speed_mbps", "cleaned_up")},
                                  ensure_ascii=False))
    check("返回 ok", report.get("ok") is True, str(report.get("notes")))
    check("写入量正确", report.get("write_bytes") == 16 * 1024 * 1024)
    check("校验量正确", report.get("verified_bytes") == 16 * 1024 * 1024)
    check("没有校验失败偏移", report.get("first_bad_offset") is None)
    check("测试文件已清理", report.get("cleaned_up") is True and not
          [n for n in os.listdir(tmp) if n.startswith("_fdtest_")])

    # 非法目标
    bad = capacity.test_capacity(os.path.join(tmp, "not-exists"), write_bytes=1024 * 1024)
    check("非法目标返回 error", "error" in bad)

    shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
