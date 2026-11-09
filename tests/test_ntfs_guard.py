"""ntfs 模块守卫测试：

- 未安装 pytsk3 时，open_volume 必须抛出带安装提示的 RuntimeError，CLI 必须给出明确 JSON 错误；
- 已安装 pytsk3 时只做 import/基本接口存在性检查（真正 NTFS 恢复需要测试盘，见 tools/make_ntfs_test_vhd.ps1）。
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fdlib import ntfs

FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("== test_ntfs_guard ==")
    print(f"  pytsk3 可用：{ntfs.HAS_PYTSK3}  ({ntfs.PYTSK3_IMPORT_ERROR})")
    if ntfs.HAS_PYTSK3:
        check("open_volume 可调用且是类/函数", callable(ntfs.open_volume))
        check("NtfsVolume 类存在", hasattr(ntfs, "NtfsVolume"))
        check("提供 recover/list_entries 方法",
              hasattr(ntfs.NtfsVolume, "recover") and hasattr(ntfs.NtfsVolume, "list_entries"))
        check("提供 list_partitions", callable(ntfs.list_partitions))
    else:
        try:
            ntfs.require_pytsk3()
            check("require_pytsk3 缺少依赖时应报错", False)
        except RuntimeError as exc:
            check("require_pytsk3 提示安装命令", "pip install pytsk3" in str(exc), str(exc))
        try:
            ntfs.open_volume(r"\\.\NoSuchDevice:")
            check("open_volume 缺少依赖时应报错", False)
        except RuntimeError as exc:
            check("open_volume 缺少依赖时提示 pytsk3", "pytsk3" in str(exc), str(exc))

        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "filetool.py"), "ntfs", r"\\.\NoSuchDevice:"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("CLI ntfs 缺少依赖时退出码 2", proc.returncode == 2, f"rc={proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except Exception:  # noqa: BLE001
            data = {}
        check("CLI 错误信息包含 pip install pytsk3",
              "pytsk3" in str(data.get("error", "")) + str(data.get("detail", "")), proc.stdout[:200])

    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
