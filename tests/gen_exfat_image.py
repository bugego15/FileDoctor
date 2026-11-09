# -*- coding: utf-8 -*-
"""手工构造一个最小合法 exFAT 测试镜像（纯标准库，不依赖任何第三方工具）。

镜像规格（与 SPEC/任务要求一致）
--------------------------------
* 扇区 512 字节（BytesPerSectorShift = 9）
* 簇 4KB（SectorsPerClusterShift = 3，即 8 扇区/簇）
* 1 个 FAT，16 个簇的簇堆（cluster 2..17）
* 根目录 1 簇（cluster 4）
* 1 个正常文件 hello.txt：5000 字节，占 2 个簇，且**故意不连续**（cluster 5 -> 8），
  用来验证解析器确实沿 FAT 链读取，而不是按“连续簇”猜测；
* 1 个已删除文件 gone.bin：3000 字节，占 1 个簇（cluster 6）。删除后该簇的
  FAT 表项被清 0、分配位图对应位被清 0，但数据仍留在原簇中，用于验证
  解析器的“连续簇回退”恢复路径（结果应标记 recovered_full=False）。

按 exFAT 规范写入的关键结构
---------------------------
* VBR（Main Boot Sector）：JumpBoot、FileSystemName="EXFAT   "、PartitionOffset、
  VolumeLength、FatOffset/FatLength、ClusterHeapOffset、ClusterCount、
  FirstClusterOfRootDirectory、VolumeSerialNumber、FileSystemRevision=0x0100、
  VolumeFlags、BytesPerSectorShift、SectorsPerClusterShift、NumberOfFats、
  DriveSelect、PercentInUse、BootSignature=0x55AA；
* Main Boot Checksum（sector 11）：对 sector 0..10 做“循环右移 1 位再相加”，
  跳过 VolumeFlags(0x6A..0x6B) 与 PercentInUse(0x70)，4 份 u32 重复；
* 备份引导区（sector 12..23）为引导区的完整副本（sector 23 = 备份校验）；
* FAT 表（每项 u32，FAT[0]=0xFFFFFFF8、FAT[1]=0xFFFFFFFF、链尾 0xFFFFFFF8..0xFFFFFFFF）；
* 根目录元数据目录项：卷标(0x83) + 分配位图(0x81) + Up-case 表(0x82)；
* 文件目录项集合：File Entry(0x85/0x05) + Stream Extension(0xC0/0x40) + File Name(0xC1/0x41)，
  并写入规范的 SetChecksum、NameHash、时间戳；
* 分配位图：每簇 1 bit，bit0 对应 cluster 2；
* Up-case 表使用压缩形式：单个 0xFFFF 表示其余码点恒等映射（最小合法表）。

镜像布局（扇区编号）
--------------------
=====  ==========================================
0      主引导扇区（Main Boot Sector）
1..10  扩展引导扇区 / OEM 参数 / 保留（本生成器留 0）
11     主引导校验（BootChecksum，4 份 u32）
12..23 备份引导区（与 0..11 完全一致）
24     FAT（1 个扇区足够容纳 18 个表项）
25..   簇堆起点：25 + (cluster-2)*8 扇区
=====  ==========================================

簇分配（最小镜像）
  2  分配位图
  3  Up-case 表
  4  根目录
  5  正常文件第 1 簇（FAT[5] = 8）
  6  已删除文件的数据簇（FAT[6] = 0，已释放）
  7  空闲
  8  正常文件第 2 簇（FAT[8] = 链尾）

用法
----
作为命令行工具生成镜像::

    python tests/gen_exfat_image.py 输出路径 [--offset 字节] [--subdir]

作为模块使用（tests/test_exfat.py 就是这样用的）::

    import gen_exfat_image
    meta = gen_exfat_image.build_exfat_image("F:\\tmp\\mini.img")
    meta["normal_file"]["data"]   # 正常文件原始内容
"""

from __future__ import annotations

import datetime
import json
import os
import struct
import sys

# --------------------------------------------------------------------------- 几何参数
SECTOR = 512                              # 扇区大小（BytesPerSector = 2^9）
BYTES_PER_SECTOR_SHIFT = 9
SECTORS_PER_CLUSTER = 8                   # 4KB 簇（SectorsPerCluster = 2^3）
SECTORS_PER_CLUSTER_SHIFT = 3
CLUSTER_SIZE = SECTOR * SECTORS_PER_CLUSTER

FAT_OFFSET_SEC = 24                       # 引导区（主 12 + 备份 12）之后
FAT_LENGTH_SEC = 1                        # (16 + 2) * 4 = 72 字节，1 个扇区足够
CLUSTER_HEAP_OFFSET_SEC = FAT_OFFSET_SEC + FAT_LENGTH_SEC          # 25
CLUSTER_COUNT = 16
VOLUME_LENGTH_SEC = CLUSTER_HEAP_OFFSET_SEC + CLUSTER_COUNT * SECTORS_PER_CLUSTER  # 153
ROOT_CLUSTER = 4

VOLUME_LABEL = "FDTEST"
VOLUME_SERIAL = 0x12345678
FS_REVISION = 0x0100                      # exFAT 1.00

# --------------------------------------------------------------------------- 簇分配
BITMAP_CLUSTER = 2
UPCASE_CLUSTER = 3
NORMAL_FILE_CLUSTERS = (5, 8)             # 故意不连续，验证 FAT 链
DELETED_FILE_CLUSTER = 6                  # 删除后仍保留数据
SUBDIR_CLUSTER = 9
NESTED_FILE_CLUSTER = 10

NORMAL_FILE_NAME = "hello.txt"
DELETED_FILE_NAME = "gone.bin"
SUBDIR_NAME = "sub"
NESTED_FILE_NAME = "nested.txt"

# 固定时间戳（2024-05-01 12:34:56），文件中按 UTC 解释
_STAMP = datetime.datetime(2024, 5, 1, 12, 34, 56)

FAT_EOC = 0xFFFFFFFF                      # 链尾（0xFFFFFFF8..0xFFFFFFFF 都合法）
FAT_RESERVED_0 = 0xFFFFFFF8               # FAT[0] 低字节 0xF8 表示固定磁盘介质


# --------------------------------------------------------------------------- 小工具
def _pattern(length: int, seed: int) -> bytes:
    """生成一段确定性的伪随机字节，便于测试逐字节比对。"""
    return bytes(((index * seed + 0x11) & 0xFF) for index in range(length))


def encode_timestamp(moment: datetime.datetime) -> int:
    """把 datetime 编码成 exFAT 32 位时间戳。

    位域：bits0-4 秒/2、5-10 分、11-15 时、16-20 日、21-24 月、25-31 年（-1980）。
    """
    return (
        (((moment.year - 1980) & 0x7F) << 25)
        | ((moment.month & 0x0F) << 21)
        | ((moment.day & 0x1F) << 16)
        | ((moment.hour & 0x1F) << 11)
        | ((moment.minute & 0x3F) << 5)
        | ((moment.second // 2) & 0x1F)
    )


def timestamp_epoch(moment: datetime.datetime) -> int:
    """测试用：把生成器使用的固定时间戳按 UTC 换算成 epoch 秒。"""
    return int(moment.replace(tzinfo=datetime.timezone.utc).timestamp())


def rotate_add_checksum(data: bytes, skip=()) -> int:
    """exFAT 校验算法：每个字节先把 32 位校验值循环右移 1 位，再相加。"""
    checksum = 0
    for index, byte in enumerate(data):
        if index in skip:
            continue
        checksum = (((checksum << 31) | (checksum >> 1)) & 0xFFFFFFFF) + byte
        checksum &= 0xFFFFFFFF
    return checksum


def boot_checksum(boot_region: bytes) -> int:
    """引导区校验：覆盖 sector 0..10，跳过 VolumeFlags(106,107) 与 PercentInUse(112)。"""
    return rotate_add_checksum(boot_region[:11 * SECTOR], skip=(106, 107, 112))


def entry_set_checksum(entry_set: bytes) -> int:
    """目录项集合 SetChecksum（16 位）。

    算法：把 16 位校验值循环右移 1 位后加上一个字节值（每步截断到 16 位），
    跳过第一个目录项里的 SetChecksum 字段本身（集合内的字节偏移 2、3）。
    """
    checksum = 0
    for index, byte in enumerate(entry_set):
        if index in (2, 3):
            continue
        checksum = ((checksum >> 1) | ((checksum & 1) << 15)) & 0xFFFF
        checksum = (checksum + byte) & 0xFFFF
    return checksum


def name_hash(name: str) -> int:
    """计算 exFAT 文件名 NameHash（16 位：每字符先循环右移 1 位，再相加）。

    本生成器的 Up-case 表是压缩恒等映射（0xFFFF），所以 UpCase(c) == c。
    """
    raw = name.encode("utf-16-le")
    units = struct.unpack("<%dH" % (len(raw) // 2), raw)
    value = 0
    for unit in units:
        value = ((value >> 1) | ((value & 1) << 15)) & 0xFFFF
        value = (value + (unit & 0xFFFF)) & 0xFFFF
    return value


# --------------------------------------------------------------------------- 目录项构造
def build_volume_label_entry(label: str) -> bytes:
    """构造卷标目录项（EntryType=0x83）。"""
    raw = label.encode("utf-16-le")
    char_count = len(raw) // 2
    if char_count > 11:
        raise ValueError("exFAT 卷标最多 11 个 UTF-16 字符")
    entry = bytearray(32)
    entry[0] = 0x83                       # 在用 + Volume Label
    entry[1] = char_count
    entry[2:2 + len(raw)] = raw
    return bytes(entry)


def build_bitmap_entry(first_cluster: int, data_length: int) -> bytes:
    """构造分配位图目录项（EntryType=0x81）。"""
    entry = bytearray(32)
    entry[0] = 0x81                       # 在用 + Allocation Bitmap
    entry[1] = 0                          # Flags：第一个分配位图
    struct.pack_into("<I", entry, 20, first_cluster)
    struct.pack_into("<Q", entry, 24, data_length)
    return bytes(entry)


def build_upcase_entry(first_cluster: int, data_length: int, checksum: int) -> bytes:
    """构造 Up-case 表目录项（EntryType=0x82）。"""
    entry = bytearray(32)
    entry[0] = 0x82                       # 在用 + Up-case Table
    struct.pack_into("<I", entry, 4, checksum & 0xFFFFFFFF)
    struct.pack_into("<I", entry, 20, first_cluster)
    struct.pack_into("<Q", entry, 24, data_length)
    return bytes(entry)


def build_file_entry_set(name, *, attributes, first_cluster, data_length,
                         deleted=False, no_fat_chain=False,
                         valid_data_length=None, timestamp=_STAMP):
    """构造一个完整的 File 目录项集合（File Entry + Stream Extension + File Name 项）。

    :param name: 文件/目录名（UTF-16LE 存储，每项最多 15 个字符）
    :param attributes: 文件属性位（bit4=0x10 表示目录，bit5=0x20 表示归档文件）
    :param first_cluster: 首簇号（目录项里的 FirstCluster，空文件可为 0）
    :param data_length: DataLength（目录可为 0）
    :param deleted: True 时所有目录项的“在用”位（0x80）清零
    :param no_fat_chain: Stream 扩展项的 NoFatChain 标志（1 = 数据连续、不走 FAT 链）
    :param valid_data_length: ValidDataLength；None 表示等于 DataLength
    :param timestamp: 创建/修改/访问时间（本生成器三处写同一值）
    """
    if valid_data_length is None:
        valid_data_length = data_length

    name_bytes = name.encode("utf-16-le")
    name_length = len(name_bytes) // 2
    name_entry_count = max(1, (name_length + 14) // 15)
    secondary_count = 1 + name_entry_count
    flush = 0x00 if deleted else 0x80     # EntryType 的“在用”位

    stamp_raw = encode_timestamp(timestamp)

    # ---- File Directory Entry -------------------------------------------------
    primary = bytearray(32)
    primary[0] = 0x05 | flush             # 0x85 在用 / 0x05 删除
    primary[1] = secondary_count
    # primary[2:4] = SetChecksum（全部目录项组装完后回填）
    struct.pack_into("<H", primary, 4, attributes)
    struct.pack_into("<I", primary, 8, stamp_raw)      # CreateTimestamp
    struct.pack_into("<I", primary, 12, stamp_raw)     # LastModifiedTimestamp
    struct.pack_into("<I", primary, 16, stamp_raw)     # LastAccessedTimestamp
    primary[20] = 0                       # Create10msIncrement
    primary[21] = 0                       # LastModified10msIncrement
    primary[22] = 0                       # CreateUtcOffset
    primary[23] = 0                       # LastModifiedUtcOffset
    primary[24] = 0                       # LastAccessedUtcOffset

    # ---- Stream Extension -----------------------------------------------------
    stream = bytearray(32)
    stream[0] = 0x40 | flush              # 0xC0 在用 / 0x40 删除
    stream[1] = 0x01 | (0x02 if no_fat_chain else 0x00)   # AllocationPossible / NoFatChain
    stream[3] = name_length
    struct.pack_into("<H", stream, 4, name_hash(name))
    struct.pack_into("<Q", stream, 8, valid_data_length)
    struct.pack_into("<I", stream, 20, first_cluster)
    struct.pack_into("<Q", stream, 24, data_length)

    # ---- File Name 项（每个 15 个 UTF-16 字符） --------------------------------
    name_entries = bytearray()
    for offset in range(0, len(name_bytes), 30):
        chunk = name_bytes[offset:offset + 30]
        entry = bytearray(32)
        entry[0] = 0x41 | flush           # 0xC1 在用 / 0x41 删除
        entry[2:2 + len(chunk)] = chunk
        name_entries += entry

    # ---- 组装并回填 SetChecksum ----------------------------------------------
    entry_set = bytearray(primary + stream + name_entries)
    struct.pack_into("<H", entry_set, 2, entry_set_checksum(bytes(entry_set)))
    return bytes(entry_set)


# --------------------------------------------------------------------------- 镜像构造
def _cluster_offset(cluster: int) -> int:
    """簇号 -> 卷内字节偏移。"""
    return (CLUSTER_HEAP_OFFSET_SEC + (cluster - 2) * SECTORS_PER_CLUSTER) * SECTOR


def _build_fat(fat_values) -> bytes:
    """构造 FAT 区（1 个扇区）。fat_values: 簇号 -> 链尾标记 'EOC' 或下一簇号。"""
    fat = bytearray(FAT_LENGTH_SEC * SECTOR)

    def put(cluster: int, value: int) -> None:
        struct.pack_into("<I", fat, cluster * 4, value & 0xFFFFFFFF)

    put(0, FAT_RESERVED_0)                # 介质描述 + 保留
    put(1, FAT_EOC)                       # 保留
    # 其余簇默认为 0（未分配）；删除文件的簇显式写 0 表示链已清空
    for cluster, next_cluster in sorted(fat_values.items()):
        put(cluster, FAT_EOC if next_cluster == "EOC" else int(next_cluster))
    return bytes(fat)


def _build_bitmap(allocated_clusters) -> bytes:
    """构造分配位图文件内容（整簇，前 ceil(ClusterCount/8) 字节有意义）。"""
    bitmap = bytearray(CLUSTER_SIZE)
    for cluster in allocated_clusters:
        bit = cluster - 2
        bitmap[bit // 8] |= 1 << (bit % 8)
    return bytes(bitmap)


def build_exfat_image(path: str, offset: int = 0, with_subdir: bool = False,
                      normal_data: bytes = None, deleted_data: bytes = None) -> dict:
    """生成最小合法 exFAT 镜像写入 *path*，返回描述信息 dict（供测试断言使用）。

    :param path: 输出镜像文件路径
    :param offset: 卷在镜像文件中的起始字节偏移（>0 用于测试 open_volume(offset=...)）
    :param with_subdir: 额外增加根目录下的子目录 ``sub`` 及其中文件 ``nested.txt``，
                        用于验证 recover() 保留目录结构
    :param normal_data: 正常文件内容（默认 5000 字节确定性数据，占 2 个不连续簇）
    :param deleted_data: 已删除文件内容（默认 3000 字节确定性数据，占 1 个簇）
    :return: 镜像描述 dict，含卷参数、文件内容、簇分配、校验值等
    """
    if offset < 0:
        raise ValueError("offset 不能为负数")
    normal_data = bytes(normal_data) if normal_data is not None else _pattern(5000, 37)
    deleted_data = bytes(deleted_data) if deleted_data is not None else _pattern(3000, 91)
    nested_data = b"nested file inside sub directory\n" * 8

    if len(normal_data) > 2 * CLUSTER_SIZE:
        raise ValueError("本生成器的正常文件最多占 2 个簇")
    if len(deleted_data) > CLUSTER_SIZE:
        raise ValueError("本生成器的删除文件最多占 1 个簇")

    # ---- 1. 簇分配与 FAT 链 ---------------------------------------------------
    allocated = [BITMAP_CLUSTER, UPCASE_CLUSTER, ROOT_CLUSTER,
                 NORMAL_FILE_CLUSTERS[0], NORMAL_FILE_CLUSTERS[1]]
    fat_values = {
        BITMAP_CLUSTER: "EOC",
        UPCASE_CLUSTER: "EOC",
        ROOT_CLUSTER: "EOC",
        NORMAL_FILE_CLUSTERS[0]: NORMAL_FILE_CLUSTERS[1],   # 5 -> 8（不连续）
        NORMAL_FILE_CLUSTERS[1]: "EOC",
        DELETED_FILE_CLUSTER: 0,                             # 删除后 FAT 链被清空
    }
    if with_subdir:
        allocated += [SUBDIR_CLUSTER, NESTED_FILE_CLUSTER]
        fat_values[SUBDIR_CLUSTER] = "EOC"
        fat_values[NESTED_FILE_CLUSTER] = "EOC"

    fat = _build_fat(fat_values)
    bitmap = _build_bitmap(allocated)
    bitmap_size = (CLUSTER_COUNT + 7) // 8               # 16 簇 -> 2 字节

    # ---- 2. Up-case 表（压缩形式：单个 0xFFFF 表示恒等映射） --------------------
    upcase = bytearray(CLUSTER_SIZE)
    upcase[0:2] = b"\xFF\xFF"
    upcase_size = 2
    upcase_checksum = rotate_add_checksum(upcase[:upcase_size])

    # ---- 3. 根目录 ------------------------------------------------------------
    root = bytearray(CLUSTER_SIZE)
    position = 0

    def append_entry(data: bytes) -> None:
        nonlocal position
        root[position:position + len(data)] = data
        position += len(data)

    append_entry(build_volume_label_entry(VOLUME_LABEL))
    append_entry(build_bitmap_entry(BITMAP_CLUSTER, bitmap_size))
    append_entry(build_upcase_entry(UPCASE_CLUSTER, upcase_size, upcase_checksum))

    if with_subdir:
        # 目录的 Stream Extension：DataLength/ValidDataLength 按规范为 0，
        # 目录内容通过 FAT 链定位（本生成器目录只占 1 簇，FAT[9] = 链尾）。
        append_entry(build_file_entry_set(
            SUBDIR_NAME, attributes=0x10, first_cluster=SUBDIR_CLUSTER,
            data_length=0, valid_data_length=0,
        ))
    append_entry(build_file_entry_set(
        NORMAL_FILE_NAME, attributes=0x20, first_cluster=NORMAL_FILE_CLUSTERS[0],
        data_length=len(normal_data),
    ))
    append_entry(build_file_entry_set(
        DELETED_FILE_NAME, attributes=0x20, first_cluster=DELETED_FILE_CLUSTER,
        data_length=len(deleted_data), deleted=True,
    ))

    # ---- 4. 子目录内容 --------------------------------------------------------
    subdir = bytearray(CLUSTER_SIZE)
    if with_subdir:
        nested_set = build_file_entry_set(
            NESTED_FILE_NAME, attributes=0x20, first_cluster=NESTED_FILE_CLUSTER,
            data_length=len(nested_data),
        )
        subdir[0:len(nested_set)] = nested_set

    # ---- 5. 主引导扇区（VBR） --------------------------------------------------
    percent_in_use = min(100, (len(allocated) * 100) // CLUSTER_COUNT)
    boot_sector = bytearray(SECTOR)
    boot_sector[0:3] = b"\xEB\x76\x90"                 # JumpBoot
    boot_sector[3:11] = b"EXFAT   "                    # FileSystemName
    struct.pack_into("<Q", boot_sector, 0x40, 0)       # PartitionOffset（本镜像无分区表）
    struct.pack_into("<Q", boot_sector, 0x48, VOLUME_LENGTH_SEC)
    struct.pack_into("<I", boot_sector, 0x50, FAT_OFFSET_SEC)
    struct.pack_into("<I", boot_sector, 0x54, FAT_LENGTH_SEC)
    struct.pack_into("<I", boot_sector, 0x58, CLUSTER_HEAP_OFFSET_SEC)
    struct.pack_into("<I", boot_sector, 0x5C, CLUSTER_COUNT)
    struct.pack_into("<I", boot_sector, 0x60, ROOT_CLUSTER)
    struct.pack_into("<I", boot_sector, 0x64, VOLUME_SERIAL)
    struct.pack_into("<H", boot_sector, 0x68, FS_REVISION)   # 1.00
    struct.pack_into("<H", boot_sector, 0x6A, 0)             # VolumeFlags
    boot_sector[0x6C] = BYTES_PER_SECTOR_SHIFT
    boot_sector[0x6D] = SECTORS_PER_CLUSTER_SHIFT
    boot_sector[0x6E] = 1                                    # NumberOfFats
    boot_sector[0x6F] = 0x80                                 # DriveSelect
    boot_sector[0x70] = percent_in_use                       # PercentInUse
    boot_sector[0x1FE] = 0x55
    boot_sector[0x1FF] = 0xAA

    # ---- 6. 引导区 + BootChecksum（sector 11）+ 备份引导区（sector 12..23） -----
    boot_region = bytearray(12 * SECTOR)
    boot_region[0:SECTOR] = boot_sector
    checksum = boot_checksum(bytes(boot_region))
    struct.pack_into("<IIII", boot_region, 11 * SECTOR,
                     checksum, checksum, checksum, checksum)

    # ---- 7. 组装卷数据 --------------------------------------------------------
    volume = bytearray(VOLUME_LENGTH_SEC * SECTOR)
    volume[0:len(boot_region)] = boot_region
    volume[len(boot_region):2 * len(boot_region)] = boot_region          # 备份引导区
    volume[FAT_OFFSET_SEC * SECTOR:FAT_OFFSET_SEC * SECTOR + len(fat)] = fat

    def put_cluster(cluster: int, data: bytes) -> None:
        start = _cluster_offset(cluster)
        payload = bytes(data)[:CLUSTER_SIZE]
        volume[start:start + len(payload)] = payload

    put_cluster(BITMAP_CLUSTER, bitmap)
    put_cluster(UPCASE_CLUSTER, bytes(upcase))
    put_cluster(ROOT_CLUSTER, bytes(root))
    put_cluster(NORMAL_FILE_CLUSTERS[0], normal_data[:CLUSTER_SIZE])
    put_cluster(NORMAL_FILE_CLUSTERS[1], normal_data[CLUSTER_SIZE:])
    # 已删除文件的数据仍然留在“已释放”的簇里（这是恢复的前提）
    put_cluster(DELETED_FILE_CLUSTER, deleted_data[:CLUSTER_SIZE])
    if with_subdir:
        put_cluster(SUBDIR_CLUSTER, bytes(subdir))
        put_cluster(NESTED_FILE_CLUSTER, nested_data)

    # ---- 8. 写文件（可选前置偏移，模拟带分区表的磁盘镜像） ----------------------
    payload = bytes(volume)
    if offset:
        payload = b"\x00" * offset + payload
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)

    image_path = os.path.abspath(path)
    timestamp_raw = encode_timestamp(_STAMP)

    # ---- 9. 返回描述信息（测试直接使用，避免重复硬编码） -----------------------
    meta = {
        "path": image_path,
        "offset": offset,
        "image_size": len(payload),
        "volume_length_sectors": VOLUME_LENGTH_SEC,
        "volume_length_bytes": VOLUME_LENGTH_SEC * SECTOR,
        "boot_checksum": checksum,
        "volume_serial": VOLUME_SERIAL,
        "volume_label": VOLUME_LABEL,
        "timestamp": {
            "utc": _STAMP.isoformat(),
            "raw": timestamp_raw,
            "epoch": timestamp_epoch(_STAMP),
        },
        "info": {
            "bytes_per_sector": SECTOR,
            "sectors_per_cluster": SECTORS_PER_CLUSTER,
            "cluster_size": CLUSTER_SIZE,
            "fat_offset_bytes": FAT_OFFSET_SEC * SECTOR,
            "fat_length_bytes": FAT_LENGTH_SEC * SECTOR,
            "heap_offset_bytes": CLUSTER_HEAP_OFFSET_SEC * SECTOR,
            "cluster_count": CLUSTER_COUNT,
            "root_cluster": ROOT_CLUSTER,
            "volume_label": VOLUME_LABEL,
            "volume_serial": VOLUME_SERIAL,
            "fs_revision": FS_REVISION,
            "volume_length_sectors": VOLUME_LENGTH_SEC,
            "volume_length_bytes": VOLUME_LENGTH_SEC * SECTOR,
        },
        "normal_file": {
            "name": NORMAL_FILE_NAME,
            "data": normal_data,
            "size": len(normal_data),
            "clusters": list(NORMAL_FILE_CLUSTERS),
            "attributes": 0x20,
            "mtime_raw": timestamp_raw,
            "mtime_epoch": timestamp_epoch(_STAMP),
            "deleted": False,
        },
        "deleted_file": {
            "name": DELETED_FILE_NAME,
            "data": deleted_data,
            "size": len(deleted_data),
            "clusters": [DELETED_FILE_CLUSTER],
            "attributes": 0x20,
            "mtime_raw": timestamp_raw,
            "mtime_epoch": timestamp_epoch(_STAMP),
            "deleted": True,
        },
        "root_entries": [
            {"name": NORMAL_FILE_NAME, "deleted": False, "size": len(normal_data)},
            {"name": DELETED_FILE_NAME, "deleted": True, "size": len(deleted_data)},
        ],
        "bitmap_allocated_clusters": sorted(allocated),
    }
    if with_subdir:
        meta["subdir"] = {
            "name": SUBDIR_NAME,
            "cluster": SUBDIR_CLUSTER,
            "nested_file": {
                "name": NESTED_FILE_NAME,
                "data": nested_data,
                "size": len(nested_data),
                "clusters": [NESTED_FILE_CLUSTER],
                "mtime_epoch": timestamp_epoch(_STAMP),
            },
        }
    return meta


# --------------------------------------------------------------------------- 命令行
def main(argv=None) -> int:
    """命令行入口：生成默认（或带子目录）测试镜像并打印概要。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("用法: python tests/gen_exfat_image.py 输出镜像路径 [--offset 字节] [--subdir]")
        return 0 if args else 2

    output = args[0]
    offset = 0
    with_subdir = False
    index = 1
    while index < len(args):
        token = args[index]
        if token == "--offset" and index + 1 < len(args):
            offset = int(args[index + 1], 0)
            index += 2
        elif token == "--subdir":
            with_subdir = True
            index += 1
        else:
            print("未知参数: %s" % token)
            return 2

    meta = build_exfat_image(output, offset=offset, with_subdir=with_subdir)
    summary = {
        "image": meta["path"],
        "offset": meta["offset"],
        "image_size": meta["image_size"],
        "boot_checksum": "0x%08X" % meta["boot_checksum"],
        "info": meta["info"],
        "normal_file": {
            "name": meta["normal_file"]["name"],
            "size": meta["normal_file"]["size"],
            "clusters": meta["normal_file"]["clusters"],
        },
        "deleted_file": {
            "name": meta["deleted_file"]["name"],
            "size": meta["deleted_file"]["size"],
            "clusters": meta["deleted_file"]["clusters"],
        },
    }
    if with_subdir:
        summary["subdir"] = {
            "name": meta["subdir"]["name"],
            "nested_file": {
                "name": meta["subdir"]["nested_file"]["name"],
                "size": meta["subdir"]["nested_file"]["size"],
            },
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
