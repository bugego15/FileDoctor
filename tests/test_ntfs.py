"""NTFS 模块功能测试（真实 NTFS 镜像 + 模拟删除记录）。

素材（公开测试数据，见 tools/fetch_test_assets.py）：
- ntfs3.raw：64MB 真实 NTFS 3.x 镜像（dfir_ntfs 项目 test_data），含 /test/1.txt 等用户文件；
- image.raw：pytsk 的 ext2 测试镜像，用于验证“非 NTFS 时报错”；
- 删除恢复：测试时把 ntfs3.raw 复制一份，并把 inode 28（/test/1.txt）的 MFT 记录“在用”位清零，
  模拟“已删除、但 data run 仍在”的场景，验证内容能完整恢复并 SHA-256 一致。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from fdlib import ntfs  # noqa: E402

ASSETS = os.path.join(ROOT, "tests", "assets")
FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _direct_read(image_path: str, inode: int) -> bytes:
    """用 pytsk3 直接读指定 inode 的内容（作为恢复结果的对照）。"""
    import pytsk3

    img = pytsk3.Img_Info(image_path)
    try:
        fs = pytsk3.FS_Info(img, offset=0)
        f = fs.open_meta(inode=inode)
        return f.read_random(0, f.info.meta.size)
    finally:
        img.close()


def main():
    print("== test_ntfs ==")
    if not ntfs.HAS_PYTSK3:
        print("  [SKIP] 未安装 pytsk3（python -m pip install pytsk3）")
        return 0
    try:
        from fetch_test_assets import ensure_assets, generate_deleted_ntfs  # noqa: PLC0415

        ensure_assets(ASSETS)
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 测试素材不可用：{exc}")
        return 0

    ntfs_image = os.path.join(ASSETS, "ntfs3.raw")
    ext_image = os.path.join(ASSETS, "image.raw")
    check("NTFS 测试镜像存在", os.path.exists(ntfs_image) and os.path.getsize(ntfs_image) > 1_000_000,
          f"{os.path.getsize(ntfs_image) if os.path.exists(ntfs_image) else 0} B")

    # ---------------- 基本识别 + 用户文件列表
    with ntfs.open_volume(ntfs_image) as vol:
        check("识别为 NTFS", vol.info["fs_type"] == "NTFS", str(vol.info))
        check("offset=0", vol.info["offset"] == 0)
        check("block size 4096", vol.info["block_size"] == 4096, str(vol.info["block_size"]))
        entries = vol.list_entries(include_deleted=True, limit=500)
        names = {e["path"] for e in entries}
        check("列出 /test/1.txt", "/test/1.txt" in names, str(sorted(names)[:20]))
        check("默认隐藏 NTFS 系统文件", not any(e["name"].startswith("$") for e in entries))
        user = [e for e in entries if e["path"] == "/test/1.txt"]
        check("inode 28 大小 286671", bool(user) and user[0]["inode"] == 28 and user[0]["size"] == 286671,
              str(user[:1]))

        # ---------------- 普通文件恢复
        outdir = tempfile.mkdtemp(prefix="fd_ntfs_out_")
        res = vol.recover(outdir, include_deleted=False, limit=20)
        check("恢复普通文件", res["recovered_count"] >= 1, str(res["errors"]))
        hit = [f for f in res["files"] if f["name"] == "1.txt" and f["recovered"]]
        check("恢复结果含 1.txt", bool(hit))
        if hit:
            with open(hit[0]["path"], "rb") as fh:
                got = fh.read()
            orig = _direct_read(ntfs_image, 28)
            check("普通文件内容一致", hashlib.sha256(got).hexdigest() == hashlib.sha256(orig).hexdigest(),
                  f"{len(got)}B")
        shutil.rmtree(outdir, ignore_errors=True)

    # ---------------- 已删除记录（模拟）+ MFT 未分配扫描恢复
    tmp = tempfile.mkdtemp(prefix="fd_ntfs_del_")
    deleted_image = os.path.join(tmp, "ntfs3_deleted.raw")
    try:
        generate_deleted_ntfs(ntfs_image, deleted_image, inode=28)
        check("删除测试镜像已生成", os.path.getsize(deleted_image) == os.path.getsize(ntfs_image))
        orig = _direct_read(ntfs_image, 28)
        outdir2 = tempfile.mkdtemp(prefix="fd_ntfs_del_out_")
        with ntfs.open_volume(deleted_image) as vol:
            entries = vol.list_entries(include_deleted=True, scan_unallocated=True, limit=2000)
            cand = [e for e in entries if e["inode"] == 28 and e["deleted"]]
            check("扫描到已删除 inode 28", bool(cand), str([e["inode"] for e in entries][:10]))
            if cand:
                check("删除记录大小 286671", cand[0]["size"] == 286671, str(cand[0]))
                check("来源是 mft_unallocated", cand[0]["source"] == "mft_unallocated",
                      str(cand[0]["source"]))
            res = vol.recover(outdir2, include_deleted=True, scan_unallocated=True, limit=50)
            hit = [f for f in res["files"] if f["inode"] == 28 and f["recovered"]]
            check("恢复已删除记录", bool(hit), str(res["errors"]))
            if hit:
                with open(hit[0]["path"], "rb") as fh:
                    got = fh.read()
                check("删除记录恢复内容一致",
                      hashlib.sha256(got).hexdigest() == hashlib.sha256(orig).hexdigest(),
                      f"{len(got)}B")
                check("标记 recovered_full", hit[0]["recovered_full"] is True)

        # ---------------- CLI 端到端
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "filetool.py"), "ntfs",
                               deleted_image, "--inode-scan", "--limit", "50"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("CLI ntfs --inode-scan 退出码 0", proc.returncode == 0, proc.stderr[-300:])
        check("CLI 输出包含 mft_unallocated", "mft_unallocated" in proc.stdout,
              proc.stdout[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(outdir2 if "outdir2" in locals() else "", ignore_errors=True)

    # ---------------- 非 NTFS 镜像的报错
    if os.path.exists(ext_image):
        try:
            ntfs.open_volume(ext_image)
            check("ext2 镜像应报错", False)
        except OSError as exc:
            check("ext2 镜像报错且提示 NTFS", "NTFS" in str(exc), str(exc))

    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
