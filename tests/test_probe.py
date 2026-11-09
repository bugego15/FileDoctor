"""probe 命令测试：合成 NTFS/exFAT/FAT32/MBR 引导扇区，验证识别逻辑与 CLI 输出。"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import filetool  # noqa: E402

TMP = tempfile.mkdtemp(prefix="fdtest_probe_")
FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def ntfs_sector() -> bytes:
    s = bytearray(512)
    s[0:3] = b"\xEB\x52\x90"
    s[3:11] = b"NTFS    "
    struct.pack_into("<H", s, 0x0B, 512)
    s[0x0D] = 8
    struct.pack_into("<Q", s, 0x28, 122877951)
    struct.pack_into("<I", s, 0x48, 0x42E03E8F)
    s[510:512] = b"\x55\xaa"
    return bytes(s)


def exfat_sector() -> bytes:
    s = bytearray(512)
    s[0:3] = b"\xEB\x76\x90"
    s[3:11] = b"EXFAT   "
    struct.pack_into("<Q", s, 0x48, 122871808)   # VolumeLength
    struct.pack_into("<I", s, 0x50, 2048)        # FatOffset
    struct.pack_into("<I", s, 0x54, 4096)        # FatLength
    struct.pack_into("<I", s, 0x58, 6144)        # ClusterHeapOffset
    struct.pack_into("<I", s, 0x5C, 30000)       # ClusterCount
    struct.pack_into("<I", s, 0x60, 4)           # RootDirCluster
    struct.pack_into("<I", s, 0x64, 0x1234ABCD)  # serial
    struct.pack_into("<H", s, 0x68, 0x0100)      # revision
    s[0x6C] = 9                                  # 512B sectors
    s[0x6D] = 3                                  # 8 sectors/cluster = 4KB
    s[0x6E] = 1                                  # one FAT
    s[510:512] = b"\x55\xaa"
    return bytes(s)


def fat32_sector() -> bytes:
    s = bytearray(512)
    s[0:3] = b"\xEB\x58\x90"
    s[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", s, 0x0B, 512)
    s[0x0D] = 8
    struct.pack_into("<I", s, 0x20, 1000000)
    struct.pack_into("<I", s, 0x43, 0xDEADBEEF)
    s[82:90] = b"FAT32   "
    s[510:512] = b"\x55\xaa"
    return bytes(s)


def mbr_sector() -> bytes:
    s = bytearray(512)
    s[0x1BE + 4] = 0x07  # partition 1 type NTFS
    struct.pack_into("<I", s, 0x1BE + 8, 2048)
    struct.pack_into("<I", s, 0x1BE + 12, 122871808 * 2)
    s[510:512] = b"\x55\xaa"
    return bytes(s)


def main():
    print("== test_probe ==")
    b = filetool._parse_boot(ntfs_sector())
    check("NTFS 识别", b["fs"] == "NTFS", str(b))
    check("NTFS 参数", b["bytes_per_sector"] == 512 and b["cluster_size"] == 4096
          and b["volume_serial"] == "42E03E8F", str(b))

    b = filetool._parse_boot(exfat_sector())
    check("exFAT 识别", b["fs"] == "exFAT", str(b))
    check("exFAT 参数", b["bytes_per_sector"] == 512 and b["cluster_size"] == 4096
          and b["root_cluster"] == 4 and b["volume_serial"] == "1234ABCD", str(b))

    b = filetool._parse_boot(fat32_sector())
    check("FAT32 识别", b["fs"] == "FAT32", str(b))

    b = filetool._parse_boot(mbr_sector())
    check("MBR 识别出分区", b["fs"].startswith("MBR") and len(b["partitions"]) == 1
          and b["partitions"][0]["start_bytes"] == 2048 * 512, str(b))

    b = filetool._parse_boot(bytes(512))
    check("全零扇区识别", b["fs"] == "empty", str(b))

    # CLI：在镜像文件上跑 probe
    img = os.path.join(TMP, "fake_exfat.img")
    data = bytearray(exfat_sector()) + bytearray(1024 * 1024)
    with open(img, "wb") as f:
        f.write(data)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "filetool.py"), "probe", img],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("probe CLI 退出码 0", proc.returncode == 0, proc.stderr)
    try:
        out = json.loads(proc.stdout)
        ok = out["boot"]["fs"] == "exFAT" and out["device_size"] == len(data)
    except Exception as exc:  # noqa: BLE001
        out, ok = {}, False
        print("  stdout:", proc.stdout[:300], "err:", exc)
    check("probe CLI JSON 正确", ok, str(out)[:300])

    shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
