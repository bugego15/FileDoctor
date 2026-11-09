#!/usr/bin/env python3
r"""FileDoctor —— 文件恢复/取证命令行工具（纯 Python 标准库）。

子命令：
  probe     识别镜像/设备的分区与文件系统信息（可对比 Windows 挂载结果）
  repair    修复损坏的 ZIP / DOCX
  carve     从镜像/原始设备按特征码雕刻恢复文件
  exfat     读取 exFAT 文件系统、列出/恢复（含已删除）文件
  ntfs      读取 NTFS 文件系统、列出/恢复（含已删除）文件（需要 pytsk3）
  capacity  U 盘容量与写入完整性检测（扩容盘/坏盘）

示例：
  python filetool.py probe \\.\G:
  python filetool.py repair broken.docx -o fixed.docx --template good.docx
  python filetool.py carve \\.\G: -o recovered --types docx,zip,png,jpg,pdf --text
  python filetool.py exfat image.img -o out                 # 恢复 exFAT 文件
  python filetool.py ntfs \\.\G:                              # 列出 NTFS 删除文件（需 pytsk3）
  python filetool.py ntfs image.img -o out --filter docx,docx  # 恢复 NTFS 删除/丢失文件（需 pytsk3）
  python filetool.py capacity G:\ --write-gb 58 --yes
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fdlib import __version__  # noqa: E402
from fdlib.device import DeviceReader, is_raw_device  # noqa: E402

SECTOR = 512


# --------------------------------------------------------------------------- probe 工具
def _parse_ntfs(sector: bytes) -> dict:
    bps = struct.unpack_from("<H", sector, 0x0B)[0]
    spc = sector[0x0D]
    total = struct.unpack_from("<Q", sector, 0x28)[0]
    serial = struct.unpack_from("<I", sector, 0x48)[0]
    return {"fs": "NTFS", "bytes_per_sector": bps,
            "sectors_per_cluster": spc,
            "cluster_size": bps * spc if bps and spc else None,
            "total_sectors": total,
            "total_bytes": total * bps if total else None,
            "volume_serial": f"{serial:08X}"}


def _parse_exfat(sector: bytes) -> dict:
    bps_shift = sector[0x6C]
    spc_shift = sector[0x6D]
    total = struct.unpack_from("<Q", sector, 0x48)[0]
    serial = struct.unpack_from("<I", sector, 0x64)[0]
    return {"fs": "exFAT",
            "bytes_per_sector": 1 << bps_shift if bps_shift < 32 else None,
            "sectors_per_cluster": 1 << spc_shift if spc_shift < 32 else None,
            "cluster_size": (1 << bps_shift) * (1 << spc_shift) if bps_shift < 32 and spc_shift < 32 else None,
            "total_sectors": total,
            "volume_serial": f"{serial:08X}",
            "fat_offset_sectors": struct.unpack_from("<I", sector, 0x50)[0],
            "fat_length_sectors": struct.unpack_from("<I", sector, 0x54)[0],
            "cluster_heap_offset_sectors": struct.unpack_from("<I", sector, 0x58)[0],
            "cluster_count": struct.unpack_from("<I", sector, 0x5C)[0],
            "root_cluster": struct.unpack_from("<I", sector, 0x60)[0]}


def _parse_fat32(sector: bytes) -> dict:
    bps = struct.unpack_from("<H", sector, 0x0B)[0]
    spc = sector[0x0D]
    total = struct.unpack_from("<I", sector, 0x20)[0]
    serial = struct.unpack_from("<I", sector, 0x43)[0]
    return {"fs": "FAT32", "bytes_per_sector": bps, "sectors_per_cluster": spc,
            "total_sectors": total, "volume_serial": f"{serial:08X}"}


def _parse_boot(sector: bytes) -> dict:
    if len(sector) < 512:
        return {"fs": "unknown", "note": "扇区数据不足 512 字节"}
    if sector[3:11] == b"NTFS    ":
        return _parse_ntfs(sector)
    if sector[3:11] == b"EXFAT   ":
        return _parse_exfat(sector)
    if sector[82:90] == b"FAT32   ":
        return _parse_fat32(sector)
    if sector[54:62] == b"FAT16   ":
        return {"fs": "FAT16"}
    # MBR？
    if sector[510:512] == b"\x55\xaa":
        parts = []
        for i in range(4):
            off = 0x1BE + i * 16
            ptype = sector[off + 4]
            lba = struct.unpack_from("<I", sector, off + 8)[0]
            cnt = struct.unpack_from("<I", sector, off + 12)[0]
            if ptype and cnt:
                parts.append({"index": i + 1, "type": f"0x{ptype:02X}",
                              "start_lba": lba, "sectors": cnt,
                              "start_bytes": lba * 512})
        if parts:
            return {"fs": "MBR(整盘分区表)", "partitions": parts}
    if sector == b"\x00" * 512:
        return {"fs": "empty", "note": "扇区全 0（未格式化/未写入）"}
    return {"fs": "unknown", "head_hex": sector[:32].hex(" ")}


def _windows_mount_info(raw_path: str) -> dict | None:
    import re
    m = re.match(r"^\\\\\.\\([A-Za-z]):$", raw_path)
    if not m:
        return None
    letter = m.group(1).upper()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint(0)
    maxlen = ctypes.c_uint(0)
    flags = ctypes.c_uint(0)
    ok = kernel32.GetVolumeInformationW(
        f"{letter}:\\", name, 261, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fs, 261)
    drive_type = kernel32.GetDriveTypeW(f"{letter}:\\")
    if not ok:
        return {"drive_letter": letter, "mounted": False}
    types = {0: "unknown", 1: "no_root", 2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ramdisk"}
    return {"drive_letter": letter, "mounted": True, "filesystem": fs.value,
            "label": name.value, "serial": f"{serial.value:08X}",
            "drive_type": types.get(drive_type, str(drive_type))}


def cmd_probe(args) -> int:
    report = {"path": args.path, "is_raw_device": is_raw_device(args.path), "offset": args.offset}
    with DeviceReader(args.path) as dev:
        report["device_size"] = dev.size
        report["device_size_gb"] = round(dev.size / 1e9, 3) if dev.size else None
        sector = dev.read_at(args.offset, SECTOR)
        report["boot"] = _parse_boot(sector)
        win = _windows_mount_info(args.path)
        if win:
            report["windows_mount"] = win
            boot_fs = str(report["boot"].get("fs", "")).upper()
            if win.get("mounted") and boot_fs and boot_fs not in win["filesystem"].upper() \
                    and boot_fs not in ("UNKNOWN", "EMPTY", "MBR(整盘分区表)"):
                report["warning"] = (
                    f"原始扇区显示 {report['boot']['fs']}，但 Windows 挂载为 {win['filesystem']}；"
                    "存在文件系统视图不一致（扩容盘/固件异常的典型线索）")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- repair
def cmd_repair(args) -> int:
    from fdlib import ziprepair
    out = args.output
    if args.zip:
        result = ziprepair.repair_zip(args.source, out)
    else:
        with open(args.source, "rb") as f:
            head = f.read(64)
        is_docx = (args.source.lower().endswith(".docx") or out.lower().endswith(".docx")
                   or head.startswith(b"PK") and b"word/" in open(args.source, "rb").read(4096))
        if is_docx:
            result = ziprepair.repair_docx(args.source, out, args.template)
        else:
            result = ziprepair.repair_zip(args.source, out)
    result["output"] = os.path.abspath(out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


# --------------------------------------------------------------------------- carve
def cmd_carve(args) -> int:
    try:
        from fdlib import carve
    except ImportError as exc:
        print(json.dumps({"error": f"carve 模块不可用：{exc}"}, ensure_ascii=False))
        return 2
    os.makedirs(args.output, exist_ok=True)
    types = [t.strip() for t in args.types.split(",") if t.strip()] if args.types else None
    state = {"last": -1}

    def progress(scanned, total):
        pct = int(scanned * 100 / total) if total else 0
        if total and pct // 5 != state["last"]:
            state["last"] = pct // 5
            print(f"[carve] {pct}% ({scanned / 1e9:.2f} / {total / 1e9:.2f} GB)", file=sys.stderr)

    report = carve.carve_image(
        args.path, args.output, types=types, text=args.text,
        start=args.start, length=args.length, min_size=args.min_size,
        max_files=args.max_files, progress=progress)
    report["input"] = args.path
    report_path = os.path.join(args.output, "carve_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    summary = {"scanned_bytes": report.get("scanned_bytes"),
               "by_type": report.get("by_type"),
               "files": len(report.get("files", [])),
               "texts": len(report.get("texts", [])),
               "report": report_path}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- exfat
def cmd_exfat(args) -> int:
    try:
        from fdlib import exfat
    except ImportError as exc:
        print(json.dumps({"error": f"exfat 模块不可用：{exc}"}, ensure_ascii=False))
        return 2
    vol = exfat.open_volume(args.path, offset=args.offset)
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        result = vol.recover(args.output, include_deleted=not args.no_deleted)
        result["info"] = vol.info
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    entries = vol.list_entries(include_deleted=not args.no_deleted)
    out = {"info": vol.info, "count": len(entries), "entries": []}
    for e in entries[: args.limit]:
        out["entries"].append({
            "name": e.name, "size": e.size, "is_dir": e.is_dir,
            "deleted": e.deleted, "first_cluster": e.first_cluster,
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- ntfs
def cmd_ntfs(args) -> int:
    try:
        from fdlib import ntfs
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"ntfs 模块不可用：{exc}"}, ensure_ascii=False))
        return 2
    if not ntfs.HAS_PYTSK3:
        print(json.dumps({
            "error": "需要 pytsk3 才能使用 NTFS 恢复：python -m pip install pytsk3",
            "detail": ntfs.PYTSK3_IMPORT_ERROR,
        }, ensure_ascii=False, indent=2))
        return 2
    extensions = None
    if args.filter:
        extensions = [e.strip().lower().lstrip(".") for e in args.filter.split(",") if e.strip()]
    if args.partitions:
        try:
            parts = ntfs.list_partitions(args.path)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"枚举分区失败：{exc}"}, ensure_ascii=False))
            return 1
        print(json.dumps({"path": args.path, "partitions": parts}, ensure_ascii=False, indent=2))
        return 0
    try:
        with ntfs.open_volume(args.path, offset=args.offset) as vol:
            if not args.output:
                entries = vol.list_entries(include_deleted=True, extensions=extensions,
                                           limit=None, scan_unallocated=args.inode_scan,
                                           include_system=args.include_system)
                if not args.all:
                    entries = [e for e in entries if e["deleted"]]
                entries = entries[: args.limit]
                print(json.dumps({
                    "info": vol.info,
                    "count": len(entries),
                    "entries": entries,
                }, ensure_ascii=False, indent=2))
                return 0
            os.makedirs(args.output, exist_ok=True)
            result = vol.recover(args.output, include_deleted=not args.all,
                                 extensions=extensions, limit=args.limit,
                                 scan_unallocated=args.inode_scan,
                                 include_system=args.include_system)
            result["info"] = vol.info
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("count") else 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


# --------------------------------------------------------------------------- capacity
def cmd_capacity(args) -> int:
    from fdlib import capacity
    write_bytes = None
    if args.write_gb:
        if not args.yes:
            print(json.dumps({"error": "写入超过 1GB 的容量测试需要加 --yes 确认（会占满磁盘空间）"},
                             ensure_ascii=False))
            return 2
        write_bytes = int(args.write_gb * 1e9)
    state = {"last": -1}

    def progress(stage, done, total):
        pct = int(done * 100 / total) if total else 0
        if pct // 10 != state["last"]:
            state["last"] = pct // 10
            print(f"[capacity] {stage} {pct}%", file=sys.stderr)

    result = capacity.test_capacity(args.target, write_bytes=write_bytes,
                                    block_size=args.block_mb * 1024 * 1024,
                                    keep=args.keep, progress=progress)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="filetool.py",
        description="FileDoctor —— ZIP/DOCX 修复、特征码雕刻、exFAT 恢复、U 盘容量检测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python filetool.py probe \\\\.\\G:\n"
               "  python filetool.py repair broken.docx -o fixed.docx --template good.docx\n"
               "  python filetool.py carve \\\\.\\G: -o recovered --types docx,zip,png,jpg,pdf --text\n"
               "  python filetool.py exfat image.img -o out\n"
               "  python filetool.py ntfs \\\\.\\G:  -o out   # 需要 pytsk3\n"
               "  python filetool.py capacity G:\\ --write-gb 58 --yes\n")
    p.add_argument("--version", action="version", version=f"FileDoctor {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("probe", help="识别镜像/设备的分区与文件系统")
    sp.add_argument("path")
    sp.add_argument("--offset", type=int, default=0, help="从该字节偏移读取引导扇区（默认 0）")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("repair", help="修复损坏的 ZIP/DOCX")
    sp.add_argument("source")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--template", help="同源健康文件（docx 可借用结构件与图片关系）")
    sp.add_argument("--zip", action="store_true", help="按普通 ZIP 处理")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("carve", help="从镜像/设备雕刻恢复文件")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--types", default="docx,zip,png,jpg,pdf,exe")
    sp.add_argument("--text", action="store_true", help="同时雕刻文本碎片")
    sp.add_argument("--start", type=int, default=0)
    sp.add_argument("--length", type=int, default=None)
    sp.add_argument("--min-size", type=int, default=1024)
    sp.add_argument("--max-files", type=int, default=500)
    sp.set_defaults(func=cmd_carve)

    sp = sub.add_parser("exfat", help="读取 exFAT 卷（列出/恢复文件，含删除）")
    sp.add_argument("path")
    sp.add_argument("-o", "--output", help="恢复到此目录（不给则只列出）")
    sp.add_argument("--offset", type=int, default=0, help="分区起始字节偏移")
    sp.add_argument("--no-deleted", action="store_true", help="不处理已删除文件")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(func=cmd_exfat)

    sp = sub.add_parser("ntfs", help="NTFS 删除文件扫描/恢复（需要 pytsk3）")
    sp.add_argument("path", help="NTFS 镜像文件或原始设备路径（如 \\\\.\\G:、\\\\.\\PhysicalDrive2）")
    sp.add_argument("-o", "--output", help="恢复到此目录（不给则只列出，默认只列删除文件）")
    sp.add_argument("--offset", type=int, default=None, help="NTFS 分区起始字节偏移（默认自动探测）")
    sp.add_argument("--all", action="store_true", help="包含未删除文件（默认只看删除文件）")
    sp.add_argument("--filter", default=None, help="扩展名过滤，如 docx,pdf,txt")
    sp.add_argument("--limit", type=int, default=200, help="最多处理条目数（默认 200）")
    sp.add_argument("--partitions", action="store_true", help="只枚举分区表")
    sp.add_argument("--inode-scan", action="store_true",
                    help="额外扫描 MFT 未分配记录（孤儿删除文件，名字可能丢失但内容有时可恢复）")
    sp.add_argument("--include-system", action="store_true",
                    help="包含 NTFS 系统元数据文件（$MFT、$Bitmap 等，默认隐藏）")
    sp.set_defaults(func=cmd_ntfs)

    sp = sub.add_parser("capacity", help="U 盘容量/写入完整性检测")
    sp.add_argument("target", help="目标目录，例如 G:\\ 或 D:\\temp\\cptest")
    sp.add_argument("--write-gb", type=float, default=None, help="写入多少 GB（默认只写 256MB 做体检）")
    sp.add_argument("--block-mb", type=int, default=8)
    sp.add_argument("--keep", action="store_true", help="保留测试文件")
    sp.add_argument("--yes", action="store_true", help="确认大容量写入测试")
    sp.set_defaults(func=cmd_capacity)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(json.dumps({"error": "已中断"}, ensure_ascii=False))
        return 130
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
