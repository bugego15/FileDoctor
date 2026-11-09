"""NTFS 删除文件扫描/恢复（基于 The Sleuth Kit 的 Python 绑定 ``pytsk3``）。

依赖安装（Windows 10/11 有预编译 wheel，无需编译）::

    python -m pip install pytsk3

设计：

- 输入可以是 **镜像文件**（如 dd/WinHex 导出的 .img）或 **原始设备路径**
  （``\\\\.\\G:``、``\\\\.\\PhysicalDriveN``；读物理盘/固定卷一般需要管理员权限）。
- ``offset`` 不传时自动探测：先试 offset=0，失败则用 ``pytsk3.Volume_Info``
  枚举分区，挑 NTFS 分区。
- 两种删除文件来源：
  1. 目录项遍历：``TSK_FS_META_FLAG_UNALLOC`` 标记的条目（名字通常还在）；
  2. ``scan_unallocated=True`` 时扫描 MFT 里未分配的 inode（孤儿记录，
     名字可能已丢，显示为 ``inode_<N>``，但 data run 有时还在，可恢复内容）。
- 删除文件的内容能否读出来取决于 data run 是否还在 MFT 记录里：
  读不出/读不全时返回 ``recovered=False/partial``，此时应改用特征码雕刻（``filetool carve``）。

命令行入口见 ``filetool.py ntfs``。
"""

from __future__ import annotations

import os
import re

try:  # pytsk3 是可选依赖，未安装时其他功能不受影响
    import pytsk3  # type: ignore

    HAS_PYTSK3 = True
    PYTSK3_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    pytsk3 = None  # type: ignore
    HAS_PYTSK3 = False
    PYTSK3_IMPORT_ERROR = str(exc)

NTFS_TYPE = "NTFS"
# pytsk3 的 fs.info.ftype 使用 TSK 的枚举值；NTFS 对应 pytsk3.TSK_FS_TYPE_NTFS（=1）。
# 注意：不同 TSK 版本/绑定里数值别直接写死，优先用 pytsk3 常量，缺失时回退 1。
_NTFS_FTYPE_VALUE = int(getattr(pytsk3, "TSK_FS_TYPE_NTFS", 1)) if HAS_PYTSK3 else 1
_WIN_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def require_pytsk3() -> None:
    """检查 pytsk3 是否可用，不可用时抛出带安装提示的 RuntimeError。"""
    if not HAS_PYTSK3:
        raise RuntimeError(
            "需要 pytsk3 才能使用 NTFS 恢复功能。安装：python -m pip install pytsk3"
            f"（当前 import 失败：{PYTSK3_IMPORT_ERROR}）")


def _safe_name(name: str) -> str:
    name = _WIN_BAD_CHARS.sub("_", name).strip().rstrip(".")
    if not name:
        name = "_unnamed"
    if name.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                        "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3",
                        "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}:
        name = "_" + name
    return name


def _unique_path(outdir: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(outdir, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(outdir, f"{base}-{i}{ext}")
        i += 1
    return candidate


def _decode_name(raw) -> str:
    try:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)
    except Exception:  # noqa: BLE001
        return str(raw)


def _meta_type_name(meta) -> str:
    try:
        if meta.type == pytsk3.TSK_FS_META_TYPE_DIR:
            return "dir"
        if meta.type == pytsk3.TSK_FS_META_TYPE_REG:
            return "file"
        return str(meta.type)
    except Exception:  # noqa: BLE001
        return "?"


class NtfsVolume:
    """一个已打开的 NTFS 卷（pytsk3 的 Img_Info + FS_Info）。"""

    def __init__(self, path: str, offset: int | None = None) -> None:
        require_pytsk3()
        self.path = path
        self.img = None
        self.fs = None
        self.offset = offset
        self.info: dict = {"path": path}
        try:
            self.img = pytsk3.Img_Info(path)
        except Exception as exc:  # noqa: BLE001
            raise OSError(
                f"打开镜像/设备失败：{exc}（读物理盘/固定卷请用管理员权限运行；"
                f"可移动盘可先确认盘符是否存在）") from exc
        if offset is None:
            self.fs, self.offset = self._auto_open()
        else:
            try:
                self.fs = pytsk3.FS_Info(self.img, offset=int(offset))
            except Exception as exc:  # noqa: BLE001
                raise OSError(f"在偏移 {offset} 打开文件系统失败：{exc}") from exc
        ftype = int(getattr(self.fs.info, "ftype", 0))
        self.info.update(
            offset=self.offset,
            fs_type=self._fs_type_name(ftype),
            ftype=ftype,
            block_size=int(getattr(self.fs.info, "block_size", 0) or 0),
            first_inum=int(getattr(self.fs.info, "first_inum", 0) or 0),
            last_inum=int(getattr(self.fs.info, "last_inum", 0) or 0),
        )

    # ------------------------------------------------------------------ 打开辅助
    @staticmethod
    def _fs_type_name(ftype: int) -> str:
        return NTFS_TYPE if ftype == _NTFS_FTYPE_VALUE else f"0x{ftype:x}"

    def _check_ntfs(self) -> None:
        if int(getattr(self.fs.info, "ftype", 0)) != _NTFS_FTYPE_VALUE:
            raise OSError(
                f"该卷不是 NTFS（ftype={getattr(self.fs.info, 'ftype', '?')}），"
                "本模块只处理 NTFS；exFAT/FAT 请用 'exfat' 或 'carve'")

    def _auto_open(self):
        # 1) 卷/单分区镜像：offset 0 直接打开
        try:
            fs = pytsk3.FS_Info(self.img, offset=0)
            if int(getattr(fs.info, "ftype", 0)) == _NTFS_FTYPE_VALUE:
                return fs, 0
        except Exception:  # noqa: BLE001
            pass
        # 2) 整盘镜像：枚举分区表，找 NTFS 分区
        try:
            volume_info = pytsk3.Volume_Info(self.img)
        except Exception as exc:  # noqa: BLE001
            raise OSError(
                "无法识别文件系统/分区表（镜像可能不是 NTFS，或偏移不对；"
                f"可先用 probe 命令确认，再传 --offset）：{exc}") from exc
        try:
            for part in volume_info:
                desc = (_decode_name(part.desc) if part.desc else "").upper()
                if "NTFS" not in desc:
                    continue
                offset = int(part.start) * 512
                try:
                    fs = pytsk3.FS_Info(self.img, offset=offset)
                except Exception:  # noqa: BLE001
                    continue
                if int(getattr(fs.info, "ftype", 0)) == _NTFS_FTYPE_VALUE:
                    return fs, offset
        finally:
            close = getattr(volume_info, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        raise OSError("分区表里没有找到 NTFS 分区（可用 --offset 手动指定）")

    # ------------------------------------------------------------------ 目录遍历
    def _entry_from_meta(self, name: str, path: str, meta) -> dict:
        deleted = bool(getattr(meta, "flags", 0) & pytsk3.TSK_FS_META_FLAG_UNALLOC)
        return {
            "name": name,
            "path": path,
            "inode": int(getattr(meta, "addr", 0) or 0),
            "size": int(getattr(meta, "size", 0) or 0),
            "deleted": deleted,
            "type": _meta_type_name(meta),
            "mtime": int(getattr(meta, "mtime", 0) or 0),
            "ctime": int(getattr(meta, "ctime", 0) or 0),
            "source": "dir",
        }

    def _walk(self, dir_obj, path: str, include_deleted: bool, extensions, errors,
              depth: int = 0, include_system: bool = False):
        if depth > 32:
            return
        try:
            entries = list(dir_obj)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"读取目录 {path} 失败：{exc}")
            return
        for entry in entries:
            try:
                name = entry.info.name
                if name is None or name.name is None:
                    continue
                name_str = _decode_name(name.name)
                if name_str in (".", ".."):
                    continue
                # TSK 的虚拟目录 / NTFS 系统元数据文件（$MFT、$Bitmap、$Extend 等）默认隐藏
                if name_str == "$OrphanFiles":
                    continue
                if not include_system and name_str.startswith("$"):
                    continue
                meta = entry.info.meta
                full = path.rstrip("/") + "/" + name_str
                if meta is None:
                    # 只有目录项、MFT 记录已释放：名字还在，内容不可读
                    if include_deleted:
                        yield {
                            "name": name_str, "path": full, "inode": 0, "size": None,
                            "deleted": True, "type": "unknown", "mtime": 0, "ctime": 0,
                            "source": "dir_entry_only",
                            "note": "仅剩目录项（MFT 记录已释放），内容不可直接读取",
                        }
                    continue
                item = self._entry_from_meta(name_str, full, meta)
                if item["deleted"] and not include_deleted:
                    continue
                if item["type"] == "dir":
                    if not item["deleted"]:  # 已删除目录的簇可能已释放，不递归
                        try:
                            sub = entry.as_directory()
                        except Exception:  # noqa: BLE001
                            try:
                                sub = self.fs.open_dir(path=full)
                            except Exception as exc:  # noqa: BLE001
                                errors.append(f"进入目录 {full} 失败：{exc}")
                                continue
                        yield from self._walk(sub, full, include_deleted, extensions,
                                              errors, depth + 1, include_system=include_system)
                    continue
                ext = os.path.splitext(name_str)[1].lower().lstrip(".")
                if extensions and ext not in extensions:
                    continue
                yield item
            except Exception as exc:  # noqa: BLE001
                errors.append(f"解析目录项失败（{path}）：{exc}")

    def _scan_unallocated(self, extensions, errors: list) -> list[dict]:
        """扫描 MFT 里未分配的 inode（孤儿删除记录），尝试恢复内容。"""
        out: list[dict] = []
        try:
            first = int(self.fs.info.first_inum)
            last = int(self.fs.info.last_inum)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"读取 inode 范围失败：{exc}")
            return out
        for ino in range(first, last + 1):
            try:
                f = self.fs.open_meta(inode=ino)
            except Exception:  # noqa: BLE001
                continue
            meta = getattr(f.info, "meta", None)
            if meta is None:
                continue
            if not (int(getattr(meta, "flags", 0)) & pytsk3.TSK_FS_META_FLAG_UNALLOC):
                continue
            if getattr(meta, "type", None) != pytsk3.TSK_FS_META_TYPE_REG:
                continue
            size = int(getattr(meta, "size", 0) or 0)
            if size <= 0:
                continue
            name = self._unallocated_name(f) or f"inode_{ino}"
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if extensions and ext not in extensions:
                continue
            out.append({
                "name": name, "path": f"/$OrphanFiles/{name}", "inode": ino,
                "size": size, "deleted": True, "type": "file",
                "mtime": int(getattr(meta, "mtime", 0) or 0),
                "ctime": int(getattr(meta, "ctime", 0) or 0),
                "source": "mft_unallocated",
                "note": "MFT 未分配记录（名字可能已丢失）",
            })
        return out

    @staticmethod
    def _unallocated_name(file_obj) -> str | None:
        """尝试从 $FILE_NAME 属性里取文件名（解析失败返回 None）。"""
        try:
            for attr in file_obj:
                info = getattr(attr, "info", None)
                if info is None:
                    continue
                # NTFS $FILE_NAME 属性类型号 0x30（不同 pytsk3 版本常量命名不同，用数字兜底）
                if int(getattr(info, "type", 0)) != 0x30:
                    continue
                data = attr.read_random(0, info.size)
                if not data or len(data) < 0x42:
                    continue
                name_len = data[0x40]
                raw = data[0x42:0x42 + name_len * 2]
                if raw:
                    return raw.decode("utf-16-le", "replace")
        except Exception:  # noqa: BLE001
            return None
        return None

    # ------------------------------------------------------------------ 公开 API
    def list_entries(self, include_deleted: bool = True, extensions=None,
                     limit: int | None = None, scan_unallocated: bool = False,
                     include_system: bool = False,
                     errors: list | None = None) -> list[dict]:
        require_pytsk3()
        errors = errors if errors is not None else []
        result: list[dict] = []
        try:
            root = self.fs.open_dir(path="/")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"打开根目录失败：{exc}")
            return result
        try:
            for item in self._walk(root, "/", include_deleted, extensions, errors,
                                   include_system=include_system):
                result.append(item)
                if limit and len(result) >= limit:
                    break
        finally:
            close = getattr(root, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        if scan_unallocated:
            remain = (limit - len(result)) if limit else None
            orphan = self._scan_unallocated(extensions, errors)
            if remain is not None:
                orphan = orphan[:remain]
            result.extend(orphan)
        return result

    def read_entry(self, item: dict) -> dict:
        """读取一个条目的内容；返回 ``{"data": bytes|None, "recovered_full": bool, "note": str}``。"""
        require_pytsk3()
        result = {"data": None, "recovered_full": False, "note": ""}
        if not item.get("inode"):
            result["note"] = item.get("note") or "没有可用的 MFT 记录"
            return result
        try:
            f = self.fs.open_meta(inode=item["inode"])
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"打开 MFT 记录失败：{exc}"
            return result
        size = int(item.get("size") or 0)
        if size <= 0:
            result["note"] = "大小为 0（删除后 MFT 可能已释放长度信息）"
            return result
        data = bytearray()
        step = 1 << 20
        try:
            offset = 0
            while offset < size:
                chunk = f.read_random(offset, min(step, size - offset))
                if not chunk:
                    break
                data += chunk
                offset += len(chunk)
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"读取内容失败（data run 可能已被清除）：{exc}"
            result["data"] = bytes(data) if data else None
            return result
        result["data"] = bytes(data)
        if len(data) >= size:
            result["recovered_full"] = True
            result["note"] = "完整读取"
        else:
            result["note"] = f"部分恢复：期望 {size} 字节，实际 {len(data)} 字节"
        return result

    def recover(self, outdir: str, include_deleted: bool = True, extensions=None,
                limit: int | None = None, scan_unallocated: bool = True,
                include_system: bool = False,
                progress=None) -> dict:
        """扫描并恢复文件到 *outdir*。

        :param include_deleted: True = 只恢复删除文件；False = 恢复全部文件
        :param scan_unallocated: 是否额外扫描 MFT 未分配记录（孤儿删除文件）
        """
        require_pytsk3()
        os.makedirs(outdir, exist_ok=True)
        errors: list = []
        items = self.list_entries(include_deleted=True, extensions=extensions,
                                  scan_unallocated=scan_unallocated,
                                  include_system=include_system, errors=errors)
        if include_deleted:
            items = [i for i in items if i["deleted"]]
        if limit:
            items = items[:limit]
        files = []
        total = len(items)
        for idx, item in enumerate(items):
            if progress:
                progress(idx, total)
            rec = self.read_entry(item)
            entry = {
                "name": item["name"], "inode": item["inode"], "size": item["size"],
                "deleted": item["deleted"], "recovered": False,
                "recovered_full": bool(rec["recovered_full"]), "note": rec["note"],
                "source": item.get("source"), "path": None,
            }
            if rec["data"]:
                out = _unique_path(outdir, _safe_name(item["name"]))
                try:
                    with open(out, "wb") as fh:
                        fh.write(rec["data"])
                    entry.update(recovered=True, path=out, recovered_size=len(rec["data"]))
                except OSError as exc:
                    errors.append(f"写出 {item['name']} 失败：{exc}")
            files.append(entry)
        if progress:
            progress(total, total)
        return {"count": len(files), "files": files, "errors": errors,
                "outdir": os.path.abspath(outdir),
                "recovered_count": sum(1 for f in files if f["recovered"])}

    def close(self) -> None:
        for obj in (self.fs, self.img):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.fs = None
        self.img = None

    def __enter__(self) -> "NtfsVolume":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def open_volume(path: str, offset: int | None = None) -> NtfsVolume:
    """打开 NTFS 镜像/设备。"""
    return NtfsVolume(path, offset=offset)


def list_partitions(path: str) -> list[dict]:
    """枚举整盘分区表（只读），便于确认 NTFS 分区偏移。"""
    require_pytsk3()
    img = pytsk3.Img_Info(path)
    volume = None
    try:
        volume = pytsk3.Volume_Info(img)
        out = []
        for part in volume:
            out.append({
                "index": int(part.addr),
                "start_sector": int(part.start),
                "sectors": int(part.len),
                "offset_bytes": int(part.start) * 512,
                "description": _decode_name(part.desc) if part.desc else "",
            })
        return out
    finally:
        for obj in (volume, img):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
