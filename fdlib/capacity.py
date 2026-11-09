"""U 盘/磁盘容量与写入完整性检测。

原理：把 **按偏移可重复生成的伪随机数据** 连续写入目标目录下的测试文件，再整段读回逐块比对：
- 正常盘：全部一致；
- 扩容盘（真实容量小于标称）：写到真实容量后数据会回卷覆盖前面的内容，读回比对时
  在 ``first_bad_offset`` 附近开始不一致 → 可估算真实容量；
- 坏盘/掉速盘：写入或读取可能直接报错、或数据不一致。

只写普通目录（如 ``G:\\``），不直接写裸设备；测试结束默认删除测试文件。
"""

from __future__ import annotations

import os
import random
import shutil
import time

DEFAULT_BLOCK = 8 * 1024 * 1024      # 8 MiB
DEFAULT_WRITE = 256 * 1024 * 1024    # 默认只测 256 MiB（快速体检）
MAX_WRITE = 4096 * 1024 * 1024 * 1024


def _block_bytes(seed: int, index: int, size: int) -> bytes:
    """按 (seed, block_index) 生成确定性的伪随机块。"""
    rnd = random.Random(((seed & 0xFFFFFFFF) << 24) ^ (index & 0xFFFFFFFF))
    return rnd.randbytes(size)


def _first_diff(a: bytes, b: bytes) -> int:
    """返回两个等长 bytes 第一个不同字节的下标；相同返回 -1。"""
    step = 4096
    for k in range(0, min(len(a), len(b)), step):
        ca, cb = a[k:k + step], b[k:k + step]
        if ca != cb:
            for j, (x, y) in enumerate(zip(ca, cb)):
                if x != y:
                    return k + j
            return k
    return -1


def test_capacity(target: str, write_bytes: int | None = None,
                  block_size: int = DEFAULT_BLOCK, seed: int = 0x46444F43,
                  keep: bool = False, progress=None) -> dict:
    """对 *target* 目录所在磁盘做写入/读回校验。

    :param target: 目标目录，例如 ``G:\\`` 或 ``F:\\temp\\cptest``
    :param write_bytes: 要写多少字节；None = 256 MiB
    :param block_size: 测试块大小
    :param keep: True 时保留测试文件
    :param progress: 回调 ``progress(stage, done, total)``
    """
    report: dict = {
        "target": os.path.abspath(target),
        "block_size": block_size,
        "ok": False,
        "notes": [],
        "errors": [],
    }
    if not os.path.isdir(target):
        return {**report, "error": f"目标不是目录：{target}"}
    usage = shutil.disk_usage(target)
    report.update(claimed_bytes=usage.total, claimed_gb=round(usage.total / 1e9, 2),
                  free_bytes_at_start=usage.free, free_gb=round(usage.free / 1e9, 2))
    if write_bytes is None:
        write_bytes = DEFAULT_WRITE
    write_bytes = int(write_bytes)
    if write_bytes <= 0 or write_bytes > MAX_WRITE:
        return {**report, "error": f"write_bytes 不合法：{write_bytes}"}
    if write_bytes > usage.free:
        report["notes"].append(
            f"要写入 {write_bytes / 1e9:.2f} GB，大于剩余空间 {usage.free / 1e9:.2f} GB，已按剩余空间截断")
        write_bytes = max(block_size, (usage.free // block_size) * block_size)
    nblocks = write_bytes // block_size
    write_bytes = nblocks * block_size
    report["write_bytes"] = write_bytes
    if nblocks < 1:
        return {**report, "error": "可用空间不足一个测试块"}

    test_file = os.path.join(target, f"_fdtest_{os.getpid()}_{int(time.time())}.bin")
    report["test_file"] = test_file
    try:
        # ---------------- 写入
        t0 = time.time()
        with open(test_file, "wb") as f:
            for idx in range(nblocks):
                f.write(_block_bytes(seed, idx, block_size))
                if progress and idx % 16 == 0:
                    progress("write", idx * block_size, write_bytes)
            f.flush()
            os.fsync(f.fileno())
        dt_write = time.time() - t0
        report["write_speed_mbps"] = round(write_bytes / dt_write / 1e6, 1) if dt_write else None

        # ---------------- 读回校验
        t0 = time.time()
        first_bad = None
        verified = 0
        with open(test_file, "rb") as f:
            for idx in range(nblocks):
                data = f.read(block_size)
                expect = _block_bytes(seed, idx, block_size)
                if data != expect:
                    diff = _first_diff(data, expect)
                    first_bad = idx * block_size + (diff if diff >= 0 else 0)
                    break
                verified += len(data)
                if progress and idx % 16 == 0:
                    progress("verify", idx * block_size, write_bytes)
        dt_read = time.time() - t0
        report["verified_bytes"] = verified
        report["read_speed_mbps"] = round(verified / dt_read / 1e6, 1) if dt_read else None

        # ---------------- 回卷检测：重读开头，看是否被后面写的数据覆盖
        wrap = False
        if first_bad is None:
            with open(test_file, "rb") as f:
                head = f.read(min(block_size, write_bytes))
            if head != _block_bytes(seed, 0, len(head)):
                wrap = True
                report["notes"].append("重读开头数据与写入不一致：疑似写入过程中发生回卷（扩容盘典型特征）")

        ok = first_bad is None and not wrap and verified == write_bytes
        report["ok"] = ok
        report["first_bad_offset"] = first_bad
        if first_bad is not None:
            real = (first_bad // (1024 * 1024)) * 1024 * 1024
            report["real_capacity_estimate_bytes"] = real
            report["real_capacity_estimate_gb"] = round(real / 1e9, 2)
            report["notes"].append(
                f"从 {first_bad / 1e9:.3f} GB 附近开始校验不一致，疑似真实容量约 {real / 1e9:.2f} GB")
        report["elapsed_s"] = round(dt_write + dt_read, 2)
    except OSError as exc:
        report["errors"].append(f"I/O 错误：{exc}")
        report["notes"].append("写入/读取过程中出错，这块盘可能已损坏")
    finally:
        if not keep:
            try:
                if os.path.exists(test_file):
                    os.remove(test_file)
            except OSError as exc:
                report["notes"].append(f"清理测试文件失败：{exc}")
        report["cleaned_up"] = not keep
    # 清理后重算剩余空间
    try:
        report["free_bytes_after"] = shutil.disk_usage(target).free
    except OSError:
        pass
    return report


if __name__ == "__main__":
    import json
    import sys
    args = sys.argv[1:]
    target = args[0] if args else "."
    size = int(args[1]) if len(args) > 1 else DEFAULT_WRITE
    print(json.dumps(test_capacity(target, size), ensure_ascii=False, indent=1))
