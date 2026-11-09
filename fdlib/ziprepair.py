"""ZIP / DOCX 修复与重建。

核心思路：
1. **完整 ZIP**：直接交给 ``zipfile`` 校验/读取。
2. **损坏 ZIP（无中央目录、尾部被截断/覆盖）**：扫描文件中的局部文件头（``PK\\x03\\x04``），
   逐条解压并做 CRC 校验，把校验通过的条目抢救出来，再重建一个合法 ZIP。
3. **DOCX**：在 2 的基础上，用可选的“健康模板”（同源 docx）补齐 ``[Content_Types].xml``、
   ``_rels/.rels``、``word/_rels/document.xml.rels`` 等结构件；图片关系通过 **内容哈希 + 文档中
   图片出现顺序** 自动对应重建。
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile
import zlib
from xml.etree import ElementTree as ET

LOCAL_SIG = b"PK\x03\x04"
CENTRAL_SIG = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"
DATA_DESC_SIG = b"PK\x07\x08"

MAX_ENTRY_SIZE = 512 * 1024 * 1024          # 单条目解压上限（防 zip bomb）
MAX_TOTAL_SALVAGE = 2 * 1024 * 1024 * 1024  # 抢救总量上限

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

_CT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_CT_STYLES = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
_CT_SETTINGS = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
_CT_FONTTABLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
_CT_THEME = "application/vnd.openxmlformats-officedocument.theme+xml"
_CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
_CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_CUSTOM = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


# --------------------------------------------------------------------------- 工具函数
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def _unique_path(outdir: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    cand = os.path.join(outdir, filename)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(outdir, f"{base}-{i}{ext}")
        i += 1
    return cand


# --------------------------------------------------------------------------- 完整 ZIP
def extract_zip_bytes(data: bytes) -> dict | None:
    """尝试把 *data* 当作完整 ZIP 读取。返回 ``{"entries": [(name, bytes)], "is_docx": bool}``；失败返回 None。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        if zf.testzip() is not None:
            return None
        entries = []
        for info in zf.infolist():
            if info.is_dir() or info.filename.endswith("/"):
                continue
            entries.append((info.filename, zf.read(info.filename)))
        names = [n for n, _ in entries]
        return {"entries": entries, "is_docx": ("word/document.xml" in names or "[Content_Types].xml" in names)}
    except Exception:
        return None


def read_zip_entries(path: str) -> dict[str, bytes]:
    """读取一个正常 ZIP/DOCX 的全部文件条目（不含目录）。"""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename.endswith("/"):
                continue
            out[info.filename] = zf.read(info.filename)
    return out


def rebuild_zip_bytes(entries) -> bytes:
    """把条目重建为 ZIP 字节串（供雕刻模块直接写文件用）。"""
    normalized = _normalize_entries(entries)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in normalized:
            zf.writestr(name, data)
    return buf.getvalue()


def salvage_zip_files(data: bytes) -> dict[str, bytes]:
    """便捷版：返回 ``{name: bytes}``（只含 CRC 校验通过的条目）。"""
    sal = salvage_zip_bytes(data)
    return {e["name"]: e["data"] for e in sal["entries"]
            if e["data"] is not None and not e["name"].endswith("/")}


def _normalize_entries(entries) -> list[tuple[str, bytes]]:
    normalized = []
    for item in entries:
        if isinstance(item, dict):
            name, data = item.get("name"), item.get("data")
        else:
            name, data = item
        if not name or data is None:
            continue
        safe = _safe_name(name)
        if not safe or safe.endswith("/"):
            continue
        normalized.append((safe, data))
    return normalized


def rebuild_zip(entries, out_path: str) -> str:
    """把 ``[(name, bytes), ...]`` 或 ``[{"name","data"}, ...]`` 重建为一个新 ZIP 文件。"""
    outdir = os.path.dirname(os.path.abspath(out_path))
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    normalized = _normalize_entries(entries)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in normalized:
            zf.writestr(name, data)
    return out_path


# --------------------------------------------------------------------------- 损坏 ZIP 抢救
def _parse_local_header(data: bytes, off: int) -> dict | None:
    if off + 30 > len(data):
        return None
    try:
        (sig, ver, flags, method, mtime, mdate, crc, csize, usize,
         nlen, elen) = struct.unpack_from("<IHHHHHIIIHH", data, off)
    except struct.error:
        return None
    if sig != 0x04034B50:
        return None
    if nlen <= 0 or nlen > 4096 or elen > 65535:
        return None
    if off + 30 + nlen > len(data):
        return None
    try:
        name = data[off + 30: off + 30 + nlen].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 32 for c in name):
        return None
    if method not in (0, 8):
        return None
    return {
        "offset": off, "version": ver, "flags": flags, "method": method,
        "crc": crc, "csize": csize, "usize": usize, "name": name,
        "data_offset": off + 30 + nlen + elen,
    }


def _inflate_raw(data: bytes) -> tuple[bytes | None, int]:
    """对 raw deflate 数据解压，返回 (输出, 消耗的输入字节数)。失败返回 (None, 0)。"""
    dec = zlib.decompressobj(-15)
    out = bytearray()
    try:
        # 分块喂入，边解压边检查上限
        step = 1 << 20
        pos = 0
        while pos < len(data):
            chunk = data[pos:pos + step]
            pos += len(chunk)
            out += dec.decompress(chunk)
            if len(out) > MAX_ENTRY_SIZE:
                return None, 0
            if dec.eof:
                break
        if not dec.eof:
            out += dec.flush()
        if not dec.eof:
            return None, 0
        consumed = pos - len(dec.unused_data)
        return bytes(out), consumed
    except zlib.error:
        return None, 0


def _extract_one(data: bytes, hdr: dict) -> dict:
    """按局部头解析单条目；返回 ``{"name","data","crc_ok","size"}``（失败时 data 为 None）。"""
    ds = hdr["data_offset"]
    name = hdr["name"]
    result = {"name": name, "data": None, "crc_ok": False, "size": 0,
              "method": hdr["method"], "offset": hdr["offset"]}
    if name.endswith("/"):
        # 目录条目：没有数据，直接视为有效
        result.update(data=b"", crc_ok=True, size=0, is_dir=True)
        return result
    if hdr["csize"] == 0 and hdr["usize"] == 0 and hdr["crc"] == 0:
        result.update(data=b"", crc_ok=True, size=0)
        return result
    if ds > len(data):
        return result
    if hdr["method"] == 0:
        if hdr["csize"] > 0:
            raw = data[ds: ds + hdr["csize"]]
        else:
            # data descriptor 场景：读到下一个签名或文件尾
            end = len(data)
            for sig in (LOCAL_SIG, CENTRAL_SIG, EOCD_SIG):
                p = data.find(sig, ds + 1)
                if 0 <= p < end:
                    end = p
            raw = data[ds:end]
        out = raw
        crc_ok = (zlib.crc32(out) & 0xFFFFFFFF) == hdr["crc"]
        result.update(data=out, crc_ok=crc_ok, size=len(out))
        return result
    # method == 8 deflate
    if hdr["csize"] > 0:
        chunk = data[ds: ds + hdr["csize"]]
        out, consumed = _inflate_raw(chunk)
        if out is not None:
            crc_ok = (zlib.crc32(out) & 0xFFFFFFFF) == hdr["crc"]
            result.update(data=out, crc_ok=crc_ok, size=len(out))
        return result
    # csize 未知：解压并自动判断消耗长度
    out, consumed = _inflate_raw(data[ds:])
    if out is not None:
        crc_ok = (zlib.crc32(out) & 0xFFFFFFFF) == hdr["crc"]
        result.update(data=out, crc_ok=crc_ok, size=len(out))
    return result


def salvage_zip_bytes(data: bytes, max_entries: int = 2000) -> dict:
    """从损坏的 ZIP 数据中抢救条目（扫描局部文件头 + CRC 校验）。"""
    entries: list[dict] = []
    broken: list[dict] = []
    pos = 0
    total_out = 0
    seen_offsets: set[int] = set()
    while len(entries) + len(broken) < max_entries:
        off = data.find(LOCAL_SIG, pos)
        if off < 0:
            break
        if off in seen_offsets:
            pos = off + 4
            continue
        seen_offsets.add(off)
        hdr = _parse_local_header(data, off)
        if hdr is None:
            pos = off + 4
            continue
        ent = _extract_one(data, hdr)
        if ent["data"] is not None and ent["crc_ok"]:
            entries.append(ent)
            total_out += ent["size"]
            # 下一个局部头从本条目数据结束处开始（目录/空文件则数据长度 0）
            pos = hdr["data_offset"] + max(hdr["csize"], 0)
            if pos <= hdr["offset"]:
                pos = hdr["offset"] + 4
            if total_out > MAX_TOTAL_SALVAGE:
                break
        else:
            broken.append(ent)
            pos = off + 4
    names = [e["name"] for e in entries]
    return {
        "entries": entries,
        "broken": broken,
        "is_docx": ("word/document.xml" in names or "[Content_Types].xml" in names),
        "recovered_count": len(entries),
        "broken_count": len(broken),
    }


# --------------------------------------------------------------------------- DOCX 结构补齐
def _xml(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def _doc_drawings(document_xml: bytes) -> list[dict]:
    """提取 document.xml 中按顺序出现的图片引用（png 的 r:embed 与 svg 的 r:embed）。"""
    root = _xml(document_xml)
    out = []
    for drawing in root.iter("{%s}drawing" % NS["w"]):
        blip = drawing.find(".//a:blip", NS)
        svg = drawing.find(".//asvg:svgBlip", NS)
        extent = drawing.find(".//wp:extent", NS)
        out.append({
            "embed": blip.get(R_EMBED) if blip is not None else None,
            "svg": svg.get(R_EMBED) if svg is not None else None,
            "cx": extent.get("cx") if extent is not None else None,
            "cy": extent.get("cy") if extent is not None else None,
        })
    return out


def _parse_rels(rels_xml: bytes) -> dict[str, dict]:
    root = _xml(rels_xml)
    out = {}
    for rel in root.findall("{%s}Relationship" % _PKG_REL_NS):
        rid = rel.get("Id")
        if rid:
            out[rid] = {"target": rel.get("Target", ""), "type": rel.get("Type", "")}
    return out


def _media_hash_map(parts: dict[str, bytes]) -> dict[str, str]:
    out = {}
    for name, data in parts.items():
        if name.startswith("word/media/"):
            out[_sha256(data)] = name
    return out


def _build_content_types(parts: dict[str, bytes]) -> bytes:
    defaults = {
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": "application/xml",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "emf": "image/x-emf",
        "wmf": "image/x-wmf",
        "svg": "image/svg+xml",
    }
    overrides = []
    if "word/document.xml" in parts:
        overrides.append(("/word/document.xml", _CT_MAIN))
    for name, ctype in (("word/styles.xml", _CT_STYLES), ("word/settings.xml", _CT_SETTINGS),
                        ("word/fontTable.xml", _CT_FONTTABLE), ("word/theme/theme1.xml", _CT_THEME),
                        ("docProps/app.xml", _CT_APP), ("docProps/core.xml", _CT_CORE),
                        ("docProps/custom.xml", _CT_CUSTOM)):
        if name in parts:
            overrides.append(("/" + name, ctype))
    used_ext = {name.rsplit(".", 1)[-1].lower() for name in parts if "." in name}
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">']
    for ext, ctype in defaults.items():
        if ext in used_ext or ext == "rels":
            xml.append(f'<Default Extension="{ext}" ContentType="{ctype}"/>')
    for part, ctype in overrides:
        xml.append(f'<Override PartName="{part}" ContentType="{ctype}"/>')
    xml.append("</Types>")
    return "".join(xml).encode("utf-8")


def _build_root_rels(parts: dict[str, bytes]) -> bytes:
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    rid = 1
    for name, ctype in (("docProps/app.xml", _CT_APP), ("docProps/core.xml", _CT_CORE),
                        ("docProps/custom.xml", _CT_CUSTOM)):
        if name in parts:
            xml.append(f'<Relationship Id="rId{rid}" Target="{name}" Type="{ctype}"/>')
            rid += 1
    xml.append(f'<Relationship Id="rId{rid}" Target="word/document.xml" '
               f'Type="{_REL_NS}/officeDocument"/>')
    xml.append("</Relationships>")
    return "".join(xml).encode("utf-8")


def _map_images_from_template(recovered: dict[str, bytes], template: dict[str, bytes],
                              report: dict) -> tuple[dict[str, str], dict[str, dict]]:
    """用模板的 document.xml + media 内容哈希，把恢复版 document.xml 里的 rId 映射到恢复版 media 文件名。"""
    if "word/document.xml" not in recovered or "word/_rels/document.xml.rels" not in template:
        return {}, {}
    template_rels = _parse_rels(template["word/_rels/document.xml.rels"])
    tmpl_media_hash = _media_hash_map(template)
    rec_media_hash = _media_hash_map(recovered)
    # 模板目标文件 -> 恢复版文件
    tmpl_to_rec: dict[str, str] = {}
    for name, data in template.items():
        if not name.startswith("word/media/"):
            continue
        h = _sha256(data)
        if h in rec_media_hash:
            tmpl_to_rec[name] = rec_media_hash[h]
    try:
        tmpl_draws = _doc_drawings(template["word/document.xml"])
        rec_draws = _doc_drawings(recovered["word/document.xml"])
    except ET.ParseError:
        report["warnings"].append("document.xml 解析失败，无法按顺序映射图片")
        return {}, template_rels
    mapping: dict[str, str] = {}
    if len(tmpl_draws) == len(rec_draws):
        for t, r in zip(tmpl_draws, rec_draws):
            for key in ("embed", "svg"):
                tid, rid = t.get(key), r.get(key)
                if not tid or not rid:
                    continue
                rel = template_rels.get(tid)
                if not rel:
                    continue
                tmpl_target = "word/" + rel["target"]
                rec_name = tmpl_to_rec.get(tmpl_target)
                if rec_name:
                    mapping[rid] = rec_name
        report["images_aligned_by_order"] = True
    else:
        report["warnings"].append(
            f"模板图片数({len(tmpl_draws)})与恢复版({len(rec_draws)})不一致，改用按序回退映射")
    if not mapping:
        # 回退：按文件名排序，把图片 rId 依次分配
        rid_order = sorted({d["embed"] for d in rec_draws if d.get("embed")} |
                           {d["svg"] for d in rec_draws if d.get("svg")},
                           key=lambda s: int(s[3:]) if s[3:].isdigit() else 0)
        media = sorted(k for k in recovered if k.startswith("word/media/"))
        for rid, name in zip(rid_order, media):
            mapping[rid] = name
        report["warnings"].append("图片关系为回退映射，可能与原位置不完全一致")
    return mapping, template_rels


def _build_document_rels(parts: dict[str, bytes], image_map: dict[str, str]) -> bytes:
    """根据现有部件 + 图片 rId 映射，重建 word/_rels/document.xml.rels。"""
    ref_ids = set(image_map)
    line = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    order = [("word/styles.xml", "styles", _CT_STYLES),
             ("word/settings.xml", "settings", _CT_SETTINGS),
             ("word/fontTable.xml", "fontTable", _CT_FONTTABLE),
             ("word/theme/theme1.xml", "theme", _CT_THEME)]
    next_id = 1
    used = set()
    for part, reltype, _ in order:
        if part not in parts:
            continue
        while f"rId{next_id}" in ref_ids or f"rId{next_id}" in used:
            next_id += 1
        rid = f"rId{next_id}"
        used.add(rid)
        target = part[len("word/"):]
        line.append(f'<Relationship Id="{rid}" Target="{target}" Type="{_REL_NS}/{reltype}"/>')
        next_id += 1
    for rid in sorted(image_map, key=lambda s: int(s[3:]) if s[3:].isdigit() else 0):
        name = image_map[rid]
        target = name[len("word/"):]
        line.append(f'<Relationship Id="{rid}" Target="{target}" Type="{_REL_NS}/image"/>')
    line.append("</Relationships>")
    return "".join(line).encode("utf-8")


def _looks_like_docx(names: set[str]) -> bool:
    return "word/document.xml" in names


def repair_docx(src_path: str, out_path: str, template_path: str | None = None) -> dict:
    """修复一个损坏的 DOCX。"""
    report: dict = {"ok": False, "mode": None, "template_used": bool(template_path),
                    "parts_filled": [], "images_mapped": 0, "warnings": [], "errors": []}
    with open(src_path, "rb") as f:
        data = f.read()
    # 先试完整 ZIP
    complete = extract_zip_bytes(data)
    if complete and _looks_like_docx({n for n, _ in complete["entries"]}):
        parts = dict(complete["entries"])
        report.update(ok=True, mode="complete", parts_recovered=len(parts))
        rebuild_zip(sorted(parts.items()), out_path)
        return report
    salvage = salvage_zip_bytes(data)
    recovered = {e["name"]: e["data"] for e in salvage["entries"]
                 if e["data"] is not None and not e["name"].endswith("/")}
    if not _looks_like_docx(set(recovered)):
        report["errors"].append("没有找到 word/document.xml，无法作为 DOCX 修复")
        report["salvage"] = {k: salvage[k] for k in ("recovered_count", "broken_count")}
        return report

    template: dict[str, bytes] = {}
    template_rels: dict[str, dict] = {}
    if template_path and os.path.exists(template_path):
        try:
            template = read_zip_entries(template_path)
            if "word/_rels/document.xml.rels" in template:
                template_rels = _parse_rels(template["word/_rels/document.xml.rels"])
        except Exception as exc:  # noqa: BLE001
            report["warnings"].append(f"模板读取失败：{exc}")
            template = {}

    # 结构件补齐
    needed = ["[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels",
              "word/styles.xml", "word/settings.xml", "word/fontTable.xml",
              "word/theme/theme1.xml"]
    for name in needed:
        if name in recovered:
            continue
        if name in template:
            recovered[name] = template[name]
            report["parts_filled"].append(name)
        elif name == "[Content_Types].xml":
            recovered[name] = _build_content_types(recovered)
            report["parts_filled"].append(name)
        elif name == "_rels/.rels":
            recovered[name] = _build_root_rels(recovered)
            report["parts_filled"].append(name)

    # 图片关系重建（仅当关系文件缺失或来自模板时）
    rels_missing = "word/_rels/document.xml.rels" not in {e["name"] for e in salvage["entries"]
                                                          if e["data"] is not None}
    if rels_missing:
        image_map: dict[str, str] = {}
        if template:
            image_map, template_rels = _map_images_from_template(recovered, template, report)
        if image_map:
            recovered["word/_rels/document.xml.rels"] = _build_document_rels(recovered, image_map)
            report["images_mapped"] = len(image_map)
        elif template_rels:
            # 模板关系文件直接可用时保留（引用的 media 与恢复版同名假设）
            report["warnings"].append("图片关系未能按内容匹配，保留模板关系文件")
        # 清理没有对应 media 的模板关系（避免 Word 报错）——交给上层渲染验证
    # 重排：ContentTypes 优先，随后 _rels/.rels，再其它
    priority = ["[Content_Types].xml", "_rels/.rels"]
    ordered = [(n, recovered[n]) for n in priority if n in recovered]
    ordered += [(n, d) for n, d in sorted(recovered.items()) if n not in priority]
    rebuild_zip(ordered, out_path)
    report.update(ok=True, mode="salvaged", parts_recovered=len(recovered),
                  salvage={"recovered": salvage["recovered_count"], "broken": salvage["broken_count"]})
    return report


def repair_zip(src_path: str, out_path: str) -> dict:
    """修复普通 ZIP（非 docx）：完整则重建，损坏则抢救。"""
    with open(src_path, "rb") as f:
        data = f.read()
    complete = extract_zip_bytes(data)
    if complete:
        rebuild_zip(complete["entries"], out_path)
        return {"ok": True, "mode": "complete", "entries": len(complete["entries"])}
    salvage = salvage_zip_bytes(data)
    entries = [(e["name"], e["data"]) for e in salvage["entries"]
               if e["data"] is not None and not e["name"].endswith("/")]
    if not entries:
        return {"ok": False, "mode": "salvaged", "entries": 0,
                "errors": ["没有可抢救的条目"]}
    rebuild_zip(entries, out_path)
    return {"ok": True, "mode": "salvaged", "entries": len(entries),
            "broken": salvage["broken_count"],
            "warnings": [f"丢弃/损坏条目 {salvage['broken_count']} 个"] if salvage["broken_count"] else []}


if __name__ == "__main__":  # 简单自测
    import sys
    print("ziprepair 自带自测：")
    print("  用法示例：repair_docx(src, out, template)")
    sys.exit(0)
