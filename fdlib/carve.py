"""特征码雕刻恢复（carve）。

从普通镜像文件或 Windows 原始设备（``\\\\.\\G:``、``\\\\.\\PhysicalDrive3``）中
按文件签名流式扫描，把内嵌的、文件系统已不可见的文件「雕」出来。

支持的恢复类型
--------------
- ``zip``  ：``PK\\x03\\x04`` 局部头 + ``PK\\x05\\x06`` EOCD。
  优先用 EOCD 里的 ``cd_size`` / ``cd_offset`` 反推压缩包起点
  （``zip_start = eocd_pos - cd_size - cd_offset``），交给
  :func:`fdlib.ziprepair.extract_zip_bytes` 校验并解包，再用
  :func:`fdlib.ziprepair.rebuild_zip` 重建为合法 ZIP；
  找不到 EOCD（被截断/覆盖）时，把局部头之后的连续数据交给
  :func:`fdlib.ziprepair.salvage_zip_bytes` 抢救 CRC 校验通过的条目。
- ``docx`` ：zip 内部含 ``word/document.xml`` 或 ``[Content_Types].xml`` 时按 DOCX 输出。
- ``png``  ：``\\x89PNG\\r\\n\\x1a\\n`` 起，逐 chunk 解析到 ``IEND``，必须结构完整；
- ``jpg``  ：``FF D8 FF`` 起，按 marker 段走到 ``SOS``，再到 ``FF D9``，最大 64MB；
- ``pdf``  ：``%PDF-`` 起，向后找 ``%%EOF``（最多 256MB）；
- ``exe``  ：``MZ`` + ``PE\\0\\0``，按 section 表计算文件长度；
- 文本碎片（``text=True``）：连续可打印文本（ASCII + UTF-8 中文 + GBK 中文混排），
  单段 >= 256 字节输出为 ``.txt``；遇到二进制噪音切段。

调用接口
--------
``carve_image(path, outdir, types=None, text=False, start=0, length=None,
min_size=1024, max_files=500, progress=None) -> dict``

返回::

    {
      "scanned_bytes": 123456789,
      "files": [{"type": "docx", "path": "...", "offset": ..., "size": ..., "note": "..."}],
      "texts": [{"path": "...", "offset": ..., "size": ...}],
      "by_type": {"docx": 1, "png": 2},
      "errors": ["..."],
    }

实现要点
--------
1. **流式**：扫描阶段用 :meth:`DeviceReader.iter_chunks` 按 4MB 分块、相邻块重叠
   ``SCAN_OVERLAP`` 字节（>= 最长签名 8 字节），因此跨块的签名不会漏；
   文件内容按上限用 ``read_at`` 分块读取，不会把整个镜像读进内存。
2. **去重**：完整恢复出来的区域记为“已认领区间”，落在其中的局部头 / 内嵌文件不再重复输出；
   另外对输出内容做指纹去重（同一段数据只写一次）。
3. **上限保护**：单文件体积、候选数量、max_files 均有上限，损坏镜像里的海量误报不会撑爆内存。
4. **进度回调**：每扫描约 256MB 调一次 ``progress(scanned_bytes, total_bytes)``，结束时再调一次。

限制：ZIP64（cd_size/cd_offset 为 0xFFFFFFFF）暂不支持；JPEG 只做到“基本合理性校验”，
progressive JPEG 多扫描段里的 marker 载荷理论上可能包含 ``FF D9`` 造成提前截断。
"""

from __future__ import annotations

import bisect
import hashlib
import os
import re
import struct

from .device import DeviceReader

# --------------------------------------------------------------------------- 常量
DEFAULT_TYPES = ("docx", "zip", "png", "jpg", "pdf", "exe")
_SUPPORTED_TYPES = frozenset(DEFAULT_TYPES)
_TYPE_ALIASES = {"jpeg": "jpg", "jpe": "jpg"}

SIG_PNG = b"\x89PNG\r\n\x1a\n"
SIG_ZIP_LOCAL = b"PK\x03\x04"
SIG_ZIP_CENTRAL = b"PK\x01\x02"
SIG_ZIP_EOCD = b"PK\x05\x06"
SIG_JPG = b"\xff\xd8\xff"
SIG_PDF = b"%PDF-"
SIG_MZ = b"MZ"
PE_SIG = b"PE\x00\x00"
PDF_EOF = b"%%EOF"

SCAN_CHUNK = 4 * 1024 * 1024          # 扫描块大小
SCAN_OVERLAP = 8                      # 相邻块重叠（>= 最长签名 8 字节）
PROGRESS_STEP = 256 * 1024 * 1024     # 每扫描约 256MB 回调一次进度
CANDIDATE_CAP = 200000                # 单类签名候选上限（防损坏镜像海量误报）

MAX_ZIP_BYTES = 256 * 1024 * 1024     # 完整 ZIP 单包上限
MAX_SALVAGE_BYTES = 64 * 1024 * 1024  # 残缺 ZIP 抢救时最多读入的数据量
MAX_PNG_BYTES = 128 * 1024 * 1024
MAX_JPG_BYTES = 64 * 1024 * 1024
MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_EXE_BYTES = 128 * 1024 * 1024
MAX_PE_SECTIONS = 96

EOCD_MIN = 22                         # 不含注释的 EOCD 固定长度

TEXT_MIN_BYTES = 256                  # 文本碎片最短输出长度
TEXT_KEEP_MIN = 32                    # 跨块合并时保留的短段阈值
TEXT_MAX_RUN = 512 * 1024 * 1024      # 单段文本上限（超出截断）
TEXT_WRITE_CHUNK = 1024 * 1024        # 文本输出分块

# 文本 token：ASCII 可打印（含 tab/换行）| UTF-8 2/3/4 字节 | GBK 双字节。
# 只用于“找连续文本段”，输出时原样写回字节，不做编码转换。
_TEXT_RUN_RE = re.compile(
    rb"(?:[\t\n\r\x20-\x7e]"
    rb"|[\xc2-\xdf][\x80-\xbf]"
    rb"|[\xe0-\xef][\x80-\xbf]{2}"
    rb"|[\xf0-\xf4][\x80-\xbf]{3}"
    rb"|[\x81-\xfe][\x40-\x7e\x80-\xfe])+"
)

# JPEG 里允许出现的 SOF 标记（C4/C8/CC 不是 SOF）
_JPG_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


# --------------------------------------------------------------------------- 小工具
def _next_off(off: int, dlen: int, end: int) -> int:
    """返回与 DeviceReader.iter_chunks 一致的下一个块的绝对偏移。

    本块数据覆盖 ``[off, off+dlen)``，但其中最后 ``SCAN_OVERLAP`` 字节属于
    “重叠区”，交给下一块处理，避免候选重复。
    """
    if off + dlen >= end:
        return off + dlen
    step = max(1, dlen - min(SCAN_OVERLAP, dlen - 1))
    return off + step


def _unique_path(outdir: str, prefix: str, ext: str) -> str:
    """生成不重名的输出路径（``carved_0001.zip`` 这样的编号，已存在则继续递增）。"""
    i = 1
    while True:
        path = os.path.join(outdir, "%s_%04d%s" % (prefix, i, ext))
        if not os.path.exists(path):
            return path
        i += 1


def _norm_type(value) -> str:
    return str(value).strip().lower().lstrip(".")


def _normalize_types(types, errors) -> list:
    """把用户传入的 types 规范成默认顺序的类型列表；未知类型记入 errors。"""
    if types is None:
        return list(DEFAULT_TYPES)
    wanted = []
    for item in types:
        if not isinstance(item, str):
            errors.append("types 中的非字符串项已忽略：%r" % (item,))
            continue
        key = _norm_type(item)
        key = _TYPE_ALIASES.get(key, key)
        if key not in _SUPPORTED_TYPES:
            errors.append("不支持的类型 %r，已忽略（支持：%s）" % (item, ",".join(DEFAULT_TYPES)))
            continue
        if key not in wanted:
            wanted.append(key)
    return wanted


def _entries_fingerprint(entries) -> str:
    """计算条目集合的指纹，用于“同一段数据只输出一次”。"""
    h = hashlib.sha1()
    for item in entries or ():
        if isinstance(item, dict):
            name, data = item.get("name"), item.get("data")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            name, data = item[0], item[1]
        else:
            continue
        h.update(str(name).encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(hashlib.sha1(bytes(data or b"")).digest())
    return h.hexdigest()


def _entry_names(entries) -> set:
    """归一化条目名集合（小写、斜杠化），用于判断 docx。"""
    names = set()
    for item in entries or ():
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, (tuple, list)) and item:
            name = item[0]
        else:
            continue
        if not name:
            continue
        names.add(str(name).replace("\\", "/").strip("/").lower())
    return names


def _looks_docx(entries, hint) -> bool:
    if hint:
        return True
    names = _entry_names(entries)
    return "word/document.xml" in names or "[content_types].xml" in names


# --------------------------------------------------------------------------- 已认领区间
class _Claimed:
    """有序、自动合并的“已输出数据区间”集合，用于去重。"""

    __slots__ = ("_starts", "_ends")

    def __init__(self):
        self._starts = []
        self._ends = []

    def overlaps(self, start: int, end: int) -> bool:
        if end <= start:
            return False
        i = bisect.bisect_right(self._starts, start)
        if i > 0 and self._ends[i - 1] > start:
            return True
        if i < len(self._starts) and self._starts[i] < end:
            return True
        return False

    def next_start(self, pos: int):
        """返回严格大于 *pos* 的下一个区间起点；没有返回 None。"""
        i = bisect.bisect_right(self._starts, pos)
        return self._starts[i] if i < len(self._starts) else None

    def add(self, start: int, end: int) -> None:
        if end <= start:
            return
        starts, ends = self._starts, self._ends
        i = bisect.bisect_left(starts, start)
        if i > 0 and ends[i - 1] >= start:
            i -= 1
            start = min(start, starts[i])
            end = max(end, ends[i])
        j = i
        while j < len(starts) and starts[j] <= end:
            end = max(end, ends[j])
            j += 1
        starts[i:j] = [start]
        ends[i:j] = [end]

    def __len__(self) -> int:
        return len(self._starts)


# --------------------------------------------------------------------------- 扫描
def _scan_stream(reader, start: int, end: int, want_text: bool, progress, errors):
    """流式扫描一遍数据，收集各类签名候选位置与文本碎片区间。

    返回 ``(candidates, text_runs, scanned_bytes)``：

    - ``candidates``：``{"zip_local": [...], "zip_eocd": [...], "png": [...], ...}``；
    - ``text_runs``：``[[start, end), ...]``，已按偏移排序；
    - ``scanned_bytes``：实际扫描的字节数。
    """
    candidates = {
        "zip_local": [],
        "zip_eocd": [],
        "png": [],
        "jpg": [],
        "pdf": [],
        "mz": [],
    }
    signatures = (
        ("zip_local", SIG_ZIP_LOCAL),
        ("zip_eocd", SIG_ZIP_EOCD),
        ("png", SIG_PNG),
        ("jpg", SIG_JPG),
        ("pdf", SIG_PDF),
        ("mz", SIG_MZ),
    )
    text_runs = []
    pending = None  # 上一块末尾可能还会延续的文本段 [s, e]
    scanned = 0
    last_progress = 0
    cap_noted = False
    total = end - start

    for off, data in reader.iter_chunks(start, total, overlap=SCAN_OVERLAP):
        dlen = len(data)
        next_off = _next_off(off, dlen, end)

        # ---- 1) 签名扫描（重叠区里的候选留给下一块，保证不重复）
        for key, sig in signatures:
            bucket = candidates[key]
            if len(bucket) >= CANDIDATE_CAP:
                if not cap_noted:
                    errors.append("签名候选超过上限 %d，后续同类候选已忽略" % CANDIDATE_CAP)
                    cap_noted = True
                continue
            pos = data.find(sig)
            while pos >= 0:
                abs_pos = off + pos
                if abs_pos < next_off:
                    bucket.append(abs_pos)
                    if len(bucket) >= CANDIDATE_CAP:
                        break
                pos = data.find(sig, pos + 1)

        # ---- 2) 文本碎片扫描：用正则一次找出所有连续文本段，跨块时合并
        if want_text:
            for match in _TEXT_RUN_RE.finditer(data):
                seg_start = off + match.start()
                seg_end = off + match.end()
                if seg_start >= next_off:
                    # 起点落在重叠区，交给下一块（否则会重复统计）
                    continue
                if pending is not None:
                    if seg_start <= pending[1]:
                        # 与上一块末尾的段连续：合并
                        if seg_end > pending[1]:
                            pending[1] = seg_end
                        continue
                    if pending[1] - pending[0] >= TEXT_KEEP_MIN:
                        text_runs.append([pending[0], pending[1]])
                    pending = None
                pending = [seg_start, seg_end]

        scanned = min(off + dlen, end) - start
        if total and scanned - last_progress >= PROGRESS_STEP:
            last_progress = scanned
            if progress is not None:
                progress(scanned, total)

    if pending is not None and pending[1] - pending[0] >= TEXT_KEEP_MIN:
        text_runs.append([pending[0], pending[1]])
    if progress is not None:
        progress(scanned, total)

    return candidates, text_runs, scanned


# --------------------------------------------------------------------------- 格式探测
def _probe_png(reader, start: int, limit: int):
    """解析 PNG chunk 到 IEND，返回完整文件长度；不完整/不合法返回 None。"""
    if reader.read_at(start, 8) != SIG_PNG:
        return None
    pos = start + 8
    first = True
    seen_idat = False
    chunks = 0
    while True:
        chunks += 1
        if chunks > 200000:
            return None
        if pos + 8 > limit:
            return None
        head = reader.read_at(pos, 8)
        if len(head) < 8:
            return None
        length = struct.unpack(">I", head[:4])[0]
        ctype = head[4:8]
        # chunk 类型必须是 4 个 ASCII 字母
        if not all(65 <= c <= 90 or 97 <= c <= 122 for c in ctype):
            return None
        if length > MAX_PNG_BYTES:
            return None
        chunk_end = pos + 12 + length  # 4 长度 + 4 类型 + 数据 + 4 CRC
        if chunk_end > limit or chunk_end - start > MAX_PNG_BYTES:
            return None
        if first:
            if ctype != b"IHDR" or length != 13:
                return None
            ihdr = reader.read_at(pos + 8, 13)
            if len(ihdr) < 13:
                return None
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", ihdr)
            if width <= 0 or height <= 0 or width > 1000000 or height > 1000000:
                return None
            if bit_depth not in (1, 2, 4, 8, 16):
                return None
            if color_type not in (0, 2, 3, 4, 6):
                return None
            if comp != 0 or filt != 0 or interlace not in (0, 1):
                return None
            first = False
        elif ctype == b"IDAT":
            seen_idat = True
        elif ctype == b"IEND":
            if length != 0 or not seen_idat:
                return None
            return chunk_end - start
        pos = chunk_end


def _find_jpg_eoi(reader, pos: int, max_end: int):
    """在 JPEG 熵编码数据里找 ``FF D9``（数据里的 FF 会被转义成 FF00，故可直接找）。"""
    while pos < max_end:
        block = reader.read_at(pos, min(1 << 20, max_end - pos))
        if not block:
            return None
        idx = block.find(b"\xff\xd9")
        if idx >= 0:
            return pos + idx + 2
        pos += max(1, len(block) - 1)  # 留 1 字节，避免 FF D9 跨窗口
    return None


def _probe_jpg(reader, start: int, limit: int):
    """按 JPEG marker 解析，走到 SOS 后再找 EOI，返回完整长度；不合法返回 None。"""
    if reader.read_at(start, 3) != SIG_JPG:
        return None
    max_end = min(limit, start + MAX_JPG_BYTES)
    pos = start + 2  # 指向第二个字节 0xFF
    saw_sof = False
    while pos + 2 <= max_end:
        head = reader.read_at(pos, 2)
        if len(head) < 2 or head[0] != 0xFF:
            return None
        # 允许 marker 前有连续的 0xFF 填充字节
        while head[0] == 0xFF and head[1] == 0xFF:
            pos += 1
            head = reader.read_at(pos, 2)
            if len(head) < 2:
                return None
        marker = head[1]
        if marker == 0xD9:  # EOI
            return (pos + 2 - start) if saw_sof else None
        if marker == 0xD8:  # 嵌套 SOI，不合理
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # 无长度字段的独立标记
            pos += 2
            continue
        seg_len_bytes = reader.read_at(pos + 2, 2)
        if len(seg_len_bytes) < 2:
            return None
        seg_len = struct.unpack(">H", seg_len_bytes)[0]
        if seg_len < 2:
            return None
        if pos + 2 + seg_len > max_end:
            return None
        if marker in _JPG_SOF_MARKERS:
            if seg_len < 8:  # SOF 至少要有 精度+高+宽+分量数
                return None
            saw_sof = True
        if marker == 0xDA:  # SOS：后续是熵编码数据，直接找 EOI
            eoi = _find_jpg_eoi(reader, pos + 2 + seg_len, max_end)
            if eoi is None:
                return None
            return eoi - start
        pos += 2 + seg_len
    return None


def _probe_pdf(reader, start: int, limit: int):
    """从 ``%PDF-`` 起向后找 ``%%EOF``，返回包含尾随空白的长度；找不到返回 None。"""
    head = reader.read_at(start, 8)
    if len(head) < 8 or not head.startswith(SIG_PDF):
        return None
    version = head[5:8]
    if not (version[0:1].isdigit() and version[1:2] == b"." and version[2:3].isdigit()):
        return None
    max_end = min(limit, start + MAX_PDF_BYTES)
    pos = start + 8
    while pos < max_end:
        block = reader.read_at(pos, min(1 << 20, max_end - pos))
        if not block:
            return None
        idx = block.find(PDF_EOF)
        if idx >= 0:
            end = pos + idx + len(PDF_EOF)
            # 把 EOF 之后的空白（换行等）也算进文件
            while end < max_end:
                tail = reader.read_at(end, 1)
                if tail in (b"\n", b"\r", b"\t", b" "):
                    end += 1
                else:
                    break
            return end - start
        pos += max(1, len(block) - (len(PDF_EOF) - 1))
    return None


def _probe_exe(reader, start: int, limit: int):
    """MZ + PE 头 + section 表推算 PE 文件长度；不合法返回 None。"""
    mz = reader.read_at(start, 64)
    if len(mz) < 64 or mz[0:2] != SIG_MZ:
        return None
    e_lfanew = struct.unpack("<I", mz[0x3C:0x40])[0]
    if e_lfanew < 0x40 or e_lfanew > 0x100000:
        return None
    pe_off = start + e_lfanew
    if pe_off + 24 > limit:
        return None
    pe_head = reader.read_at(pe_off, 24)
    if len(pe_head) < 24 or pe_head[0:4] != PE_SIG:
        return None
    (machine, n_sections, _ts, _sym_ptr, _sym_num, opt_size,
     _chars) = struct.unpack("<HHIIIHH", pe_head[4:24])
    if n_sections <= 0 or n_sections > MAX_PE_SECTIONS:
        return None
    if opt_size < 96 or opt_size > 0x1000:
        return None
    opt_off = pe_off + 24
    if opt_off + opt_size > limit:
        return None
    opt = reader.read_at(opt_off, opt_size)
    if len(opt) < 64:
        return None
    magic = struct.unpack("<H", opt[0:2])[0]
    if magic not in (0x10B, 0x20B):  # PE32 / PE32+
        return None
    size_of_headers = struct.unpack("<I", opt[60:64])[0]
    sec_off = opt_off + opt_size
    if sec_off + 40 * n_sections > limit:
        return None
    sec_data = reader.read_at(sec_off, 40 * n_sections)
    if len(sec_data) < 40 * n_sections:
        return None
    total = size_of_headers
    for i in range(n_sections):
        base = i * 40
        raw_size, raw_ptr = struct.unpack("<II", sec_data[base + 16:base + 24])
        if raw_size == 0:
            continue
        if raw_ptr > (1 << 30) or raw_size > (1 << 30):
            return None
        total = max(total, raw_ptr + raw_size)
    if total < 2 or total > MAX_EXE_BYTES:
        return None
    if start + total > limit:
        return None
    return total


_PROBES = {
    "png": (_probe_png, SIG_PNG, ".png", "PNG 完整（解析到 IEND）"),
    "jpg": (_probe_jpg, SIG_JPG, ".jpg", "JPEG 完整（找到 EOI）"),
    "pdf": (_probe_pdf, SIG_PDF, ".pdf", "PDF 完整（找到 %%EOF）"),
    "exe": (_probe_exe, SIG_MZ, ".exe", "PE 完整（按 section 表推算长度）"),
}
_CAND_KEYS = {"png": "png", "jpg": "jpg", "pdf": "pdf", "exe": "mz"}


# --------------------------------------------------------------------------- 恢复上下文
class _Ctx:
    """一次 carve_image 调用的恢复上下文。"""

    def __init__(self, reader, outdir, scan_start, end, wanted, min_size, max_files,
                 errors, ziprepair):
        self.reader = reader
        self.outdir = outdir
        self.scan_start = scan_start
        self.end = end
        self.wanted = wanted
        self.min_size = min_size
        self.max_files = max_files
        self.errors = errors
        self.ziprepair = ziprepair
        self.files = []
        self.texts = []
        self.by_type = {}
        self.claimed = _Claimed()
        self.seen = set()          # 输出内容指纹，规避同一段数据的重复
        self.limit_noted = False

    # -------------------------------------------------------------- 上限/写文件
    def can_add_file(self) -> bool:
        if len(self.files) >= self.max_files:
            if not self.limit_noted:
                self.errors.append("已达到 max_files=%d 上限，停止继续雕刻文件" % self.max_files)
                self.limit_noted = True
            return False
        return True

    def save_bytes(self, file_type: str, ext: str, data: bytes, offset: int, note: str):
        """写一个雕刻出来的文件（内容已在内存里）；成功返回路径，失败/太短返回 None。"""
        if not self.can_add_file():
            return None
        if len(data) < self.min_size:
            return None
        digest = hashlib.sha1(data).hexdigest()
        key = (file_type, len(data), digest)
        if key in self.seen:
            return None
        path = _unique_path(self.outdir, "carved", ext)
        try:
            with open(path, "wb") as fp:
                fp.write(data)
        except OSError as exc:
            self.errors.append("写入 %s 失败：%s" % (path, exc))
            return None
        self.seen.add(key)
        self.files.append({"type": file_type, "path": path, "offset": offset,
                           "size": len(data), "note": note})
        self.by_type[file_type] = self.by_type.get(file_type, 0) + 1
        return path

    def save_zip_output(self, out_type: str, entries, offset: int, note: str,
                        claim_start: int, claim_end: int) -> bool:
        """把条目重建为 .docx/.zip 并记录认领区间；成功返回 True。

        优先用 ``ziprepair.rebuild_zip_bytes()`` 拿到字节串（便于内容去重和统计），
        若 ziprepair 版本较旧没有该函数，则退回 ``ziprepair.rebuild_zip()`` 直接写文件。
        """
        if not self.can_add_file():
            return False
        ext = ".docx" if out_type == "docx" else ".zip"
        rebuild_bytes = getattr(self.ziprepair, "rebuild_zip_bytes", None)
        if rebuild_bytes is not None:
            try:
                data = rebuild_bytes(entries)
            except Exception as exc:  # noqa: BLE001 - ziprepair 异常要报告不能吞
                self.errors.append("重建 %s 字节失败：%s" % (ext, exc))
                return False
            if not data or len(data) < self.min_size:
                return False
            key = (out_type, len(data), hashlib.sha1(data).hexdigest())
            if key in self.seen:
                return False
            path = _unique_path(self.outdir, "carved", ext)
            try:
                with open(path, "wb") as fp:
                    fp.write(data)
            except OSError as exc:
                self.errors.append("写入 %s 失败：%s" % (path, exc))
                _silent_remove(path)
                return False
            self.seen.add(key)
            size = len(data)
        else:  # 兼容旧版 ziprepair
            path = _unique_path(self.outdir, "carved", ext)
            try:
                self.ziprepair.rebuild_zip(entries, path)
            except Exception as exc:  # noqa: BLE001
                self.errors.append("重建 %s 失败：%s" % (ext, exc))
                _silent_remove(path)
                return False
            if not os.path.isfile(path):
                self.errors.append("重建 %s 后文件不存在：%s" % (ext, path))
                return False
            size = os.path.getsize(path)
            if size < self.min_size:
                _silent_remove(path)
                return False
            key = (out_type, size, _entries_fingerprint(entries))
            if key in self.seen:
                _silent_remove(path)
                return False
            self.seen.add(key)
        self.files.append({"type": out_type, "path": path, "offset": offset,
                           "size": size, "note": note})
        self.by_type[out_type] = self.by_type.get(out_type, 0) + 1
        self.claimed.add(claim_start, claim_end)
        return True


def _silent_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- ZIP / DOCX
def _read_local_header(data: bytes, off: int):
    """解析一个 ZIP 局部文件头，返回 ``(method, csize, data_offset, nlen, elen)``；失败 None。"""
    if off < 0 or off + 30 > len(data) or data[off:off + 4] != SIG_ZIP_LOCAL:
        return None
    try:
        (_sig, _ver, _flags, method, _mt, _md, _crc, csize, _usize,
         nlen, elen) = struct.unpack_from("<IHHHHHIIIHH", data, off)
    except struct.error:
        return None
    if nlen <= 0 or nlen > 4096 or elen > 65535:
        return None
    data_off = off + 30 + nlen + elen
    return method, csize, data_off, nlen, elen


def _local_entry_end(data: bytes, off: int):
    """粗略计算一个局部头条目的结束偏移（用于估计残缺 ZIP 的实际覆盖范围）。"""
    parsed = _read_local_header(data, off)
    if parsed is None:
        return None
    _method, csize, data_off, _nlen, _elen = parsed
    if csize and csize != 0xFFFFFFFF:
        end = data_off + csize
        return end if end <= len(data) else None
    # csize 未知（data descriptor）：找到下一个 ZIP 签名作为结束
    candidates = []
    for sig in (SIG_ZIP_LOCAL, SIG_ZIP_CENTRAL, SIG_ZIP_EOCD):
        p = data.find(sig, data_off + 1)
        if p > 0:
            candidates.append(p)
    return min(candidates) if candidates else None


def _salvage_extent(data: bytes, salv_entries) -> int:
    """根据抢救结果里的局部头偏移，估计这段残缺 ZIP 实际占用的字节数。"""
    best = 0
    for ent in salv_entries or ():
        if not isinstance(ent, dict):
            continue
        off = ent.get("offset")
        if not isinstance(off, int) or off < 0:
            continue
        end = _local_entry_end(data, off)
        if end is not None and end > best:
            best = end
    return best


def _carve_complete_zips(ctx: _Ctx, candidates, want_docx: bool, want_zip: bool) -> None:
    """处理所有 EOCD 候选：反推 zip 起点、校验中央目录、完整重建。"""
    reader = ctx.reader
    ziprepair = ctx.ziprepair
    for eocd_pos in sorted(candidates["zip_eocd"]):
        if len(ctx.files) >= ctx.max_files:
            break
        if eocd_pos < ctx.scan_start or eocd_pos + EOCD_MIN > ctx.end:
            continue
        if ctx.claimed.overlaps(eocd_pos, eocd_pos + 4):
            continue
        eocd = reader.read_at(eocd_pos, EOCD_MIN)
        if len(eocd) < EOCD_MIN or not eocd.startswith(SIG_ZIP_EOCD):
            continue
        try:
            (_sig, _disk, _cd_disk, _n_disk, _n_total, cd_size, cd_offset,
             comment_len) = struct.unpack("<IHHHHIIH", eocd[:EOCD_MIN])
        except struct.error:
            continue
        eocd_end = eocd_pos + EOCD_MIN + comment_len
        if eocd_end > ctx.end:
            continue
        if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            ctx.errors.append("偏移 %d 的 ZIP64 包暂不支持，已跳过" % eocd_pos)
            continue
        zip_start = eocd_pos - cd_size - cd_offset
        if zip_start < ctx.scan_start or zip_start < 0 or zip_start + 4 > ctx.end:
            continue
        head = reader.read_at(zip_start, 4)
        if head not in (SIG_ZIP_LOCAL, SIG_ZIP_EOCD):
            continue
        if ctx.claimed.overlaps(zip_start, eocd_end):
            continue
        total = eocd_end - zip_start
        if total > MAX_ZIP_BYTES:
            ctx.errors.append("偏移 %d 的 ZIP 体积 %d 超过上限 %d，已跳过"
                              % (zip_start, total, MAX_ZIP_BYTES))
            continue
        raw = reader.read_at(zip_start, total)
        if len(raw) != total:
            ctx.errors.append("偏移 %d 的 ZIP 读取不完整（%d/%d）" % (zip_start, len(raw), total))
            continue
        try:
            info = ziprepair.extract_zip_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            ctx.errors.append("偏移 %d: extract_zip_bytes 异常：%s" % (zip_start, exc))
            continue
        if not info:
            ctx.errors.append("偏移 %d: 找到 EOCD 但中央目录校验失败（数据可能已损坏）" % zip_start)
            continue
        entries = info.get("entries") or []
        if not entries:
            continue
        is_docx = _looks_docx(entries, info.get("is_docx"))
        if is_docx and want_docx:
            out_type, note = "docx", "EOCD+中央目录完整重建（DOCX）"
        elif want_zip:
            out_type, note = "zip", "EOCD+中央目录完整重建"
        else:
            # 已验证是完整 ZIP/DOCX，即使因类型过滤不输出也认领区域，避免内部文件被重复雕出
            ctx.claimed.add(zip_start, eocd_end)
            continue
        ctx.claimed.add(zip_start, eocd_end)
        ctx.save_zip_output(out_type, entries, zip_start, note, zip_start, eocd_end)


def _carve_salvaged_zips(ctx: _Ctx, candidates, want_docx: bool, want_zip: bool) -> None:
    """处理局部头候选：没有可用 EOCD 时，交给 salvage_zip_bytes 按 CRC 抢救。"""
    reader = ctx.reader
    ziprepair = ctx.ziprepair
    for pos in sorted(candidates["zip_local"]):
        if len(ctx.files) >= ctx.max_files:
            break
        if pos < ctx.scan_start or pos + 30 > ctx.end:
            continue
        if ctx.claimed.overlaps(pos, pos + 4):
            continue
        # 读取窗口：不越过已认领区间，防止把已输出的数据重复算进来
        read_len = min(MAX_SALVAGE_BYTES, ctx.end - pos)
        next_claim = ctx.claimed.next_start(pos + 1)
        if next_claim is not None and next_claim > pos:
            read_len = min(read_len, next_claim - pos)
        if read_len < 30:
            continue
        data = reader.read_at(pos, read_len)
        if len(data) < 30 or not data.startswith(SIG_ZIP_LOCAL):
            continue
        try:
            salv = ziprepair.salvage_zip_bytes(data)
        except Exception as exc:  # noqa: BLE001
            ctx.errors.append("偏移 %d: salvage_zip_bytes 异常：%s" % (pos, exc))
            continue
        salv_entries = salv.get("entries") or []
        entries = [(e.get("name"), e.get("data")) for e in salv_entries
                   if isinstance(e, dict) and e.get("data") is not None
                   and not str(e.get("name") or "").endswith("/")]
        if not entries:
            continue
        is_docx = _looks_docx(entries, salv.get("is_docx"))
        extent = _salvage_extent(data, salv_entries)
        claim_end = pos + min(len(data), max(extent, 4))
        if is_docx and want_docx:
            out_type, note = "docx", "残缺重建（无 EOCD，仅保留 CRC 校验通过的条目）"
        elif want_zip:
            out_type, note = "zip", "残缺重建（无 EOCD，仅保留 CRC 校验通过的条目）"
        else:
            out_type, note = None, None
        # 无论最终是否输出（内容去重/太小/类型过滤），都认领这段残缺 ZIP 的范围，
        # 否则它的后续局部头会被反复抢救，产生部分重复的文件。
        ctx.claimed.add(pos, claim_end)
        if out_type is not None:
            ctx.save_zip_output(out_type, entries, pos, note, pos, claim_end)


# --------------------------------------------------------------------------- PNG/JPG/PDF/EXE
def _carve_simple_types(ctx: _Ctx, candidates) -> None:
    """按格式结构解析 png/jpg/pdf/exe 并输出完整文件。"""
    for file_type in ("png", "jpg", "pdf", "exe"):
        if file_type not in ctx.wanted:
            continue
        probe, sig, ext, note = _PROBES[file_type]
        for pos in sorted(candidates[_CAND_KEYS[file_type]]):
            if len(ctx.files) >= ctx.max_files:
                return
            if pos < ctx.scan_start:
                continue
            if ctx.claimed.overlaps(pos, pos + len(sig)):
                continue
            size = probe(ctx.reader, pos, ctx.end)
            if size is None or size < ctx.min_size:
                continue
            if pos + size > ctx.end:
                continue
            data = ctx.reader.read_at(pos, size)
            if len(data) != size or not data.startswith(sig):
                continue
            if ctx.save_bytes(file_type, ext, data, pos, note):
                ctx.claimed.add(pos, pos + size)


# --------------------------------------------------------------------------- 文本碎片
def _carve_text_fragments(ctx: _Ctx, text_runs) -> None:
    """把扫描到的文本段写成 .txt（原样字节，不做转码）。"""
    for seg_start, seg_end in text_runs:
        if len(ctx.texts) >= ctx.max_files:
            ctx.errors.append("已达到 max_files=%d 上限，文本碎片雕刻提前结束" % ctx.max_files)
            break
        if seg_start < ctx.scan_start or seg_end > ctx.end:
            seg_start = max(seg_start, ctx.scan_start)
            seg_end = min(seg_end, ctx.end)
        if seg_end - seg_start < TEXT_MIN_BYTES:
            continue
        if ctx.claimed.overlaps(seg_start, seg_end):
            # 这段数据已经作为完整文件输出过，不再重复雕成文本碎片
            continue
        size = min(seg_end - seg_start, TEXT_MAX_RUN)
        path = _unique_path(ctx.outdir, "text", ".txt")
        written = 0
        try:
            with open(path, "wb") as fp:
                pos = seg_start
                remain = size
                while remain > 0:
                    block = ctx.reader.read_at(pos, min(TEXT_WRITE_CHUNK, remain))
                    if not block:
                        break
                    fp.write(block)
                    pos += len(block)
                    remain -= len(block)
                    written += len(block)
        except OSError as exc:
            ctx.errors.append("写入文本碎片 %s 失败：%s" % (path, exc))
            _silent_remove(path)
            continue
        if written < TEXT_MIN_BYTES:
            _silent_remove(path)
            continue
        note = None
        if size < seg_end - seg_start:
            note = "文本段超过 %d 字节，已截断" % TEXT_MAX_RUN
        item = {"path": path, "offset": seg_start, "size": written}
        if note:
            item["note"] = note
        ctx.texts.append(item)


# --------------------------------------------------------------------------- 主入口
def _load_ziprepair(errors):
    """延迟导入 ziprepair：模块缺失只影响 zip/docx，不影响其他类型。"""
    try:
        from . import ziprepair  # noqa: PLC0415 - 故意延迟导入
        return ziprepair
    except Exception as exc:  # noqa: BLE001
        errors.append("无法导入 fdlib.ziprepair（%s），zip/docx 雕刻不可用" % exc)
        return None


def carve_image(path: str, outdir: str,
                types=None, text: bool = False,
                start: int = 0, length=None,
                min_size: int = 1024, max_files: int = 500,
                progress=None) -> dict:
    """从 *path*（镜像文件或原始设备）中按特征码雕刻恢复文件。

    参数
    ----
    path      : 镜像文件路径或 Windows 设备路径（``\\\\.\\G:``）。
    outdir    : 输出目录（不存在会自动创建）。
    types     : 要恢复的类型列表；None 表示默认全部
                ``["docx", "zip", "png", "jpg", "pdf", "exe"]``；``jpg``/``jpeg`` 等价。
    text      : 是否额外做文本碎片雕刻（ASCII + UTF-8/GBK 中文）。
    start     : 扫描起始字节偏移。
    length    : 扫描长度；None 表示从 *start* 到设备末尾。
    min_size  : 小于该字节数的文件不输出（文本碎片固定按 256 字节）。
    max_files : 最多输出多少个文件（文本碎片也受此上限约束）。
    progress  : 进度回调 ``progress(scanned_bytes, total_bytes)``，可为 None。

    返回
    ----
    dict：``{"scanned_bytes", "files", "texts", "by_type", "errors", "types"}``，
    可直接 ``json.dumps``。
    """
    start = max(0, int(start or 0))
    min_size = max(0, int(min_size))
    max_files = max(1, int(max_files))
    errors = []

    if not os.path.isdir(outdir):
        os.makedirs(outdir, exist_ok=True)

    wanted = _normalize_types(types, errors)
    result = {"scanned_bytes": 0, "files": [], "texts": [], "by_type": {},
              "errors": errors, "types": wanted}

    reader = DeviceReader(path, chunk_size=SCAN_CHUNK)
    try:
        size = reader.size
        if size is None and length is None:
            errors.append("无法获知设备大小（裸设备），请显式传入 length")
            return result
        total = size if size is not None else start + int(length)
        end = total if length is None else min(total, start + int(length))
        if start >= end:
            errors.append("扫描范围为空（start=%d, end=%d）" % (start, end))
            return result

        ziprepair = _load_ziprepair(errors) if ("zip" in wanted or "docx" in wanted) else None

        # ---- 第一遍：流式扫描（只收集候选位置和文本段，不读大文件）
        candidates, text_runs, scanned = _scan_stream(
            reader, start, end, bool(text), progress, errors)
        result["scanned_bytes"] = scanned

        ctx = _Ctx(reader, outdir, start, end, wanted, min_size, max_files, errors, ziprepair)

        # ---- 第二遍：按候选位置解析/重建（ZIP 优先，避免内部文件重复雕刻）
        if ziprepair is not None and ("zip" in wanted or "docx" in wanted):
            want_docx = "docx" in wanted
            want_zip = "zip" in wanted
            _carve_complete_zips(ctx, candidates, want_docx, want_zip)
            _carve_salvaged_zips(ctx, candidates, want_docx, want_zip)
        if any(t in wanted for t in ("png", "jpg", "pdf", "exe")):
            _carve_simple_types(ctx, candidates)
        if text:
            _carve_text_fragments(ctx, text_runs)

        result["files"] = ctx.files
        result["texts"] = ctx.texts
        result["by_type"] = ctx.by_type
        return result
    finally:
        reader.close()


if __name__ == "__main__":  # 简单自测（不访问真实设备：只打印用法）
    print("carve_image(path, outdir, types=None, text=False, start=0, length=None,"
          " min_size=1024, max_files=500, progress=None)")
