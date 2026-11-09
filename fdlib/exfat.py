# -*- coding: utf-8 -*-
"""exFAT 文件系统解析与删除文件恢复（只读实现）。

本模块提供 FileDoctor 的 exFAT 相关能力：

* :func:`open_volume` —— 解析 exFAT VBR（含分区偏移 offset），返回 :class:`ExfatVolume`；
* :meth:`ExfatVolume.list_entries` —— 遍历目录树，解析
  File Directory Entry(0x85/0x05) + Stream Extension(0xC0/0x40) + File Name(0xC1/0x41)，
  UTF-16LE 文件名，并识别删除标记（EntryType 的 0x80 位被清零）；
* :meth:`ExfatVolume.read_entry_data` —— 读取文件内容：正常文件沿 FAT 链读；
  删除文件的 FAT 链通常已被清空，此时按“文件原本连续存放”的假设回退读取，
  并返回 ``recovered_full=False`` 提示结果不可完全信任；
* :meth:`ExfatVolume.recover` —— 按原目录结构把文件恢复到输出目录
  （自动创建子目录、重名自动加 ``-1``/``-2`` 后缀）。

设计约束
--------
* **只读**：不写设备/镜像的任何扇区，所有读取都通过 :class:`fdlib.device.DeviceReader`；
* **流式**：:meth:`ExfatVolume.iter_entry_chunks` 逐簇产出数据、``recover()`` 用它边读边写，
  大文件不会整体占用内存；只有 ``read_entry_data()`` 按接口约定返回完整 bytes；
* 目录内容整体读入内存（exFAT 目录通常很小），超过 ``MAX_DIR_BYTES`` 时抛出异常；
* 单个 FAT 卷完整支持；NumberOfFats==2 时按 VolumeFlags 选择活动 FAT 读取（只读）。

exFAT 关键格式备忘（Microsoft exFAT 规范 1.00）
------------------------------------------------
* VBR：0x03 "EXFAT   "；0x40 PartitionOffset(u64)；0x48 VolumeLength(u64, 扇区)；
  0x50 FatOffset(u32, 扇区)；0x54 FatLength；0x58 ClusterHeapOffset；0x5C ClusterCount；
  0x60 RootDirCluster；0x64 VolumeSerial；0x68 FsRevision(=0x0100)；
  0x6C BytesPerSectorShift；0x6D SectorsPerClusterShift；0x6E NumberOfFats；
  0x1FE 55 AA；
* FAT 每项 u32；cluster 0/1 保留；数据簇从 cluster 2 开始，合法簇号 2..ClusterCount+1；
  链尾为 0xFFFFFFF8..0xFFFFFFFF；簇 N 偏移 = ClusterHeapOffset + (N-2)*簇大小；
* 目录项：0x85 在用 / 0x05 删除的 File Entry（SecondaryCount、SetChecksum、属性、时间戳），
  其后是 0xC0/0x40 Stream Extension（Flags、NameLength、NameHash、ValidDataLength、
  FirstCluster、DataLength）与若干 0xC1/0x41 File Name 项（每项 15 个 UTF-16LE 字符）。
"""

from __future__ import annotations

import datetime
import os
import struct
import sys
from typing import Iterator, List, Optional, Tuple

try:  # 作为包导入（正常用法）
    from .device import DeviceReader
except ImportError:  # 直接运行本文件时兜底
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from fdlib.device import DeviceReader

__all__ = ["open_volume", "ExfatVolume", "Entry", "decode_timestamp"]

# --------------------------------------------------------------------------- 常量
EXFAT_SIGNATURE = b"EXFAT   "
SECTOR_SIZE = 512                       # VBR 前 512 字节足以解析全部引导字段
_DIR_ENTRY_SIZE = 32                    # 目录项大小
_FAT_EOC_MIN = 0xFFFFFFF8               # 0xFFFFFFF8..0xFFFFFFFF = 链尾
_FAT_BAD_CLUSTER = 0xFFFFFFF7           # 坏簇标记
_FAT_CACHE_BLOCK = 64 * 1024            # FAT 表读取缓存块（减少小读次数）
_MAX_DIR_BYTES = 64 * 1024 * 1024       # 单个目录最多整体读入 64MB
_MAX_DIR_DEPTH = 64                     # 目录递归深度上限（防御环）

_RESERVED_WINDOWS_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % index for index in range(1, 10)]
    + ["LPT%d" % index for index in range(1, 10)]
)
_ILLEGAL_FILENAME_CHARS = set('<>:"/\\|?*')


# --------------------------------------------------------------------------- 工具函数
def decode_timestamp(raw: int) -> Optional[int]:
    """把 exFAT 32 位时间戳解码成 epoch 秒；值为 0 或字段非法时返回 None。

    位域：bits0-4 秒/2、5-10 分、11-15 时、16-20 日、21-24 月、25-31 年（-1980）。
    exFAT 时间戳按“本地时间 + 时区偏移字段”存储，本函数忽略时区偏移、统一按 UTC 解释，
    以便在不同机器上得到确定值；需要原始字段时可用 ``Entry.mtime_raw``。
    """
    if not raw:
        return None
    second = (raw & 0x1F) * 2
    minute = (raw >> 5) & 0x3F
    hour = (raw >> 11) & 0x1F
    day = (raw >> 16) & 0x1F
    month = (raw >> 21) & 0x0F
    year = ((raw >> 25) & 0x7F) + 1980
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24
            and minute < 60 and second < 60):
        return None
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second,
                                   tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(moment.timestamp())


def _sanitize_name(name: str) -> str:
    """把 exFAT 文件名转换成安全的 Windows 文件名（防止非法字符/路径穿越/设备名）。"""
    cleaned = []
    for char in name:
        if char in _ILLEGAL_FILENAME_CHARS or ord(char) < 32:
            cleaned.append("_")
        else:
            cleaned.append(char)
    result = "".join(cleaned).strip().rstrip(".")      # Windows 不允许结尾空格/点
    if result in ("", ".", ".."):
        result = "_unnamed"
    stem = result.split(".")[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        result = "_" + result
    return result


# --------------------------------------------------------------------------- 目录项
class Entry:
    """一个 exFAT 目录项（File Entry 集合的解析结果），表示文件或目录。"""

    __slots__ = (
        "name", "size", "is_dir", "deleted", "first_cluster", "attributes",
        "mtime", "ctime", "atime", "mtime_raw", "ctime_raw", "atime_raw",
        "valid_data_length", "no_fat_chain", "name_hash", "name_length",
        "secondary_count", "parent_path", "path", "entry_offset",
    )

    def __init__(self, name: str = "", size: int = 0, is_dir: bool = False,
                 deleted: bool = False, first_cluster: int = 0, attributes: int = 0,
                 mtime: Optional[int] = None, ctime: Optional[int] = None,
                 atime: Optional[int] = None, mtime_raw: int = 0, ctime_raw: int = 0,
                 atime_raw: int = 0, valid_data_length: int = 0,
                 no_fat_chain: bool = False, name_hash: int = 0,
                 name_length: int = 0, secondary_count: int = 0,
                 parent_path: str = "", entry_offset: int = 0) -> None:
        self.name = name
        self.size = int(size)                       # DataLength（声明大小）
        self.is_dir = bool(is_dir)
        self.deleted = bool(deleted)
        self.first_cluster = int(first_cluster)
        self.attributes = int(attributes)
        self.mtime = mtime
        self.ctime = ctime
        self.atime = atime
        self.mtime_raw = int(mtime_raw)
        self.ctime_raw = int(ctime_raw)
        self.atime_raw = int(atime_raw)
        self.valid_data_length = int(valid_data_length)
        self.no_fat_chain = bool(no_fat_chain)
        self.name_hash = int(name_hash)
        self.name_length = int(name_length)
        self.secondary_count = int(secondary_count)
        self.parent_path = parent_path
        self.entry_offset = int(entry_offset)       # 目录项集合在本目录内的字节偏移
        self.path = f"{parent_path}/{name}" if parent_path else name

    def __repr__(self) -> str:
        kind = "dir" if self.is_dir else "file"
        flag = " deleted" if self.deleted else ""
        return f"<Entry {kind}{flag} {self.path!r} size={self.size} cluster={self.first_cluster}>"

    def to_dict(self) -> dict:
        """返回可 JSON 序列化的 dict（不含路径信息，路径见 ``path``）。"""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "is_dir": self.is_dir,
            "deleted": self.deleted,
            "first_cluster": self.first_cluster,
            "attributes": self.attributes,
            "mtime": self.mtime,
            "ctime": self.ctime,
            "valid_data_length": self.valid_data_length,
            "no_fat_chain": self.no_fat_chain,
            "name_hash": self.name_hash,
        }


# --------------------------------------------------------------------------- 卷
class ExfatVolume:
    """一个已打开的 exFAT 卷（只读）。

    用法::

        with open_volume(r"F:\\img\\usb.img") as vol:
            print(vol.info)
            entries = vol.list_entries(include_deleted=True)
            result = vol.recover(r"F:\\out", include_deleted=True)
    """

    def __init__(self, path: str, offset: int = 0) -> None:
        self.path = str(path)
        self.offset = int(offset or 0)
        if self.offset < 0:
            raise ValueError("offset 不能为负数")
        self._dev = DeviceReader(self.path)
        self._fat_cache_start = 0
        self._fat_cache = b""
        self._boot_notes: List[str] = []
        self._label = ""
        self._label_error: Optional[str] = None
        try:
            self._parse_boot_sector()
            try:
                self._label = self._read_volume_label()
            except Exception as exc:      # 卷标损坏不影响其他解析，但把原因记录进 info
                self._label_error = f"{type(exc).__name__}: {exc}"
            self.info = self._build_info()
        except Exception:
            self._dev.close()
            raise

    # ------------------------------------------------------------- 初始化/关闭
    def _parse_boot_sector(self) -> None:
        """解析 VBR（Main Boot Sector）并校验关键字段。"""
        boot = self._dev.read_at(self.offset, SECTOR_SIZE)
        if len(boot) < 0x78:
            raise ValueError(
                f"读取引导扇区失败：偏移 {self.offset} 处数据不足"
                f"（path={self.path!r}，可能文件太小或 offset 不正确）"
            )
        if boot[3:11] != EXFAT_SIGNATURE:
            raise ValueError(
                "指定位置不是 exFAT 卷：未找到 'EXFAT   ' 签名 "
                f"(path={self.path!r}, offset={self.offset})"
            )

        bytes_per_sector_shift = boot[0x6C]
        sectors_per_cluster_shift = boot[0x6D]
        if not (9 <= bytes_per_sector_shift <= 12):
            raise ValueError(
                f"exFAT 引导扇区 BytesPerSectorShift 非法：{bytes_per_sector_shift}")
        if not (0 <= sectors_per_cluster_shift <= 25):
            raise ValueError(
                f"exFAT 引导扇区 SectorsPerClusterShift 非法：{sectors_per_cluster_shift}")
        if bytes_per_sector_shift + sectors_per_cluster_shift > 25:
            raise ValueError("exFAT 簇大小超过规范上限（32MB）")

        self.bytes_per_sector = 1 << bytes_per_sector_shift
        self.sectors_per_cluster = 1 << sectors_per_cluster_shift
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.boot_signature_ok = boot[0x1FE:0x200] == b"\x55\xAA"
        if not self.boot_signature_ok:
            self._boot_notes.append("引导扇区结尾缺少 0x55AA 签名（仍尝试解析）")

        self.partition_offset = struct.unpack_from("<Q", boot, 0x40)[0]
        self.volume_length_sectors = struct.unpack_from("<Q", boot, 0x48)[0]
        self.fat_offset_sectors = struct.unpack_from("<I", boot, 0x50)[0]
        self.fat_length_sectors = struct.unpack_from("<I", boot, 0x54)[0]
        self.heap_offset_sectors = struct.unpack_from("<I", boot, 0x58)[0]
        self.cluster_count = struct.unpack_from("<I", boot, 0x5C)[0]
        self.root_cluster = struct.unpack_from("<I", boot, 0x60)[0]
        self.volume_serial = struct.unpack_from("<I", boot, 0x64)[0]
        self.fs_revision = struct.unpack_from("<H", boot, 0x68)[0]
        self.volume_flags = struct.unpack_from("<H", boot, 0x6A)[0]
        self.number_of_fats = boot[0x6E]
        self.drive_select = boot[0x6F]
        self.percent_in_use = boot[0x70]

        if self.fat_offset_sectors == 0 or self.fat_length_sectors == 0:
            raise ValueError("exFAT FAT 偏移/长度非法（为 0），卷可能已损坏")
        if self.heap_offset_sectors == 0 or self.cluster_count == 0:
            raise ValueError("exFAT ClusterHeap 偏移/簇数量非法（为 0），卷可能已损坏")
        if not self._valid_cluster(self.root_cluster):
            raise ValueError(
                f"exFAT 根目录簇号非法：{self.root_cluster}"
                f"（合法范围 2..{self.cluster_count + 1}）")

        self.fat_offset_bytes = self.fat_offset_sectors * self.bytes_per_sector
        self.fat_length_bytes = self.fat_length_sectors * self.bytes_per_sector
        self.heap_offset_bytes = self.heap_offset_sectors * self.bytes_per_sector

        # exFAT/TexFAT 允许 2 个 FAT，VolumeFlags 位 0 表示当前活动 FAT
        self.active_fat_index = 0
        if self.number_of_fats == 2 and (self.volume_flags & 0x0001):
            self.active_fat_index = 1
        self.active_fat_offset_bytes = (
            self.fat_offset_bytes + self.active_fat_index * self.fat_length_bytes)

        # 卷总字节数：优先用 VolumeLength，异常时按簇堆末端推算
        heap_end_sectors = (
            self.heap_offset_sectors + self.cluster_count * self.sectors_per_cluster)
        if self.volume_length_sectors and self.volume_length_sectors >= heap_end_sectors:
            self.volume_length_bytes = self.volume_length_sectors * self.bytes_per_sector
        else:
            self.volume_length_bytes = heap_end_sectors * self.bytes_per_sector
            if self.volume_length_sectors:
                self._boot_notes.append("VolumeLength 小于簇堆末端，已按簇堆推算卷长度")
        if self.fat_length_bytes < (self.cluster_count + 2) * 4:
            self._boot_notes.append("FAT 长度不足以容纳全部簇表项，卷可能已损坏")

    def _build_info(self) -> dict:
        """构造 SPEC 约定的 info dict（可 JSON 序列化）。"""
        major = (self.fs_revision >> 8) & 0xFF
        minor = self.fs_revision & 0xFF
        return {
            # SPEC 要求的最小字段集
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "fat_offset_bytes": self.fat_offset_bytes,
            "heap_offset_bytes": self.heap_offset_bytes,
            "cluster_count": self.cluster_count,
            "root_cluster": self.root_cluster,
            "volume_label": self._label,
            "volume_serial": self.volume_serial,
            "fs_revision": self.fs_revision,
            # 附加信息（便于 probe / 调试）
            "path": self.path,
            "offset": self.offset,
            "volume_serial_hex": "0x%08X" % self.volume_serial,
            "fs_revision_str": "%d.%02d" % (major, minor),
            "fat_offset": self.fat_offset_sectors,
            "fat_length": self.fat_length_sectors,
            "fat_length_bytes": self.fat_length_bytes,
            "heap_offset": self.heap_offset_sectors,
            "number_of_fats": self.number_of_fats,
            "active_fat": self.active_fat_index,
            "volume_flags": self.volume_flags,
            "percent_in_use": self.percent_in_use,
            "drive_select": self.drive_select,
            "partition_offset": self.partition_offset,
            "volume_length_sectors": self.volume_length_sectors,
            "volume_length_bytes": self.volume_length_bytes,
            "device_size": self._dev.size,
            "boot_signature_ok": self.boot_signature_ok,
            "volume_label_error": self._label_error,
            "notes": list(self._boot_notes),
        }

    def close(self) -> None:
        """关闭底层设备句柄。"""
        if getattr(self, "_dev", None) is not None:
            self._dev.close()
            self._dev = None

    def __enter__(self) -> "ExfatVolume":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # ------------------------------------------------------------- 底层读取
    def _valid_cluster(self, cluster: int) -> bool:
        """簇号是否落在合法数据簇范围 2..ClusterCount+1。"""
        try:
            cluster = int(cluster)
        except (TypeError, ValueError):
            return False
        return 2 <= cluster <= self.cluster_count + 1

    def _read(self, relative_offset: int, length: int) -> bytes:
        """从“卷内相对偏移”读取数据（自动加上卷起始 offset 并限制在卷内）。"""
        if getattr(self, "_dev", None) is None:
            raise ValueError("ExfatVolume 已关闭，不能再读取")
        if length <= 0 or relative_offset < 0:
            return b""
        if self.volume_length_bytes:
            if relative_offset >= self.volume_length_bytes:
                return b""
            length = min(length, self.volume_length_bytes - relative_offset)
        return self._dev.read_at(self.offset + relative_offset, length)

    def _fat_entry(self, cluster: int) -> Optional[int]:
        """读取 FAT 表中 *cluster* 的 u32 表项；越界/读失败返回 None。"""
        if cluster < 0:
            return None
        offset = cluster * 4
        start = self._fat_cache_start
        cache = self._fat_cache
        if not (start <= offset and offset + 4 <= start + len(cache)):
            block_start = (offset // _FAT_CACHE_BLOCK) * _FAT_CACHE_BLOCK
            block_length = min(_FAT_CACHE_BLOCK,
                               max(0, self.fat_length_bytes - block_start))
            if block_length < 4:
                raw = self._read(self.active_fat_offset_bytes + offset, 4)
                if len(raw) < 4:
                    return None
                return struct.unpack("<I", raw)[0]
            cache = self._read(self.active_fat_offset_bytes + block_start, block_length)
            self._fat_cache_start = block_start
            self._fat_cache = cache
            start = block_start
        relative = offset - start
        if relative + 4 > len(cache):
            return None
        return struct.unpack_from("<I", cache, relative)[0]

    @staticmethod
    def _is_end_of_chain(value: Optional[int]) -> bool:
        """FAT 表项是否为链尾（0xFFFFFFF8..0xFFFFFFFF）。"""
        return value is not None and (value & 0xFFFFFFFF) >= _FAT_EOC_MIN

    def _cluster_offset(self, cluster: int) -> int:
        """簇号 -> 卷内字节偏移。"""
        return self.heap_offset_bytes + (cluster - 2) * self.cluster_size

    def _read_cluster(self, cluster: int) -> bytes:
        """读取一个簇的数据（越界返回空）。"""
        if not self._valid_cluster(cluster):
            return b""
        return self._read(self._cluster_offset(cluster), self.cluster_size)

    def _walk_chain(self, first_cluster: int, max_clusters: int) -> Tuple[List[int], str]:
        """沿 FAT 链收集簇号。

        返回 ``(簇列表, 状态)``，状态取值：

        * ``'eoc'``    —— 正常走到链尾；
        * ``'enough'`` —— 已收集到 *max_clusters* 个簇（文件大小所需）；
        * ``'broken'`` —— 链中断/指向非法簇/成环。
        """
        clusters: List[int] = []
        seen = set()
        cluster = int(first_cluster)
        while True:
            if not self._valid_cluster(cluster):
                return clusters, "broken"
            if cluster in seen:
                return clusters, "broken"
            seen.add(cluster)
            clusters.append(cluster)
            next_cluster = self._fat_entry(cluster)
            if next_cluster is None:
                return clusters, "broken"
            if self._is_end_of_chain(next_cluster):
                return clusters, "eoc"
            if not self._valid_cluster(next_cluster):
                return clusters, "broken"
            if len(clusters) >= max_clusters:
                return clusters, "enough"
            cluster = next_cluster

    def _contiguous_clusters(self, first_cluster: int, needed: int) -> List[int]:
        """从 *first_cluster* 起按连续簇返回最多 *needed* 个合法簇号。"""
        clusters: List[int] = []
        for index in range(max(0, int(needed))):
            cluster = int(first_cluster) + index
            if not self._valid_cluster(cluster):
                break
            clusters.append(cluster)
        return clusters

    # ------------------------------------------------------------- 目录解析
    def _iter_directory_clusters(self, first_cluster: int) -> Iterator[int]:
        """产出目录占用的簇号（沿 FAT 链；已删除目录的链可能已失效，则只产出首簇）。"""
        seen = set()
        cluster = int(first_cluster)
        while self._valid_cluster(cluster) and cluster not in seen:
            seen.add(cluster)
            yield cluster
            next_cluster = self._fat_entry(cluster)
            if self._is_end_of_chain(next_cluster) or not self._valid_cluster(next_cluster):
                break
            cluster = next_cluster

    def _read_directory_bytes(self, first_cluster: int) -> bytes:
        """把一个目录（可能多簇）的内容整体读入内存。"""
        data = bytearray()
        for cluster in self._iter_directory_clusters(first_cluster):
            if len(data) >= _MAX_DIR_BYTES:
                raise ValueError(
                    f"目录过大（超过 {_MAX_DIR_BYTES} 字节），拒绝整体读入内存")
            chunk = self._read_cluster(cluster)
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def _parse_file_set(self, entry_set: bytes, parent_path: str,
                        entry_offset: int) -> Optional[Entry]:
        """解析一个 File 目录项集合（主项 + 次项），返回 Entry；无法识别时返回 None。"""
        if len(entry_set) < 2 * _DIR_ENTRY_SIZE:
            return None
        deleted = not (entry_set[0] & 0x80)                 # 0x80 位清零 = 已删除
        secondary_count = entry_set[1]
        attributes = struct.unpack_from("<H", entry_set, 4)[0]
        create_raw = struct.unpack_from("<I", entry_set, 8)[0]
        modify_raw = struct.unpack_from("<I", entry_set, 12)[0]
        access_raw = struct.unpack_from("<I", entry_set, 16)[0]

        # 找到 Stream Extension 次项（规范上它是第一个次项，这里容错搜索）
        stream = None
        for index in range(1, secondary_count + 1):
            start = index * _DIR_ENTRY_SIZE
            if start + _DIR_ENTRY_SIZE > len(entry_set):
                break
            if (entry_set[start] & 0x7F) == 0x40:
                stream = entry_set[start:start + _DIR_ENTRY_SIZE]
                break
        if stream is None:
            return None

        flags = stream[1]
        name_length = stream[3]
        name_hash_value = struct.unpack_from("<H", stream, 4)[0]
        valid_data_length = struct.unpack_from("<Q", stream, 8)[0]
        first_cluster = struct.unpack_from("<I", stream, 20)[0]
        data_length = struct.unpack_from("<Q", stream, 24)[0]

        # 收集所有 File Name 次项（0xC1/0x41），按顺序拼接 UTF-16LE
        name_parts: List[bytes] = []
        for index in range(1, secondary_count + 1):
            start = index * _DIR_ENTRY_SIZE
            if start + _DIR_ENTRY_SIZE > len(entry_set):
                break
            if (entry_set[start] & 0x7F) == 0x41:
                name_parts.append(entry_set[start + 2:start + _DIR_ENTRY_SIZE])
        name_bytes = b"".join(name_parts)[:max(0, int(name_length)) * 2]
        name = name_bytes.decode("utf-16-le", errors="replace")
        if not name:
            name = f"<unnamed_{entry_offset}>"

        attributes = int(attributes)
        return Entry(
            name=name,
            size=int(data_length),
            is_dir=bool(attributes & 0x0010),               # bit4 = Directory
            deleted=bool(deleted),
            first_cluster=int(first_cluster),
            attributes=attributes,
            mtime=decode_timestamp(modify_raw),
            ctime=decode_timestamp(create_raw),
            atime=decode_timestamp(access_raw),
            mtime_raw=modify_raw,
            ctime_raw=create_raw,
            atime_raw=access_raw,
            valid_data_length=int(valid_data_length),
            no_fat_chain=bool(flags & 0x02),                # bit1 = NoFatChain
            name_hash=int(name_hash_value),
            name_length=int(name_length),
            secondary_count=int(secondary_count),
            parent_path=parent_path,
            entry_offset=int(entry_offset),
        )

    def _parse_directory(self, raw: bytes, parent_path: str,
                         is_root: bool = False) -> Tuple[List[Entry], Optional[str]]:
        """解析一个目录的原始字节，返回 ``(Entry 列表, 卷标或 None)``。"""
        entries: List[Entry] = []
        label: Optional[str] = None
        position = 0
        total = len(raw)
        while position + _DIR_ENTRY_SIZE <= total:
            entry_type = raw[position]
            if entry_type == 0x00:                          # 目录结束标记
                break
            code = entry_type & 0x7F
            if code == 0x05:
                # File Directory Entry：0x85 在用 / 0x05 已删除，后面跟 SecondaryCount 个次项
                secondary_count = raw[position + 1]
                set_size = (1 + secondary_count) * _DIR_ENTRY_SIZE
                if position + set_size > total:
                    break                                   # 目录项集合被截断，停止
                entry = self._parse_file_set(
                    raw[position:position + set_size], parent_path, position)
                if entry is not None:
                    entries.append(entry)
                position += set_size
                continue
            if is_root and entry_type == 0x83:              # Volume Label（仅根目录）
                char_count = min(raw[position + 1], 11)
                label_bytes = raw[position + 2:position + 2 + char_count * 2]
                label = label_bytes.decode("utf-16-le", errors="replace")
            # 其他元数据项（分配位图 0x81 / Up-case 表 0x82 / Volume GUID 等）跳过 1 项
            position += _DIR_ENTRY_SIZE
        return entries, label

    def _read_volume_label(self) -> str:
        """读取根目录里的卷标（没有卷标时为 ""）。"""
        raw = self._read_directory_bytes(self.root_cluster)
        _entries, label = self._parse_directory(raw, "", is_root=True)
        return label or ""

    def list_entries(self, include_deleted: bool = True) -> List[Entry]:
        """遍历目录树，返回全部文件/目录 Entry（默认包含已删除项）。

        * 已删除目录会被列出，但不会递归进去（其簇可能已被释放）；
        * 返回顺序为深度优先，与目录中项目出现顺序一致。
        """
        result: List[Entry] = []
        self._walk_directory(self.root_cluster, "", include_deleted,
                             result, set(), 0)
        return result

    def _walk_directory(self, cluster: int, parent_path: str, include_deleted: bool,
                        out: List[Entry], visited: set, depth: int) -> None:
        """递归遍历目录（内部用）。"""
        if depth > _MAX_DIR_DEPTH or cluster in visited:
            return
        visited.add(cluster)
        raw = self._read_directory_bytes(cluster)
        entries, _label = self._parse_directory(raw, parent_path)
        for entry in entries:
            if entry.deleted and not include_deleted:
                continue
            out.append(entry)
            if entry.is_dir and not entry.deleted and self._valid_cluster(entry.first_cluster):
                self._walk_directory(entry.first_cluster, entry.path, include_deleted,
                                     out, visited, depth + 1)

    # ------------------------------------------------------------- 文件数据
    def _effective_size(self, entry: Entry, max_bytes: Optional[int] = None) -> int:
        """计算要读取的字节数。

        删除文件若 ValidDataLength 小于 DataLength（DataLength 之后可能是未定义数据），
        优先恢复有效数据长度；否则按 DataLength 读取。
        """
        size = max(0, int(entry.size or 0))
        if entry.deleted:
            valid = int(entry.valid_data_length or 0)
            if 0 < valid < size:
                size = valid
        if max_bytes is not None:
            size = min(size, max(0, int(max_bytes)))
        return size

    def _plan_file(self, entry: Entry, total: int) -> Tuple[List[int], bool]:
        """规划读取 *total* 字节所需簇，返回 ``(簇列表, 是否完整可信)``。

        * 正常文件：沿 FAT 链读取；NoFatChain 标志置位的连续文件直接按连续簇读；
        * 删除文件：FAT 链通常已被清空（表项为 0/非法），此时按“连续簇”回退，
          并返回不可信标记；
        * FAT 链中断时返回已能定位的簇并标记不完整。
        """
        needed = max(1, (int(total) + self.cluster_size - 1) // self.cluster_size)
        first_cluster = int(entry.first_cluster)
        if not self._valid_cluster(first_cluster):
            return [], False

        if entry.no_fat_chain:
            # 连续分配（Stream 扩展项 NoFatChain=1）：数据簇号连续递增
            clusters = self._contiguous_clusters(first_cluster, needed)
            return clusters, len(clusters) == needed

        chain, state = self._walk_chain(first_cluster, needed)
        if state != "broken" and len(chain) >= needed:
            return chain[:needed], True

        if entry.deleted:
            # 删除文件的 FAT 链已失效：按“文件原本连续存放”假设回退读取
            return self._contiguous_clusters(first_cluster, needed), False

        return chain, False

    def _iter_cluster_chunks(self, clusters: List[int], total: int) -> Iterator[bytes]:
        """按簇产出文件数据块，最多 *total* 字节（流式，不整体读入内存）。"""
        remaining = max(0, int(total))
        for cluster in clusters:
            if remaining <= 0:
                break
            chunk = self._read_cluster(cluster)
            if not chunk:
                break
            take = min(len(chunk), remaining)
            yield chunk[:take]
            remaining -= take

    def iter_entry_chunks(self, entry: Entry,
                          max_bytes: Optional[int] = None) -> Iterator[bytes]:
        """流式产出文件数据块（逐簇），供大文件流式写盘/校验使用。"""
        if entry.is_dir:
            raise ValueError(f"目录没有数据流，不能读取内容：{entry.path!r}")
        total = self._effective_size(entry, max_bytes)
        if total <= 0:
            return
        clusters, _full = self._plan_file(entry, total)
        yield from self._iter_cluster_chunks(clusters, total)

    def read_entry_data(self, entry: Entry,
                        max_bytes: Optional[int] = None) -> Tuple[bytes, bool]:
        """读取文件内容，返回 ``(数据, 是否完整可信)``。

        * 正常文件沿 FAT 链（或连续簇）读取，取满 DataLength 时 ``recovered_full=True``；
        * 删除文件若 FAT 链已失效，则按连续簇回退读取，``recovered_full=False``，
          表示“仅当文件原本连续存放时内容才完整”；
        * FAT 链中断/簇越界时会返回已读到的部分数据并标记 ``False``；
        * 目录项请使用 ``list_entries`` 获取，目录没有数据流，调用会抛 ValueError。
        """
        if entry.is_dir:
            raise ValueError(f"目录没有数据流，不能读取内容：{entry.path!r}")
        total = self._effective_size(entry, max_bytes)
        if total <= 0:
            return b"", True
        clusters, recovered_full = self._plan_file(entry, total)
        data = bytearray()
        for chunk in self._iter_cluster_chunks(clusters, total):
            data += chunk
        if len(data) < total:
            recovered_full = False
        return bytes(data[:total]), bool(recovered_full)

    @staticmethod
    def _recovery_note(entry: Entry) -> str:
        """解释“为什么 recovered_full=False”。"""
        if entry.deleted:
            return ("删除文件的 FAT 链已失效，按连续簇回退读取；"
                    "仅当文件原本连续存放时内容才完整")
        if entry.no_fat_chain:
            return "连续分配的簇不足（可能越界或卷被截断），内容不完整"
        return "FAT 链中断或数据簇不足，内容不完整"

    # ------------------------------------------------------------- 恢复
    @staticmethod
    def _unique_path(directory: str, name: str) -> str:
        """在 *directory* 下生成不冲突的输出路径（重名自动加 -1/-2 后缀）。"""
        candidate = os.path.join(directory, name)
        if not os.path.exists(candidate):
            return candidate
        stem, extension = os.path.splitext(name)
        if not stem:
            stem, extension = name, ""
        index = 1
        while True:
            candidate = os.path.join(directory, f"{stem}-{index}{extension}")
            if not os.path.exists(candidate):
                return candidate
            index += 1
            if index > 100000:
                raise OSError(f"无法为 {name!r} 生成不重名的输出文件名")

    def _dump_entry(self, entry: Entry, target_path: str) -> Tuple[int, bool, Optional[str]]:
        """把单个文件流式写入 *target_path*，返回 ``(写入字节数, 是否完整, 说明)``。"""
        total = self._effective_size(entry)
        if total > 0:
            clusters, recovered_full = self._plan_file(entry, total)
        else:
            clusters, recovered_full = [], True
        written = 0
        with open(target_path, "wb") as handle:
            for chunk in self._iter_cluster_chunks(clusters, total):
                handle.write(chunk)
                written += len(chunk)
        if written < total:
            recovered_full = False
        if entry.mtime:
            try:                                     # best-effort 恢复修改时间
                os.utime(target_path, (entry.mtime, entry.mtime))
            except OSError:
                pass
        note = None if recovered_full else self._recovery_note(entry)
        return written, bool(recovered_full), note

    def recover(self, outdir: str, include_deleted: bool = True) -> dict:
        """把卷内文件按原目录结构恢复到 *outdir*，返回统计 dict。

        返回结构::

            {"files": [{"name", "path", "size", "declared_size", "deleted",
                        "recovered_full", "first_cluster", "attributes", "mtime",
                        "note"?}, ...],
             "count": n,
             "errors": ["..."],
             "outdir": "..."}

        只读：不会写回设备/镜像；输出目录自动创建，重名自动加 ``-1``/``-2`` 后缀。
        已删除目录不递归（其内容簇可能已经释放）。
        """
        outdir = os.path.abspath(str(outdir))
        os.makedirs(outdir, exist_ok=True)
        files: List[dict] = []
        errors: List[str] = []
        self._recover_directory(self.root_cluster, outdir, "", include_deleted,
                                files, errors, set(), 0)
        return {
            "files": files,
            "count": len(files),
            "errors": errors,
            "outdir": outdir,
        }

    def _recover_directory(self, cluster: int, out_dir: str, parent_path: str,
                           include_deleted: bool, files: List[dict],
                           errors: List[str], visited: set, depth: int) -> None:
        """递归恢复一个目录下的内容（内部用）。"""
        if depth > _MAX_DIR_DEPTH or cluster in visited:
            return
        visited.add(cluster)
        try:
            raw = self._read_directory_bytes(cluster)
            entries, _label = self._parse_directory(raw, parent_path)
        except Exception as exc:      # 目录损坏：记录错误后继续恢复其他目录
            errors.append(
                "读取目录失败（cluster=%s, path=%s）: %s: %s"
                % (cluster, parent_path or "/", type(exc).__name__, exc))
            return

        pending_dirs: List[Tuple[int, str, str]] = []
        for entry in entries:
            if entry.deleted and not include_deleted:
                continue
            if entry.is_dir:
                if entry.deleted:
                    continue          # 已删除目录的簇可能已释放，不递归
                dir_path = self._unique_path(out_dir, _sanitize_name(entry.name))
                try:
                    os.makedirs(dir_path, exist_ok=True)
                except OSError as exc:
                    errors.append(f"创建目录失败 {dir_path!r}: {exc}")
                    continue
                pending_dirs.append((entry.first_cluster, dir_path, entry.path))
                continue

            target_path = self._unique_path(out_dir, _sanitize_name(entry.name))
            try:
                written, recovered_full, note = self._dump_entry(entry, target_path)
            except Exception as exc:      # 单个文件失败不影响其它文件恢复
                errors.append(
                    f"恢复文件失败 {entry.path!r}: {type(exc).__name__}: {exc}")
                try:                      # 清理写了一半的残留文件
                    if os.path.exists(target_path):
                        os.remove(target_path)
                except OSError:
                    pass
                continue
            item = {
                "name": entry.name,
                "path": target_path,
                "size": written,
                "declared_size": int(entry.size),
                "deleted": bool(entry.deleted),
                "recovered_full": bool(recovered_full),
                "first_cluster": int(entry.first_cluster),
                "attributes": int(entry.attributes),
                "mtime": entry.mtime,
            }
            if note:
                item["note"] = note
            files.append(item)

        for sub_cluster, sub_dir, sub_path in pending_dirs:
            self._recover_directory(sub_cluster, sub_dir, sub_path, include_deleted,
                                    files, errors, visited, depth + 1)


# --------------------------------------------------------------------------- 对外入口
def open_volume(path: str, offset: int = 0) -> ExfatVolume:
    """打开一个 exFAT 卷/镜像（只读）。

    :param path: 镜像文件路径（如 ``F:\\img\\usb.img``）或 Windows 设备路径
                 （如 ``r'\\\\.\\G:'``、``r'\\\\.\\PhysicalDrive3'``）
    :param offset: 卷在 *path* 中的起始字节偏移（分区起始位置；无分区表时为 0）
    :return: :class:`ExfatVolume`
    :raises ValueError: 指定位置不是合法 exFAT 卷（签名/关键字段校验失败）
    :raises OSError: 路径无法打开
    """
    return ExfatVolume(path, offset=offset)


# --------------------------------------------------------------------------- 自测
if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        print("用法: python fdlib/exfat.py <镜像或设备> [offset字节]")
        sys.exit(2)
    _offset = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0
    with open_volume(sys.argv[1], _offset) as _volume:
        print(json.dumps(_volume.info, ensure_ascii=False, indent=2))
        for _entry in _volume.list_entries(include_deleted=True):
            _kind = "DIR " if _entry.is_dir else "FILE"
            _flag = " [已删除]" if _entry.deleted else ""
            print("%s %8d  %s%s" % (_kind, _entry.size, _entry.path, _flag))
    sys.exit(0)
