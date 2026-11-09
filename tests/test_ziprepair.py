"""ziprepair 模块测试：模拟“中央目录丢失 + 缺失结构件”的 DOCX，用模板修复。"""

import io
import os
import re
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fdlib import ziprepair as zr

TMP = tempfile.mkdtemp(prefix="fdtest_zip_")
FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_healthy_docx(path):
    from docx import Document
    from docx.shared import Inches
    from PIL import Image

    doc = Document()
    doc.add_heading("FileDoctor 测试文档", 0)
    doc.add_paragraph("这是用来测试 DOCX 修复功能的第一段中文内容。")
    doc.add_paragraph("Second paragraph for repair test.")
    img_path = os.path.join(TMP, "img.png")
    Image.new("RGB", (64, 32), (200, 60, 60)).save(img_path)
    doc.add_picture(img_path, width=Inches(1.0))
    doc.save(path)
    return path


def strip_central_dir(data: bytes, garbage_size: int = 1024) -> bytes:
    """删掉中央目录 + EOCD（模拟保存中断），并在尾部追加垃圾数据（模拟未写入区域）。"""
    idx = data.find(zr.CENTRAL_SIG)
    assert idx > 0, "找不到中央目录"
    return data[:idx] + bytes((i * 37) & 0xFF for i in range(garbage_size))


def main():
    print("== test_ziprepair ==")
    healthy = os.path.join(TMP, "healthy.docx")
    make_healthy_docx(healthy)
    with zipfile.ZipFile(healthy) as zf:
        healthy_parts = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}

    import docx
    text_expected = [p.text for p in docx.Document(healthy).paragraphs]

    # 1) 完整 ZIP 读取
    entries = zr.read_zip_entries(healthy)
    check("read_zip_entries 读取完整 docx", "word/document.xml" in entries)

    # 2) 构造损坏样本：去掉 3 个结构件，再砍掉中央目录
    drop = {"[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in healthy_parts.items():
            if name in drop:
                continue
            zf.writestr(name, data)
    damaged = strip_central_dir(buf.getvalue())
    damaged_path = os.path.join(TMP, "damaged.docx")
    with open(damaged_path, "wb") as f:
        f.write(damaged)

    # 3) 抢救
    sal = zr.salvage_zip_bytes(damaged)
    check("salvage 找到 word/document.xml",
          any(e["name"] == "word/document.xml" and e["crc_ok"] for e in sal["entries"]))
    check("salvage 没有把目录条目误算为损坏", sal["broken_count"] == 0, str(sal["broken_count"]))

    # 4) 用健康模板修复
    out = os.path.join(TMP, "repaired.docx")
    report = zr.repair_docx(damaged_path, out, template_path=healthy)
    check("repair_docx 返回 ok", report.get("ok") is True, str(report))
    filled = set(report.get("parts_filled", []))
    check("补齐 [Content_Types].xml", "[Content_Types].xml" in filled, str(filled))
    check("补齐 word/_rels/document.xml.rels", "word/_rels/document.xml.rels" in filled, str(filled))
    check("图片关系映射数量 > 0", report.get("images_mapped", 0) >= 1, str(report))

    # 5) 修复结果校验
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        check("修复后 ZIP 完整", zf.testzip() is None)
        check("含 [Content_Types].xml", "[Content_Types].xml" in names)
        check("含 _rels/.rels", "_rels/.rels" in names)
        check("含 document.xml.rels", "word/_rels/document.xml.rels" in names)
        check("正文未被改动", zf.read("word/document.xml") == healthy_parts["word/document.xml"])
        check("图片数量一致",
              len([n for n in names if n.startswith("word/media/")]) ==
              len([n for n in healthy_parts if n.startswith("word/media/")]))
        rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
        defined = set(re.findall(r'Id="(rId\d+)"', rels))
        embed = set(re.findall(r'r:embed="(rId\d+)"',
                              zf.read("word/document.xml").decode("utf-8")))
        check("文档引用的图片关系全部有定义", embed <= defined, f"缺 {sorted(embed - defined)}")

    repaired = docx.Document(out)
    check("修复后可被 python-docx 打开", len(repaired.paragraphs) == len(text_expected))
    check("正文文本一致", [p.text for p in repaired.paragraphs] == text_expected)

    # 6) 普通 ZIP 修复
    src_zip = os.path.join(TMP, "a.zip")
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("hello.txt", "hello file doctor 你好")
        zf.writestr("data.bin", b"\x00\x01\x02" * 1000)
    broken_zip = os.path.join(TMP, "a_broken.zip")
    with open(broken_zip, "wb") as f:
        f.write(strip_central_dir(open(src_zip, "rb").read()))
    out_zip = os.path.join(TMP, "a_fixed.zip")
    rep = zr.repair_zip(broken_zip, out_zip)
    check("普通 ZIP 抢救 ok", rep.get("ok") is True, str(rep))
    with zipfile.ZipFile(out_zip) as zf:
        check("ZIP 文本内容完整", zf.read("hello.txt") == "hello file doctor 你好".encode("utf-8"))
        check("ZIP 二进制内容完整", zf.read("data.bin") == b"\x00\x01\x02" * 1000)

    # 7) 若存在真实损坏样本则顺带验证（本机专有，缺席时跳过）
    real = os.environ.get("FILEDOCTOR_REAL_DOCX", "")
    if real and os.path.exists(real):
        sal = zr.salvage_zip_bytes(open(real, "rb").read())
        check("真实损坏样本可抢救出 word/document.xml",
              any(e["name"] == "word/document.xml" and e["crc_ok"] for e in sal["entries"]),
              str(sal["recovered_count"]))

    shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
