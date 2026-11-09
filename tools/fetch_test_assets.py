"""下载/生成 NTFS 测试素材（用于 tests/test_ntfs.py 与手工验证）。

素材来源（pytsk 官方测试数据，公开）：
- image.raw               : 很小的 NTFS 卷镜像（含普通文件与已删除目录项）
- tsk_volume_system.raw   : FAT 卷（用于验证“非 NTFS 报错”分支）
- image_deleted.raw       : 由 image.raw 生成：把 inode 15 的 MFT 记录标记为“已删除”，
                            用于验证“未分配 MFT 记录 + data run 仍在”时的内容恢复路径。

用法：
    python tools/fetch_test_assets.py [--assets-dir <项目>/tests/assets]
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "tests", "assets")

URLS = {
    "image.raw": "https://raw.githubusercontent.com/py4n6/pytsk/main/test_data/image.raw",
    "tsk_volume_system.raw": "https://raw.githubusercontent.com/py4n6/pytsk/main/test_data/tsk_volume_system.raw",
}
GZ_URLS = {
    "ntfs3.raw.gz": "https://raw.githubusercontent.com/msuhanov/dfir_ntfs/master/test_data/ntfs3.raw.gz",
}


def _download(name: str, assets_dir: str) -> str:
    path = os.path.join(assets_dir, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"[assets] 已存在 {name} ({os.path.getsize(path)} B)")
        return path
    print(f"[assets] 下载 {name} ...")
    urllib.request.urlretrieve(URLS[name], path)
    print(f"[assets] 完成 {name} ({os.path.getsize(path)} B)")
    return path


def _download_gz(name: str, assets_dir: str) -> str:
    """下载 .gz 并解压成同名去掉 .gz 的镜像。"""
    import gzip
    import shutil

    gz_path = os.path.join(assets_dir, name)
    out_path = gz_path[:-3]
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        print(f"[assets] 已存在 {os.path.basename(out_path)} ({os.path.getsize(out_path)} B)")
        return out_path
    if not os.path.exists(gz_path):
        print(f"[assets] 下载 {name} ...")
        urllib.request.urlretrieve(GZ_URLS[name], gz_path)
        print(f"[assets] 下载完成 {name} ({os.path.getsize(gz_path)} B)")
    with gzip.open(gz_path, "rb") as fin, open(out_path, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    print(f"[assets] 解压 {name} -> {os.path.basename(out_path)} ({os.path.getsize(out_path)} B)")
    return out_path


def generate_deleted_ntfs(src: str, dst: str, inode: int = 28) -> str:
    """复制真实 NTFS 镜像，把指定 inode 的 MFT 记录“在用”位清零（模拟删除）。"""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    boot = bytes(data[:512])
    assert boot[3:11] == b"NTFS    ", "不是 NTFS 镜像"
    cluster_size = struct.unpack_from("<H", boot, 0x0B)[0] * boot[0x0D]
    mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
    rec_off = mft_lcn * cluster_size + inode * mft_record_size(boot)
    assert data[rec_off:rec_off + 4] == b"FILE", f"inode {inode} 位置不是 FILE 记录"
    flags_off = rec_off + 0x16
    flags = struct.unpack_from("<H", data, flags_off)[0]
    print(f"[assets] 生成 {os.path.basename(dst)}：inode={inode} flags 0x{flags:04x} -> 0x{flags & ~1:04x}")
    struct.pack_into("<H", data, flags_off, flags & ~0x0001)
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def mft_record_size(boot: bytes) -> int:
    """解析 NTFS 引导扇区得到 MFT 记录大小。"""
    cluster = struct.unpack_from("<H", boot, 0x0B)[0] * boot[0x0D]
    raw = struct.unpack_from("<b", boot, 0x40)[0]
    if raw > 0:
        return raw * cluster
    return 1 << (-raw)


def generate_deleted_image(src: str, dst: str) -> str:
    """复制 image.raw，并把 inode 15 的 MFT 记录 flags 的“在用”位清零（模拟删除）。"""
    with open(src, "rb") as f:
        data = bytearray(f.read())
    boot = bytes(data[:512])
    assert boot[3:11] == b"NTFS    ", "不是 NTFS 镜像"
    cluster_size = struct.unpack_from("<H", boot, 0x0B)[0] * boot[0x0D]
    mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
    record_size = mft_record_size(boot)
    inode = 15
    rec_off = mft_lcn * cluster_size + inode * record_size
    assert data[rec_off:rec_off + 4] == b"FILE", f"inode {inode} 位置不是 FILE 记录"
    flags_off = rec_off + 0x16
    flags = struct.unpack_from("<H", data, flags_off)[0]
    print(f"[assets] 生成 {os.path.basename(dst)}：inode={inode} record_off=0x{rec_off:x} "
          f"flags 0x{flags:04x} -> 0x{flags & ~0x0001:04x}")
    struct.pack_into("<H", data, flags_off, flags & ~0x0001)
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def ensure_assets(assets_dir: str = DEFAULT_DIR) -> dict:
    os.makedirs(assets_dir, exist_ok=True)
    paths = {}
    for name in URLS:
        paths[name] = _download(name, assets_dir)
    for name in GZ_URLS:
        paths[name[:-3]] = _download_gz(name, assets_dir)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下载/生成 NTFS 测试素材")
    ap.add_argument("--assets-dir", default=DEFAULT_DIR)
    args = ap.parse_args(argv)
    try:
        paths = ensure_assets(args.assets_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[assets] 失败：{exc}")
        return 1
    for k, v in paths.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
