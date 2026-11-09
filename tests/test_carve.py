#!/usr/bin/env python3
"""tests/test_carve.py —— 合成镜像验证 fdlib.carve.carve_image() 的特征码雕刻。

测试内容
--------
1. 生成一个 6MB 的随机数据镜像，在指定偏移嵌入：
   - 1 个完整 DOCX（python-docx 生成，内部含 word/document.xml）；
   - 1 个「无中央目录」的残缺 DOCX（把完整 DOCX 的中央目录整段截掉）；
   - 1 个 PNG（手写 IHDR/IDAT/IEND，带随机像素）；
   - 1 个 PDF、1 个最小 PE（exe）；
   - 1 段 UTF-8 中文文本 + 1 段 GBK 中文文本（两侧用 0x00 做噪音边界）；
   并让 PNG / 中文文本尽量跨越 4MB 扫描块边界，验证流式分块 + 重叠扫描。
2. 调用 carve_image(...) 断言：
   - 两类 DOCX（完整重建 / 残缺重建）都能恢复，且用 python-docx 打开后正文一致；
   - PNG / PDF / EXE 逐字节一致，偏移与嵌入位置一致；
   - 文本碎片原样恢复（UTF-8、GBK 各一段）；
   - 不产生重复的 zip / docx；
   - scanned_bytes、by_type、files/texts 结构与 JSON 可序列化；
   - progress 回调被调用且最终进度为 100%。
3. 额外验证 start/length 子区间扫描只恢复范围内的文件。
4. 可选：如果设置了环境变量 FILEDOCTOR_REAL_DOCX 指向一个真实损坏 docx，把它嵌入小镜像验证「无 EOCD
   docx 抢救」路径（不存在则跳过，测试本身不依赖该外部文件）。

运行：``python tests/test_carve.py``，全部通过退出码 0。
"""

import binascii
import io
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fdlib.carve import carve_image  # noqa: E402

# --------------------------------------------------------------------------- 测试常量
IMG_SIZE = 6 * 1024 * 1024          # 6MB，超过 4MB 扫描块，强制多块 + 跨块
CHUNK = 4 * 1024 * 1024

PNG_OFF = 1_000_000
DOCX_OFF = 2_500_000
PDF_OFF = 3_200_000
GBK_OFF = 3_500_000
TEXT_OFF = CHUNK - 20               # 跨 4MB 块边界
BROKEN_DOCX_OFF = 4_300_000
PE_OFF = 5_000_000

DOCX_PARAGRAPH = "这是用于验证特征码雕刻恢复的 Word 文档正文，包含中文与 ASCII 混排 abc 123。"
BROKEN_DOCX_PARAGRAPH = "这是另一份「无中央目录」的残缺 DOCX 正文：用于验证无 EOCD 时按局部头抢救的路径。"
TEXT_CN = (
    "文件雕刻恢复测试：本段中文文本用于验证 carve_image() 的文本碎片雕刻功能，"
    "包含 UTF-8 中文、GBK 中文与 ASCII 混排 abcdefghijklmnopqrstuvwxyz 0123456789。\n"
) * 8
TEXT_BYTES = TEXT_CN.encode("utf-8")
GBK_TEXT = (
    "GBK 编码的中文文本碎片测试：文件雕刻恢复模块应当能够识别 GBK 双字节中文，"
    "并与 ASCII 混排成段。"
) * 6
GBK_BYTES = GBK_TEXT.encode("gbk")


# --------------------------------------------------------------------------- 素材生成
def make_png(width=64, height=64, seed=2026):
    """手写一个合法 PNG（RGB 8bit，随机像素），返回完整字节。"""
    rnd = random.Random(seed)
    raw = bytearray()
    for _y in range(height):
        raw.append(0)  # filter type 0
        raw += bytes(rnd.randrange(256) for _ in range(width * 3))
    idat = zlib.compress(bytes(raw), 6)

    def chunk(ctype, data):
        return (struct.pack(">I", len(data)) + ctype + data
                + struct.pack(">I", binascii.crc32(ctype + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def make_docx(paragraph=DOCX_PARAGRAPH, heading="FileDoctor 雕刻恢复测试文档"):
    """用 python-docx 生成一个完整 DOCX，返回字节。"""
    from docx import Document
    doc = Document()
    doc.add_heading(heading, level=1)
    doc.add_paragraph(paragraph)
    doc.add_paragraph("English paragraph for carving: the quick brown fox jumps over the lazy dog.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def local_data_end(docx_bytes):
    """顺序遍历 DOCX 的局部文件头，返回最后一个局部条目数据结束位置。"""
    pos = 0
    while docx_bytes[pos:pos + 4] == b"PK\x03\x04":
        (_sig, _ver, _flags, _method, _mt, _md, _crc,
         csize, _usize, nlen, elen) = struct.unpack_from(
            "<IHHHHHIIIHH", docx_bytes, pos)
        pos = pos + 30 + nlen + elen + csize
    return pos


def make_broken_docx(full_docx):
    """把完整 DOCX 的中央目录 + EOCD 整段截掉，制造「无 EOCD 的残缺 docx」。"""
    cut = local_data_end(full_docx)
    assert cut > 0 and cut <= len(full_docx), "无法定位 docx 局部条目结束位置"
    broken = full_docx[:cut]
    assert b"PK\x05\x06" not in broken, "截断后仍残留 EOCD"
    assert broken.startswith(b"PK\x03\x04"), "截断后应保留局部文件头"
    return broken


def make_pdf():
    """合成一个结构简单但完整的 PDF（内容用注释行撑到 1KB 以上）。"""
    body = b"%PDF-1.4\n"
    body += b"% " + b"FileDoctor synthetic PDF for carve test. " * 30 + b"\n"
    body += b"%%EOF\n"
    return body


def make_pe():
    """合成一个最小 PE 文件（1 个 section，按 section 表推算总长 0x600 字节）。"""
    e_lfanew = 0x80
    opt_size = 0xE0
    n_sections = 1
    size_of_headers = 0x200
    raw_ptr, raw_size = 0x200, 0x400
    total = raw_ptr + raw_size
    buf = bytearray(e_lfanew + 4 + 20 + opt_size + 40)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, e_lfanew)
    pe_off = e_lfanew
    buf[pe_off:pe_off + 4] = b"PE\x00\x00"
    # COFF header: machine, sections, timestamp, symtab ptr, nsyms, opt size, chars
    struct.pack_into("<HHIIIHH", buf, pe_off + 4,
                     0x14C, n_sections, 0, 0, 0, opt_size, 0x0102)
    opt_off = pe_off + 24
    struct.pack_into("<H", buf, opt_off, 0x10B)          # PE32 magic
    struct.pack_into("<I", buf, opt_off + 60, size_of_headers)  # SizeOfHeaders
    sec_off = opt_off + opt_size
    struct.pack_into("<8sIIIIIIHHI", buf, sec_off,
                     b".text\x00\x00\x00", 0x200, 0x1000, raw_size, raw_ptr,
                     0, 0, 0, 0, 0x60000020)
    buf += b"\x90" * (total - len(buf))
    assert len(buf) == total
    return bytes(buf)


def _put(buf, pos, data, guard=True):
    """把 data 写入镜像 buf 的 pos 处，并在其后写一个 0x00 分隔字节。"""
    end = pos + len(data)
    assert end + 2 <= len(buf), "嵌入位置越界：pos=%d len=%d" % (pos, len(data))
    buf[pos:end] = data
    if guard:
        buf[end] = 0
    return pos


def build_image():
    """组装测试镜像，返回 (image_bytes, placed_offsets, materials)。"""
    rnd = random.Random(20261109)
    img = bytearray(rnd.randbytes(IMG_SIZE))

    png = make_png()
    docx = make_docx()
    # 残缺 docx 用「另一份不同内容」的文档：更接近真实场景，也避免和完整 docx 重建字节相同
    broken_docx = make_broken_docx(make_docx(BROKEN_DOCX_PARAGRAPH, "FileDoctor 残缺文档测试"))
    pdf = make_pdf()
    pe = make_pe()

    placed = {}
    placed["png"] = _put(img, PNG_OFF, png)
    placed["docx"] = _put(img, DOCX_OFF, docx)
    placed["pdf"] = _put(img, PDF_OFF, pdf)
    # GBK 文本：前后都用 0x00 隔离，保证雕刻出的内容与原文逐字节一致
    img[GBK_OFF - 1] = 0
    placed["gbk_text"] = _put(img, GBK_OFF, GBK_BYTES)
    # UTF-8 中文文本：起点在 4MB 块边界前 20 字节，验证跨块文本段合并
    img[TEXT_OFF - 1] = 0
    placed["text"] = _put(img, TEXT_OFF, TEXT_BYTES)
    placed["broken_docx"] = _put(img, BROKEN_DOCX_OFF, broken_docx)
    placed["exe"] = _put(img, PE_OFF, pe)

    materials = {"png": png, "docx": docx, "broken_docx": broken_docx,
                 "pdf": pdf, "pe": pe}
    return bytes(img), placed, materials


# --------------------------------------------------------------------------- 断言工具
def _read(path):
    with open(path, "rb") as fp:
        return fp.read()


def _docx_text(path):
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def _find_file(files, file_type, offset=None):
    hits = [f for f in files if f["type"] == file_type]
    if offset is not None:
        hits = [f for f in hits if f["offset"] == offset]
    assert len(hits) == 1, "期望 1 个 %s（offset=%r），实际 %d 个" % (
        file_type, offset, len(hits))
    return hits[0]


# --------------------------------------------------------------------------- 主测试
def test_main(base):
    print("[1/3] 生成合成镜像（6MB，嵌入完整/残缺 DOCX、PNG、PDF、PE、中文文本）……")
    img, placed, materials = build_image()
    img_path = os.path.join(base, "synth.img")
    with open(img_path, "wb") as fp:
        fp.write(img)
    print("      镜像: %s (%d 字节)" % (img_path, len(img)))

    outdir = os.path.join(base, "carved")
    progress_calls = []

    def progress(scanned, total):
        progress_calls.append((scanned, total))

    print("[2/3] 调用 carve_image(types=docx/zip/png/pdf/exe, text=True)……")
    result = carve_image(img_path, outdir,
                         types=["docx", "zip", "png", "pdf", "exe"],
                         text=True, min_size=1024, max_files=50,
                         progress=progress)

    # ---- 结构 / JSON 可序列化
    for key in ("scanned_bytes", "files", "texts", "by_type", "errors"):
        assert key in result, "返回结果缺少字段 %r" % key
    json.dumps(result, ensure_ascii=False)  # 必须能 JSON 序列化

    assert result["scanned_bytes"] == len(img), \
        "scanned_bytes=%d，应为 %d" % (result["scanned_bytes"], len(img))

    files = result["files"]
    texts = result["texts"]
    by_type = result["by_type"]

    # ---- DOCX：完整重建 + 残缺重建，各 1 个，内容一致
    assert by_type.get("docx") == 2, "by_type=%r，期望 docx=2" % (by_type,)
    assert by_type.get("zip", 0) == 0, "DOCX 不应再重复输出为 zip：%r" % (by_type,)
    docx_complete = _find_file(files, "docx", placed["docx"])
    docx_broken = _find_file(files, "docx", placed["broken_docx"])
    assert "完整" in docx_complete["note"], "完整 docx 的 note 异常：%r" % (docx_complete["note"],)
    assert "残缺" in docx_broken["note"], "残缺 docx 的 note 异常：%r" % (docx_broken["note"],)
    for item, expected in ((docx_complete, DOCX_PARAGRAPH),
                           (docx_broken, BROKEN_DOCX_PARAGRAPH)):
        assert os.path.isfile(item["path"]), "输出文件不存在：%s" % item["path"]
        assert item["path"].endswith(".docx")
        text = _docx_text(item["path"])
        assert expected in text, "恢复的 docx 正文不一致：%r" % (text[:120],)
        assert item["size"] == os.path.getsize(item["path"])

    # ---- PNG：逐字节一致
    png_file = _find_file(files, "png", placed["png"])
    assert _read(png_file["path"]) == materials["png"], "PNG 内容不一致"
    assert png_file["size"] == len(materials["png"])

    # ---- PDF：逐字节一致
    pdf_file = _find_file(files, "pdf", placed["pdf"])
    assert _read(pdf_file["path"]) == materials["pdf"], "PDF 内容不一致"
    assert pdf_file["size"] == len(materials["pdf"])

    # ---- EXE：逐字节一致
    exe_file = _find_file(files, "exe", placed["exe"])
    assert _read(exe_file["path"]) == materials["pe"], "EXE 内容不一致"
    assert exe_file["size"] == len(materials["pe"])

    # ---- 文件总数 / by_type 一致
    assert len(files) == 5, "文件数=%d，期望 5：%r" % (
        len(files), [(f["type"], f["offset"]) for f in files])
    assert by_type == {"docx": 2, "png": 1, "pdf": 1, "exe": 1}, "by_type=%r" % (by_type,)
    for item in files:
        assert set(item) >= {"type", "path", "offset", "size", "note"}
        assert isinstance(item["offset"], int) and item["offset"] >= 0
        assert os.path.isfile(item["path"])

    # ---- 文本碎片：UTF-8 中文 + GBK 中文各恢复一段，且逐字节一致
    text_blobs = [(_read(t["path"])) for t in texts]
    assert len(texts) == 2, "文本碎片数=%d，期望 2（UTF-8 + GBK）" % len(texts)
    assert TEXT_BYTES in text_blobs, "未恢复出 UTF-8 中文文本碎片"
    assert GBK_BYTES in text_blobs, "未恢复出 GBK 中文文本碎片"
    text_offsets = sorted(t["offset"] for t in texts)
    assert text_offsets == sorted([placed["text"], placed["gbk_text"]]), \
        "文本碎片偏移=%r，期望 %r" % (text_offsets, [placed["text"], placed["gbk_text"]])
    for t in texts:
        assert os.path.isfile(t["path"]) and t["path"].endswith(".txt")
        assert t["size"] == os.path.getsize(t["path"])

    # ---- 进度回调
    assert progress_calls, "progress 回调未被调用"
    assert progress_calls[-1] == (len(img), len(img)), \
        "最终进度=%r，期望 (%d, %d)" % (progress_calls[-1], len(img), len(img))
    monotonic = all(progress_calls[i][0] <= progress_calls[i + 1][0]
                    for i in range(len(progress_calls) - 1))
    assert monotonic, "进度回调不是单调递增：%r" % (progress_calls,)

    print("      文件: %s" % [(f["type"], f["offset"], f["size"]) for f in files])
    print("      文本: %s" % [(t["offset"], t["size"]) for t in texts])
    print("      by_type=%r  progress_calls=%d" % (by_type, len(progress_calls)))
    if result["errors"]:
        print("      errors: %r" % (result["errors"],))

    # ---- start/length 子区间：只扫描前 2MB，应只恢复 1MB 处的 PNG
    print("[3/3] 验证 start/length 子区间扫描……")
    out2 = os.path.join(base, "carved_range")
    range_len = 2 * 1024 * 1024
    result2 = carve_image(img_path, out2, types=["png"], start=0, length=range_len,
                          min_size=1024, max_files=10)
    assert result2["scanned_bytes"] == range_len, \
        "子区间 scanned_bytes=%d，期望 %d" % (result2["scanned_bytes"], range_len)
    assert result2["by_type"].get("png") == 1, "子区间应恢复 1 个 PNG：%r" % (result2["by_type"],)
    assert len(result2["files"]) == 1
    assert result2["files"][0]["offset"] == placed["png"]
    assert result2["texts"] == [], "未开 text 时 texts 应为空"
    assert _read(result2["files"][0]["path"]) == materials["png"]

    # ---- 可选：真实损坏样本（设置 FILEDOCTOR_REAL_DOCX 环境变量才跑；测试本身不依赖它）
    sample = os.environ.get("FILEDOCTOR_REAL_DOCX", "")
    if os.path.isfile(sample):
        print("      [可选] 发现真实损坏样本 %s，验证无 EOCD docx 抢救路径……" % sample)
        with open(sample, "rb") as fp:
            sample_bytes = fp.read()
        rnd = random.Random(7)
        img3 = bytearray(rnd.randbytes(512 * 1024))
        off = 200_000
        assert off + len(sample_bytes) + 2 <= len(img3)
        _put(img3, off, sample_bytes)
        img3[off - 1] = 0
        img3_path = os.path.join(base, "synth_sample.img")
        with open(img3_path, "wb") as fp:
            fp.write(bytes(img3))
        out3 = os.path.join(base, "carved_sample")
        result3 = carve_image(img3_path, out3, types=["docx"], min_size=256, max_files=10)
        assert result3["by_type"].get("docx") == 1, \
            "真实样本未恢复出 docx：%r" % (result3.get("by_type"),)
        rec = result3["files"][0]
        with zipfile.ZipFile(rec["path"]) as zf:
            names = zf.namelist()
        assert "word/document.xml" in names, "抢救出的 docx 缺少 word/document.xml：%r" % (names[:5],)
        print("      真实样本恢复: %s (%d 字节, note=%r)" % (
            rec["path"], rec["size"], rec["note"]))
    else:
        print("      [可选] 未发现 %s，跳过真实损坏样本验证（不影响自包含测试）" % sample)


def main():
    base = tempfile.mkdtemp(prefix="fd_carve_test_")
    ok = False
    try:
        test_main(base)
        ok = True
    finally:
        if ok:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print("\n测试失败：临时目录保留以便排查 -> %s" % base)
    print("=" * 66)
    print("test_carve.py PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
