# -*- coding: utf-8 -*-
"""tests/test_exfat.py —— fdlib/exfat.py 的解析与恢复测试。

测试思路
--------
用 ``tests/gen_exfat_image.py`` 现场生成一个最小合法 exFAT 镜像，然后验证：

1. 镜像本身符合规范（本次测试独立重算，不依赖生成器的函数）：
   * 引导区 BootChecksum（sector 11 的 4 份 u32，跳过 VolumeFlags/PercentInUse）；
   * 备份引导区与主引导区完全一致；
   * FAT 表项（cluster 0/1 保留值、链尾、正常文件 5->8 的链、删除簇被清 0）；
   * 分配位图（已分配/已释放位）；
   * 两个文件目录项集合的 SetChecksum，以及删除标记位；
   * 已删除文件的数据仍留在已释放的簇里（连续簇回退恢复的前提）。
2. ``open_volume()`` 解析出的 ``info`` 关键字段与生成器一致；
3. ``list_entries()`` 的数量、名字、删除标记、大小、时间戳、路径；
4. 正常文件内容逐字节一致，且确实是沿 FAT 链读到（两簇不连续，
   若按连续簇读会读到已删除文件在 cluster 6 的数据）；
5. 已删除文件用 read_entry_data / recover 都能恢复出正确内容，
   且 ``recovered_full=False``（FAT 链失效 → 连续簇回退）；
6. ``recover()`` 输出目录结构、重名加后缀、mtime 保留、JSON 可序列化；
7. ``open_volume(path, offset=...)`` 支持卷带分区偏移（offset != 0）；
8. 边界情况：错误的 offset / 非 exFAT 文件 / 目录没有数据流。

运行方式::

    python tests/test_exfat.py

退出码 0 表示全部通过，1 表示有失败（会打印失败项）。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
for _module_path in (PROJECT_ROOT, HERE):
    if _module_path not in sys.path:
        sys.path.insert(0, _module_path)

from fdlib.exfat import decode_timestamp, open_volume   # noqa: E402
import gen_exfat_image                                  # noqa: E402

CHECKS = []   # [(ok, 名称, 详情)]


# --------------------------------------------------------------------------- 断言框架
def check(name: str, condition, detail="") -> bool:
    """记录并打印一条断言结果。"""
    ok = bool(condition)
    CHECKS.append((ok, name, detail))
    print("[PASS] %s" % name if ok else "[FAIL] %s  %s" % (name, detail))
    return ok


def section(title: str) -> None:
    print("\n===== %s =====" % title)


# --------------------------------------------------------------------------- 独立校验工具
def rotate_add_checksum(data: bytes, skip=()) -> int:
    """按规范独立重算 32 位校验值（引导区 BootChecksum / Up-case 表用）。"""
    checksum = 0
    for index, byte in enumerate(data):
        if index in skip:
            continue
        checksum = (((checksum << 31) | (checksum >> 1)) & 0xFFFFFFFF) + byte
        checksum &= 0xFFFFFFFF
    return checksum


def entry_set_checksum16(data: bytes, skip=(2, 3)) -> int:
    """按规范独立重算目录项集合的 16 位 SetChecksum（跳过 @2..3）。"""
    checksum = 0
    for index, byte in enumerate(data):
        if index in skip:
            continue
        checksum = ((checksum >> 1) | ((checksum & 1) << 15)) & 0xFFFF
        checksum = (checksum + byte) & 0xFFFF
    return checksum


def read_image_span(image_path: str, offset: int, length: int) -> bytes:
    """从镜像文件读取一段数据。"""
    with open(image_path, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def read_cluster(image_path: str, meta: dict, cluster: int) -> bytes:
    """读取镜像里某个簇的原始数据。"""
    info = meta["info"]
    offset = info["heap_offset_bytes"] + (cluster - 2) * info["cluster_size"]
    return read_image_span(image_path, meta["offset"] + offset, info["cluster_size"])


def scan_file_entry_sets(raw: bytes):
    """独立扫描目录原始字节，返回 [(集合字节, 起始偏移, EntryType), ...]（只认 0x05 类型）。"""
    result = []
    position = 0
    while position + 32 <= len(raw):
        entry_type = raw[position]
        if entry_type == 0x00:
            break
        if (entry_type & 0x7F) == 0x05:
            secondary_count = raw[position + 1]
            set_size = (1 + secondary_count) * 32
            result.append((raw[position:position + set_size], position, entry_type))
            position += set_size
        else:
            position += 32
    return result


# --------------------------------------------------------------------------- 测试项
def test_image_structure(image_path: str, meta: dict) -> None:
    """独立检查生成的镜像是否符合 exFAT 规范（证明测试镜像是可信的）。"""
    section("镜像结构自检（独立重算校验值，不依赖解析器）")

    check("镜像文件大小 == VolumeLength",
          os.path.getsize(image_path) == meta["volume_length_bytes"],
          "%d != %d" % (os.path.getsize(image_path), meta["volume_length_bytes"]))

    # ---- 引导区 BootChecksum（sector 11，4 份 u32 重复） ----
    boot_region = read_image_span(image_path, meta["offset"], 12 * 512)
    expected = rotate_add_checksum(boot_region[:11 * 512], skip=(106, 107, 112))
    copies = struct.unpack_from("<IIII", boot_region, 11 * 512)
    check("BootChecksum 与规范算法一致", copies[0] == expected,
          "0x%08X != 0x%08X" % (copies[0], expected))
    check("BootChecksum 4 份 u32 重复", len(set(copies)) == 1, str(copies))
    check("备份引导区与主引导区一致",
          read_image_span(image_path, meta["offset"] + 12 * 512, 12 * 512) == boot_region)

    # ---- FAT ----
    info = meta["info"]
    fat_raw = read_image_span(image_path, meta["offset"] + info["fat_offset_bytes"],
                              info["fat_length_bytes"])
    fat = struct.unpack("<%dI" % (len(fat_raw) // 4), fat_raw)
    normal_first, normal_second = meta["normal_file"]["clusters"]
    check("FAT[0] == 0xFFFFFFF8（介质/保留）", fat[0] == 0xFFFFFFF8, hex(fat[0]))
    check("FAT[1] == 0xFFFFFFFF（保留）", fat[1] == 0xFFFFFFFF, hex(fat[1]))
    check("正常文件 FAT 链 %d -> %d" % (normal_first, normal_second),
          fat[normal_first] == normal_second, hex(fat[normal_first]))
    check("正常文件 FAT 链尾", fat[normal_second] >= 0xFFFFFFF8, hex(fat[normal_second]))
    deleted_cluster = meta["deleted_file"]["clusters"][0]
    check("删除文件的 FAT 表项已被清 0", fat[deleted_cluster] == 0, hex(fat[deleted_cluster]))

    # ---- 分配位图：bit0 对应 cluster 2 ----
    bitmap = read_cluster(image_path, meta, 2)
    allocated_bits = set()
    for cluster in meta["bitmap_allocated_clusters"]:
        allocated_bits.add(cluster - 2)
    expected_byte0 = 0
    for bit in allocated_bits:
        if bit < 8:
            expected_byte0 |= 1 << bit
    expected_byte1 = 0
    for bit in allocated_bits:
        if 8 <= bit < 16:
            expected_byte1 |= 1 << (bit - 8)
    check("分配位图字节 0 与簇分配一致", bitmap[0] == expected_byte0,
          "0x%02X != 0x%02X" % (bitmap[0], expected_byte0))
    check("分配位图字节 1 与簇分配一致", bitmap[1] == expected_byte1,
          "0x%02X != 0x%02X" % (bitmap[1], expected_byte1))
    check("已删除文件的簇在位图中已释放",
          not (bitmap[(deleted_cluster - 2) // 8] >> ((deleted_cluster - 2) % 8)) & 1)

    # ---- 根目录里的文件目录项集合：SetChecksum + 删除位 ----
    root_raw = read_cluster(image_path, meta, info["root_cluster"])
    entry_sets = scan_file_entry_sets(root_raw)
    check("根目录有 2 个文件目录项集合", len(entry_sets) == 2, "实际 %d 个" % len(entry_sets))
    for index, (raw_set, position, entry_type) in enumerate(entry_sets):
        stored = struct.unpack_from("<H", raw_set, 2)[0]
        computed = entry_set_checksum16(raw_set)
        check("目录项集合 #%d SetChecksum 正确" % index, stored == computed,
              "0x%04X != 0x%04X" % (stored, computed))
    if len(entry_sets) >= 2:
        check("正常文件 File Entry 类型为 0x85（在用位 0x80 置位）",
              entry_sets[0][2] == 0x85, hex(entry_sets[0][2]))
        check("已删除文件 File Entry 类型为 0x05（在用位被清零）",
              entry_sets[1][2] == 0x05, hex(entry_sets[1][2]))
        stream_offset = 32
        name_offset = 64
        check("已删除文件 Stream 项类型为 0x40",
              entry_sets[1][0][stream_offset] == 0x40,
              hex(entry_sets[1][0][stream_offset]))
        check("已删除文件 File Name 项类型为 0x41",
              entry_sets[1][0][name_offset] & 0x7F == 0x41,
              hex(entry_sets[1][0][name_offset]))

    # ---- 正常文件的两簇数据确实不连续存放 ----
    normal_data = meta["normal_file"]["data"]
    cluster_first, cluster_second = meta["normal_file"]["clusters"]
    check("正常文件第 1 簇内容一致",
          read_cluster(image_path, meta, cluster_first) == normal_data[:4096])
    check("正常文件第 2 簇内容一致",
          read_cluster(image_path, meta, cluster_second)[:len(normal_data) - 4096]
          == normal_data[4096:])
    check("若按连续簇读会读错（证明必须走 FAT 链）",
          normal_data[4096:] != read_cluster(image_path, meta, cluster_first + 1)[:len(normal_data) - 4096])

    # ---- 已删除文件的数据仍留在已释放的簇里 ----
    stale = read_cluster(image_path, meta, deleted_cluster)[:meta["deleted_file"]["size"]]
    check("已删除文件数据仍保留在原簇（恢复前提）",
          stale == meta["deleted_file"]["data"])


def test_info(volume, meta: dict, label: str) -> None:
    """验证 open_volume() 解析出的 info 关键字段。"""
    section("open_volume() / info 字段（%s）" % label)
    info = volume.info
    expected = meta["info"]
    for key, value in expected.items():
        check("info[%r] == %r" % (key, value), info.get(key) == value,
              "实际 %r" % (info.get(key),))
    check("info['fs_revision_str'] == '1.00'", info["fs_revision_str"] == "1.00",
          info["fs_revision_str"])
    check("info['volume_serial_hex'] == '0x12345678'",
          info["volume_serial_hex"] == "0x12345678", info["volume_serial_hex"])
    check("info['boot_signature_ok'] is True", info["boot_signature_ok"] is True)
    check("info['fat_length_bytes'] >= (cluster_count+2)*4",
          info["fat_length_bytes"] >= (info["cluster_count"] + 2) * 4)
    check("info['device_size'] == 镜像文件大小",
          info["device_size"] == os.path.getsize(meta["path"]), info["device_size"])
    check("info['active_fat'] == 0（单 FAT）", info["active_fat"] == 0)
    try:
        json.dumps(info, ensure_ascii=False)
        serializable = True
    except (TypeError, ValueError):
        serializable = False
    check("info 可 JSON 序列化", serializable)


def test_list_entries(volume, meta: dict) -> None:
    """验证 list_entries()：数量、名字、删除标记、大小、路径、时间戳。"""
    section("list_entries()")
    entries = volume.list_entries(include_deleted=True)
    names = [entry.name for entry in entries]
    check("包含 2 个条目", len(entries) == 2, "实际 %r" % names)
    check("名字与顺序正确",
          names == [meta["normal_file"]["name"], meta["deleted_file"]["name"]],
          repr(names))

    by_name = {entry.name: entry for entry in entries}
    normal = by_name.get(meta["normal_file"]["name"])
    deleted = by_name.get(meta["deleted_file"]["name"])
    check("正常文件条目存在", normal is not None)
    check("已删除文件条目存在", deleted is not None)

    if normal is not None:
        check("正常文件未标记删除", normal.deleted is False)
        check("正常文件不是目录", normal.is_dir is False)
        check("正常文件大小正确", normal.size == meta["normal_file"]["size"],
              "实际 %d" % normal.size)
        check("正常文件首簇正确", normal.first_cluster == meta["normal_file"]["clusters"][0],
              "实际 %d" % normal.first_cluster)
        check("正常文件属性含 Archive(0x20)", normal.attributes & 0x20)
        check("正常文件路径为 hello.txt", normal.path == meta["normal_file"]["name"],
              normal.path)
        check("正常文件 mtime 解码正确", normal.mtime == meta["normal_file"]["mtime_epoch"],
              "%r != %r" % (normal.mtime, meta["normal_file"]["mtime_epoch"]))
        check("正常文件 mtime_raw 与写入值一致",
              normal.mtime_raw == meta["normal_file"]["mtime_raw"])
        check("正常文件 ctime 不为 None", normal.ctime is not None)
        check("正常文件 ValidDataLength == DataLength",
              normal.valid_data_length == normal.size)
        check("正常文件未设置 NoFatChain（走 FAT 链）", normal.no_fat_chain is False)

    if deleted is not None:
        check("已删除文件标记为删除", deleted.deleted is True)
        check("已删除文件大小正确", deleted.size == meta["deleted_file"]["size"],
              "实际 %d" % deleted.size)
        check("已删除文件首簇正确",
              deleted.first_cluster == meta["deleted_file"]["clusters"][0],
              "实际 %d" % deleted.first_cluster)

    only_live = volume.list_entries(include_deleted=False)
    check("include_deleted=False 只返回 1 个条目", len(only_live) == 1,
          repr([entry.name for entry in only_live]))
    check("include_deleted=False 过滤掉删除项",
          all(not entry.deleted for entry in only_live))

    try:
        json.dumps([entry.to_dict() for entry in entries], ensure_ascii=False)
        serializable = True
    except (TypeError, ValueError):
        serializable = False
    check("Entry.to_dict() 可 JSON 序列化", serializable)


def test_read_normal(volume, meta: dict) -> None:
    """验证正常文件读取：逐字节一致、走 FAT 链、max_bytes、流式接口。"""
    section("正常文件读取（FAT 链）")
    normal = next(entry for entry in volume.list_entries()
                  if entry.name == meta["normal_file"]["name"])
    data, full = volume.read_entry_data(normal)
    check("正常文件内容逐字节一致", data == meta["normal_file"]["data"],
          "长度 %d vs %d" % (len(data), len(meta["normal_file"]["data"])))
    check("正常文件 recovered_full is True", full is True)

    partial, partial_full = volume.read_entry_data(normal, max_bytes=1000)
    check("max_bytes=1000 只读前 1000 字节",
          partial == meta["normal_file"]["data"][:1000], "长度 %d" % len(partial))
    check("max_bytes 截断仍标记完整", partial_full is True)

    streamed = b"".join(volume.iter_entry_chunks(normal))
    check("iter_entry_chunks 流式读取内容一致", streamed == data,
          "长度 %d vs %d" % (len(streamed), len(data)))

    cluster_size = volume.info["cluster_size"]
    chunks = list(volume.iter_entry_chunks(normal))
    check("流式读取按簇分块（不把整文件作为一个大块）",
          len(chunks) >= 2 and all(len(chunk) <= cluster_size for chunk in chunks),
          "块数 %d" % len(chunks))


def test_read_deleted(volume, meta: dict) -> None:
    """验证已删除文件读取：连续簇回退 + recovered_full=False。"""
    section("已删除文件读取（连续簇回退）")
    deleted = next(entry for entry in volume.list_entries()
                   if entry.name == meta["deleted_file"]["name"])
    data, full = volume.read_entry_data(deleted)
    check("已删除文件内容逐字节一致", data == meta["deleted_file"]["data"],
          "长度 %d vs %d" % (len(data), len(meta["deleted_file"]["data"])))
    check("已删除文件 recovered_full is False（连续簇回退，不完全可信）", full is False)
    check("已删除文件 ValidDataLength 正确",
          deleted.valid_data_length == meta["deleted_file"]["size"])

    partial, partial_full = volume.read_entry_data(deleted, max_bytes=100)
    check("已删除文件 max_bytes 截断正确", partial == meta["deleted_file"]["data"][:100])
    check("已删除文件 max_bytes 截断仍标记不可信", partial_full is False)


def test_recover(volume, meta: dict, tmpdir: str) -> None:
    """验证 recover()：目录结构、内容、recovered_full、重名后缀、mtime、JSON。"""
    section("recover()")
    outdir = os.path.join(tmpdir, "recovered_all")
    result = volume.recover(outdir, include_deleted=True)
    check("recover 返回 count == 2", result["count"] == 2, str(result["count"]))
    check("recover 无错误", result["errors"] == [], repr(result["errors"]))
    check("recover 返回 files 列表", isinstance(result["files"], list))
    try:
        json.dumps(result, ensure_ascii=False)
        serializable = True
    except (TypeError, ValueError):
        serializable = False
    check("recover 结果可 JSON 序列化", serializable)

    by_name = {item["name"]: item for item in result["files"]}
    normal_item = by_name.get(meta["normal_file"]["name"])
    deleted_item = by_name.get(meta["deleted_file"]["name"])
    check("恢复结果包含正常文件", normal_item is not None)
    check("恢复结果包含已删除文件", deleted_item is not None)

    if normal_item is not None:
        with open(normal_item["path"], "rb") as handle:
            content = handle.read()
        check("恢复出的正常文件内容逐字节一致", content == meta["normal_file"]["data"])
        check("恢复出的正常文件 recovered_full is True",
              normal_item["recovered_full"] is True)
        check("恢复出的正常文件 size 正确",
              normal_item["size"] == meta["normal_file"]["size"])
        check("恢复出的正常文件 mtime 已保留",
              abs(os.path.getmtime(normal_item["path"])
                  - meta["normal_file"]["mtime_epoch"]) <= 2,
              "mtime=%r" % os.path.getmtime(normal_item["path"]))
        check("恢复出的正常文件路径在 outdir 下",
              os.path.dirname(os.path.abspath(normal_item["path"]))
              == os.path.abspath(outdir))

    if deleted_item is not None:
        with open(deleted_item["path"], "rb") as handle:
            content = handle.read()
        check("恢复出的已删除文件内容逐字节一致", content == meta["deleted_file"]["data"])
        check("恢复出的已删除文件 recovered_full is False",
              deleted_item["recovered_full"] is False)
        check("恢复出的已删除文件带 note 说明",
              bool(deleted_item.get("note")), repr(deleted_item.get("note")))

    # 重名自动加后缀：再恢复一次到同一目录
    result_again = volume.recover(outdir, include_deleted=True)
    paths_again = sorted(item["path"] for item in result_again["files"])
    check("第二次恢复重名自动加 -1 后缀",
          any(path.endswith("-1.txt") for path in paths_again)
          and any(path.endswith("-1.bin") for path in paths_again),
          repr([os.path.basename(path) for path in paths_again]))
    check("第二次恢复文件数量仍为 2", result_again["count"] == 2)

    # include_deleted=False：只恢复正常文件
    outdir_live = os.path.join(tmpdir, "recovered_live")
    result_live = volume.recover(outdir_live, include_deleted=False)
    check("include_deleted=False 时 count == 1", result_live["count"] == 1,
          str(result_live["count"]))
    check("include_deleted=False 不写出删除文件",
          not os.path.exists(os.path.join(outdir_live, meta["deleted_file"]["name"])))
    live_path = os.path.join(outdir_live, meta["normal_file"]["name"])
    check("include_deleted=False 仍恢复出正常文件", os.path.exists(live_path))
    with open(live_path, "rb") as handle:
        check("仅恢复正常文件时内容仍正确", handle.read() == meta["normal_file"]["data"])


def test_with_subdir(tmpdir: str) -> None:
    """验证带子目录的镜像：递归遍历 + recover 保留目录结构 + offset != 0。"""
    section("子目录 + 分区偏移（offset != 0）")

    image_path = os.path.join(tmpdir, "offset_exfat.img")
    offset = 1024 * 1024           # 卷起始偏移 1MB，模拟带分区表的镜像
    meta = gen_exfat_image.build_exfat_image(image_path, offset=offset, with_subdir=True)
    check("带偏移镜像文件大小正确",
          os.path.getsize(image_path) == offset + meta["volume_length_bytes"],
          "%d != %d" % (os.path.getsize(image_path),
                        offset + meta["volume_length_bytes"]))

    # 不带 offset 打开应当失败
    try:
        open_volume(image_path).close()
        check("offset=0 打开带偏移镜像应抛 ValueError", False, "没有抛异常")
    except ValueError:
        check("offset=0 打开带偏移镜像抛 ValueError", True)

    volume = open_volume(image_path, offset=offset)
    try:
        check("offset 打开时 info 正确",
              volume.info["volume_label"] == meta["volume_label"]
              and volume.info["root_cluster"] == meta["info"]["root_cluster"]
              and volume.info["cluster_count"] == meta["info"]["cluster_count"])
        check("info['offset'] 记录卷起始偏移", volume.info["offset"] == offset)

        entries = volume.list_entries(include_deleted=True)
        paths = {entry.path for entry in entries}
        expected_paths = {
            meta["normal_file"]["name"],
            meta["deleted_file"]["name"],
            meta["subdir"]["name"],
            "%s/%s" % (meta["subdir"]["name"], meta["subdir"]["nested_file"]["name"]),
        }
        check("子目录镜像遍历出全部 4 个条目", len(entries) == 4,
              repr(sorted(paths)))
        check("子目录路径正确", expected_paths <= paths, repr(sorted(paths)))

        sub_entry = next(entry for entry in entries
                         if entry.path == meta["subdir"]["name"])
        nested_entry = next(entry for entry in entries
                            if entry.path.endswith(meta["subdir"]["nested_file"]["name"]))
        check("子目录项 is_dir is True", sub_entry.is_dir is True)
        check("子目录内文件 is_dir is False", nested_entry.is_dir is False)

        nested_data, nested_full = volume.read_entry_data(nested_entry)
        check("子目录内文件内容逐字节一致",
              nested_data == meta["subdir"]["nested_file"]["data"])
        check("子目录内文件 recovered_full is True", nested_full is True)

        try:
            volume.read_entry_data(sub_entry)
            check("读取目录数据应抛 ValueError", False, "没有抛异常")
        except ValueError:
            check("读取目录数据抛 ValueError", True)

        outdir = os.path.join(tmpdir, "recovered_subdir")
        result = volume.recover(outdir, include_deleted=True)
        # count 是恢复出的“文件”数（不含目录）：hello.txt + gone.bin + sub/nested.txt
        check("子目录镜像 recover count == 3（文件数，不含目录）", result["count"] == 3,
              str(result["count"]))
        check("子目录镜像 recover 无错误", result["errors"] == [],
              repr(result["errors"]))
        nested_out = os.path.join(outdir, meta["subdir"]["name"],
                                  meta["subdir"]["nested_file"]["name"])
        check("recover 保留目录结构（sub/nested.txt 存在）", os.path.exists(nested_out),
              nested_out)
        if os.path.exists(nested_out):
            with open(nested_out, "rb") as handle:
                check("子目录内文件恢复内容逐字节一致",
                      handle.read() == meta["subdir"]["nested_file"]["data"])
        by_name = {item["name"]: item for item in result["files"]}
        check("recover 结果中已删除文件仍标记 recovered_full=False",
              by_name[meta["deleted_file"]["name"]]["recovered_full"] is False)
        check("recover 结果中正常文件 recovered_full=True",
              by_name[meta["normal_file"]["name"]]["recovered_full"] is True)
    finally:
        volume.close()


def test_errors(tmpdir: str) -> None:
    """验证错误处理：非 exFAT 文件、错误 offset、不存在的文件。"""
    section("错误处理")
    try:
        open_volume(os.path.join(PROJECT_ROOT, "SPEC.md")).close()
        check("打开非 exFAT 文件应抛 ValueError", False, "没有抛异常")
    except ValueError as exc:
        check("打开非 exFAT 文件抛 ValueError", "exFAT" in str(exc), str(exc))

    image_path = os.path.join(tmpdir, "mini_for_errors.img")
    gen_exfat_image.build_exfat_image(image_path)
    try:
        open_volume(image_path, offset=1000).close()
        check("错误 offset 打开应抛 ValueError", False, "没有抛异常")
    except ValueError:
        check("错误 offset 打开抛 ValueError", True)

    try:
        open_volume(os.path.join(tmpdir, "不存在的镜像.img")).close()
        check("打开不存在的文件应抛 OSError", False, "没有抛异常")
    except OSError:
        check("打开不存在的文件抛 OSError", True)


def test_decode_timestamp() -> None:
    """验证时间戳位域解码（秒/2、分、时、日、月、年-1980）。"""
    section("时间戳解码")
    moment = __import__("datetime").datetime(2024, 5, 1, 12, 34, 56)
    raw = gen_exfat_image.encode_timestamp(moment)
    expected = int(moment.replace(
        tzinfo=__import__("datetime").timezone.utc).timestamp())
    check("decode_timestamp 解码 2024-05-01 12:34:56", decode_timestamp(raw) == expected,
          "%r != %r" % (decode_timestamp(raw), expected))
    check("decode_timestamp(0) 返回 None", decode_timestamp(0) is None)
    check("decode_timestamp 非法月份返回 None",
          decode_timestamp(((2024 - 1980) << 25) | (13 << 21) | (1 << 16)) is None)


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="fd_exfat_test_")
    print("临时目录：%s" % tmpdir)
    try:
        # ---- 最小镜像（1 个正常文件 + 1 个删除文件） ----
        image_path = os.path.join(tmpdir, "mini_exfat.img")
        meta = gen_exfat_image.build_exfat_image(image_path)
        test_image_structure(image_path, meta)

        volume = open_volume(image_path)
        try:
            test_info(volume, meta, "最小镜像")
            test_list_entries(volume, meta)
            test_read_normal(volume, meta)
            test_read_deleted(volume, meta)
            test_recover(volume, meta, tmpdir)
        finally:
            volume.close()

        # ---- 带子目录 + 分区偏移的镜像 ----
        test_with_subdir(tmpdir)

        # ---- 其他 ----
        test_errors(tmpdir)
        test_decode_timestamp()

        # ---- 关闭语义 ----
        section("关闭语义")
        volume = open_volume(image_path)
        info_before_close = dict(volume.info)
        volume.close()
        check("close() 后 info 仍可读", volume.info == info_before_close)
        try:
            volume.list_entries()
            check("close() 后读取应抛 ValueError", False, "没有抛异常")
        except ValueError:
            check("close() 后读取抛 ValueError", True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 汇总 ----
    failed = [name for ok, name, _detail in CHECKS if not ok]
    print("\n" + "=" * 60)
    print("断言总数：%d，通过：%d，失败：%d"
          % (len(CHECKS), len(CHECKS) - len(failed), len(failed)))
    if failed:
        print("失败项：")
        for name in failed:
            print("  - %s" % name)
        return 1
    print("全部通过：fdlib/exfat.py + tests/gen_exfat_image.py 工作正常")
    return 0


if __name__ == "__main__":
    try:
        EXIT_CODE = main()
    except Exception:                       # 未捕获异常也算测试失败
        traceback.print_exc()
        EXIT_CODE = 1
    sys.exit(EXIT_CODE)
