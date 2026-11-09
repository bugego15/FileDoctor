"""GUI 冒烟测试：PySide6 可用时离屏创建主窗口；不可用时跳过。"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("== test_gui ==")
    try:
        import PySide6  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 未安装 PySide6（{exc}）")
        return 0
    out_path = os.path.join(tempfile.gettempdir(), f"filedoctor_gui_selftest_{os.getpid()}.json")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["FILEDOCTOR_SELFTEST_OUT"] = out_path
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "gui.py"), "--self-test"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=120, cwd=ROOT)
    check("gui --self-test 退出码 0", proc.returncode == 0, (proc.stderr or "")[-500:])
    data = {}
    if os.path.exists(out_path):
        try:
            data = json.loads(open(out_path, encoding="utf-8").read())
        except Exception as exc:  # noqa: BLE001
            print("  selftest json 解析失败:", exc)
        os.remove(out_path)
    check("写入自检 JSON", bool(data), out_path)
    check("自检 ok", data.get("ok") is True, str(data)[:400])
    check("主窗口 5 个标签页", data.get("tabs") == 5, str(data.get("tabs")))
    check("PySide6 版本已记录", bool(data.get("pyside6")))

    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
