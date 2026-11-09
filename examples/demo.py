"""FileDoctor 一键演示（examples/demo.py）。

依次演示：
1. 生成一个"坏 U 盘"镜像（随机数据 + 残缺 docx + PNG + 中文文本 + 旧程序残留数据）；
2. probe 识别镜像；
3. repair 修复残缺 docx；
4. carve 从镜像雕刻恢复 docx/png 与文本碎片；
5. 生成 exFAT 镜像并恢复"已删除文件"（复用 tests/gen_exfat_image.py）；
6. capacity 快速写入完整性体检（默认 64MB，写在本目录下的临时目录）。

运行：python examples/demo.py [--keep]
输出：examples/out/（含 DEMO_REPORT.md 与恢复出的文件）
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "examples", "out")
PY = sys.executable
sys.path.insert(0, ROOT)

CHECKS = []


def section(name):
    print("\n" + "=" * 70)
    print(name)
    print("=" * 70)


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))


def make_healthy_docx(path):
    from docx import Document
    doc = Document()
    doc.add_heading("FileDoctor 演示文档", level=1)
    doc.add_paragraph("这是演示用的 Word 文档正文：文件恢复、U 盘、扩容盘、误删数据。")
    doc.save(path)


def make_broken_docx(full_bytes):
    pos = 0
    while full_bytes[pos:pos + 4] == b"PK\x03\x04":
        (_s, _v, _f, _m, _mt, _md, _crc, csize, _us, nl, el) = struct.unpack_from(
            "<IHHHHHIIIHH", full_bytes, pos)
        pos = pos + 30 + nl + el + csize
    return full_bytes[:pos]


def build_fake_usb_image(img_path):
    from PIL import Image
    tmp = os.path.join(OUT, "_materials")
    os.makedirs(tmp, exist_ok=True)
    healthy = os.path.join(tmp, "healthy.docx")
    make_healthy_docx(healthy)
    docx_bytes = open(healthy, "rb").read()
    broken = make_broken_docx(docx_bytes)
    png_path = os.path.join(tmp, "pic.png")
    Image.new("RGB", (40, 20), (30, 160, 220)).save(png_path)
    png_bytes = open(png_path, "rb").read()
    text = ("FileDoctor 演示：这是一段中文文本碎片，用于验证 carve --text 能把它捞回来。\n"
            "第二行：数据恢复、U盘、扩容盘、误删、文档修复。\n") * 10
    text_bytes = text.encode("utf-8")
    rnd = random.Random(20261109)
    img = bytearray(rnd.randbytes(8 * 1024 * 1024))
    img[1_000_000:1_000_000 + len(broken)] = broken
    img[3_000_000:3_000_000 + len(png_bytes)] = png_bytes
    img[5_000_000:5_000_000 + len(text_bytes)] = text_bytes
    with open(img_path, "wb") as f:
        f.write(img)
    return {"broken_docx": broken, "png": png_bytes, "text": text_bytes}


def run_cli(args):
    proc = subprocess.run([PY, os.path.join(ROOT, "filetool.py")] + args,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留演示产物")
    args = ap.parse_args()
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    report_lines = ["# FileDoctor 一键演示报告", ""]

    # ---------------------------------------------------------------- 1. 造“坏 U 盘”
    section("1. 生成模拟“坏 U 盘”镜像（8MB：残缺 docx + PNG + 中文文本 + 随机垃圾）")
    img_path = os.path.join(OUT, "fake_usb.img")
    materials = build_fake_usb_image(img_path)
    print(f"  镜像：{img_path}  ({os.path.getsize(img_path)} 字节)")
    print(f"  残缺 docx：{len(materials['broken_docx'])} 字节；PNG：{len(materials['png'])} 字节；"
          f"中文文本：{len(materials['text'])} 字节")
    report_lines += ["## 1. 模拟坏 U 盘镜像", "",
                     f"- `{os.path.basename(img_path)}`（{os.path.getsize(img_path)} 字节）",
                     f"- 内嵌：残缺 docx {len(materials['broken_docx'])}B、PNG {len(materials['png'])}B、"
                     f"UTF-8 中文文本 {len(materials['text'])}B", ""]

    # ---------------------------------------------------------------- 2. probe
    section("2. probe：识别镜像的引导扇区")
    proc = run_cli(["probe", img_path])
    try:
        data = json.loads(proc.stdout)
    except Exception:
        data = {"raw": proc.stdout}
    print(f"  exit={proc.returncode}  fs={data.get('boot', {}).get('fs')}  size={data.get('device_size')}")
    check("probe 命令成功", proc.returncode == 0)

    # ---------------------------------------------------------------- 3. repair
    section("3. repair：修复残缺 docx（无中央目录 → 重建）")
    broken_path = os.path.join(OUT, "fake_usb_broken.docx")
    with open(broken_path, "wb") as f:
        f.write(materials["broken_docx"])
    fixed_path = os.path.join(OUT, "repaired.docx")
    proc = run_cli(["repair", broken_path, "-o", fixed_path])
    print(f"  exit={proc.returncode}")
    ok = proc.returncode == 0 and os.path.isfile(fixed_path) and zipfile.is_zipfile(fixed_path)
    if ok:
        with zipfile.ZipFile(fixed_path) as zf:
            names = zf.namelist()
            ok = "word/document.xml" in names and zf.testzip() is None
            print(f"  修复结果：{len(names)} 个部件，含 word/document.xml={('word/document.xml' in names)}")
    check("repair 修复出可打开的 docx", ok)
    report_lines += ["## 2. repair（修复残缺 docx）", "",
                     f"- 输出：`repaired.docx`；结果：{'成功' if ok else '失败'}", ""]

    # ---------------------------------------------------------------- 4. carve
    section("4. carve：从镜像按特征码雕刻恢复 docx/png 与文本碎片")
    carved_dir = os.path.join(OUT, "carved")
    proc = run_cli(["carve", img_path, "-o", carved_dir, "--types", "docx,png,zip",
                    "--text", "--min-size", "100"])
    print(f"  exit={proc.returncode}")
    carved = {}
    try:
        carved = json.loads(proc.stdout)
    except Exception:
        pass
    print(f"  scanned_bytes={carved.get('scanned_bytes')} by_type={carved.get('by_type')} "
          f"texts={carved.get('texts')}")
    files = []
    for root, _, names in os.walk(carved_dir):
        files += [os.path.join(root, n) for n in names]
    docx_ok = any(f.endswith(".docx") and zipfile.is_zipfile(f) for f in files)
    png_ok = any(f.endswith(".png") for f in files)
    txt_ok = any(f.endswith(".txt") for f in files)
    check("carve 恢复出 docx", docx_ok, str(files))
    check("carve 恢复出 png", png_ok)
    check("carve 恢复出文本碎片", txt_ok)
    report_lines += ["## 3. carve（特征码雕刻）", "",
                     f"- scanned_bytes={carved.get('scanned_bytes')}",
                     f"- by_type={carved.get('by_type')}",
                     f"- 恢复文件：{[os.path.basename(f) for f in files]}", ""]

    # ---------------------------------------------------------------- 5. exFAT
    section("5. exfat：从 exFAT 镜像恢复已删除文件")
    spec = importlib.util.spec_from_file_location(
        "gen_exfat_image", os.path.join(ROOT, "tests", "gen_exfat_image.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    exfat_img = os.path.join(OUT, "exfat.img")
    gen.main([exfat_img])  # 生成默认 exFAT 镜像（含正常文件 + 删除文件）
    check("exFAT 测试镜像已生成", os.path.isfile(exfat_img) and os.path.getsize(exfat_img) > 0)
    exfat_out = os.path.join(OUT, "exfat_recovered")
    proc = run_cli(["exfat", exfat_img, "-o", exfat_out])
    print(f"  exit={proc.returncode}")
    try:
        rec = json.loads(proc.stdout)
    except Exception:
        rec = {}
    count = rec.get("count")
    print(f"  recovered={count} errors={rec.get('errors')}")
    has_deleted = any(f.get("deleted") for f in rec.get("files", []))
    check("exfat 恢复成功", proc.returncode == 0 and (count or 0) >= 1)
    check("exfat 恢复出已删除文件", has_deleted)
    report_lines += ["## 4. exfat（恢复已删除文件）", "",
                     f"- 恢复数量={count}，含已删除文件={has_deleted}", ""]

    # ---------------------------------------------------------------- 6. capacity
    section("6. capacity：写入/读回完整性体检（64MB）")
    cap_dir = os.path.join(OUT, "capacity")
    os.makedirs(cap_dir, exist_ok=True)
    proc = run_cli(["capacity", cap_dir, "--block-mb", "8"])
    print(f"  exit={proc.returncode}")
    try:
        cap = json.loads(proc.stdout)
    except Exception:
        cap = {}
    check("capacity 体检通过", proc.returncode == 0 and cap.get("ok") is True, str(cap.get("errors")))
    report_lines += ["## 5. capacity（容量/完整性体检）", "",
                     f"- ok={cap.get('ok')}，写入 {cap.get('write_bytes')}B，"
                     f"写速 {cap.get('write_speed_mbps')} MB/s，读速 {cap.get('read_speed_mbps')} MB/s", ""]

    # ---------------------------------------------------------------- 汇总
    section("汇总")
    total = len(CHECKS)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    for name, ok, detail in CHECKS:
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + ("" if ok else f"  {detail}"))
    report_lines += ["## 汇总", "", f"- 检查项：{passed}/{total} 通过", ""]
    print(f"\n结果：{passed}/{total} 通过")
    with open(os.path.join(OUT, "DEMO_REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    print(f"报告：{os.path.join(OUT, 'DEMO_REPORT.md')}")
    if not args.keep:
        pass  # 演示产物保留在 examples/out（用户可查看）；临时素材可删
    shutil.rmtree(os.path.join(OUT, "_materials"), ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
