# FileDoctor 项目规范（v0.1）

Windows 下的文件恢复/取证小工具，**纯 Python 3.8+ 标准库**，命令行使用，不依赖第三方包。

## 目录结构

```
file-doctor\
  filetool.py          # 命令行入口（Lead 负责）
  fdlib/
    __init__.py
    device.py          # 原始设备/镜像读取（Lead 负责）
    ziprepair.py       # ZIP/DOCX 修复与重建（Lead 负责）
    carve.py           # 特征码扫描/雕刻恢复（carve-dev 负责）
    exfat.py           # exFAT 文件系统解析 + 删除文件恢复（exfat-dev 负责）
    capacity.py        # U 盘容量/写入完整性检测（Lead 负责）
  tests/
    test_ziprepair.py
    test_carve.py
    test_exfat.py
    test_capacity.py
    gen_exfat_image.py  # 测试用 exFAT 镜像生成器（exfat-dev 负责）
  README.md
```

## 通用约定

- 源码 UTF-8（中文注释、中文 docstring 均可）；每个模块自带 `if __name__ == '__main__'` 的小自测可选。
- 只使用标准库；不写网络访问；不修改 `Q:\`、`C:\Windows` 等系统位置。
- 所有模块只写自己名下的文件，不修改别人文件（并行开发，避免冲突）。
- 对外函数返回 **可 json 序列化的 dict / list**（路径用 str），异常不要静默吞掉；无法恢复时返回带 `error`/`note` 字段的结果。
- 输出文件时自动创建目录，重名自动加 `-1`、`-2` 后缀。
- 大文件/设备必须 **流式分块处理**，不得一次性读入内存（镜像 > 4GB 要能跑）。

## fdlib/device.py（已完成，作为依赖）

```python
class DeviceReader:
    def __init__(self, path: str, chunk_size: int = 4 * 1024 * 1024)
    # path 可以是：
    #   普通镜像文件路径，如 F:\img\usb.img
    #   Windows 原始设备路径，如 r'\\.\G:' 或 r'\\.\PhysicalDrive3'
    @property
    def is_raw(self) -> bool
    @property
    def size(self) -> int | None          # 未知（如裸设备且取不到长度）时为 None
    def read_at(self, offset: int, length: int) -> bytes   # 越界自动截断；裸设备自动 512 对齐
    def iter_chunks(self, start: int = 0, length: int | None = None,
                    overlap: int = 0) -> Iterator[tuple[int, bytes]]
    # 产出 (绝对偏移, 数据块)，块大小约 chunk_size，overlap 为相邻块重叠字节数
    def close(self)
    def __enter__(self); def __exit__(...)
```

## fdlib/carve.py 接口（carve-dev）

```python
def carve_image(path: str, outdir: str,
                types: list[str] | None = None,   # None = 默认全部: docx/zip/png/jpg/pdf/exe
                text: bool = False,               # 额外做文本碎片雕刻
                start: int = 0, length: int | None = None,
                min_size: int = 1024,
                max_files: int = 500,
                progress=None) -> dict
```
返回示例（必须包含这些字段）：
```python
{
  "scanned_bytes": 123456789,
  "files": [
     {"type": "docx", "path": "outdir/carved_0001.docx", "offset": 22593536,
      "size": 30943, "note": "EOCD+central-dir 完整重建"},
     ...
  ],
  "texts": [ {"path": ".../text_0001.txt", "offset": ..., "size": ...} ],  # text=True 时
  "by_type": {"docx": 1, "png": 2, ...},
  "errors": [ "..." ]
}
```
要求：
- 支持的类型：
  - `zip`：`PK\x03\x04`。完整包优先：在附近找 `PK\x05\x06` EOCD，推算压缩包起始（`zip_start = eocd_pos - cd_size - cd_offset`），交给 `fdlib.ziprepair.extract_zip_bytes()` 校验/解包后重建为 .zip；
  - `docx`：zip 中和 `word/document.xml` 或 `[Content_Types].xml` 的完整包 → 用 `fdlib.ziprepair.rebuild_zip()` 输出 .docx；若无 EOCD 但有局部文件头，用 `fdlib.ziprepair.salvage_zip_bytes()` 抢救（CRC 校验通过的条目）并输出，note 说明是“残缺重建”。
  - `png`：`\x89PNG\r\n\x1a\n` 起，逐 chunk 解析到 IEND，完整才输出；
  - `jpg`：`FF D8 FF` 起，扫描到 `FF D9`，限制最大 64MB，做基本合理性校验；
  - `pdf`：`%PDF-` 起，向后找 `%%EOF`（最多 256MB），输出完整区间；
  - `exe`：`MZ` + PE 头，依据 section 表算文件长度，>=min_size 才输出（可选类型，默认包含）。
- 去重：同一段数据只输出一次；已作为完整 zip 输出的区域不再重复雕刻其内部文件。
- 必须流式：用 `DeviceReader.iter_chunks`，跨块签名用 overlap（如 1MB）处理。
- 文本碎片（text=True）：扫描连续可打印文本（ASCII 可打印 + UTF-8 中文 + GBK 中文混排），单段 >= 256 字节输出为 .txt；遇到二进制噪音切段。此功能用于从坏盘里捞回记事本类文件。
- 进度回调：每处理约 256MB 调一次 `progress(scanned_bytes, total_bytes)`（可为 None）。
- **测试**：`tests/test_carve.py` 生成一个合成镜像（随机数据中间嵌入 1 个完整 docx、1 个 PNG、1 段中文文本），验证三种都能被恢复出来、内容一致；跑通后报告结果。

## fdlib/exfat.py 接口（exfat-dev）

```python
def open_volume(path: str, offset: int = 0) -> "ExfatVolume"   # path 为镜像或 r'\\.\G:'；offset 为分区起始字节
class ExfatVolume:
    info: dict            # 至少包含: bytes_per_sector, sectors_per_cluster, cluster_size,
                          # fat_offset_bytes, heap_offset_bytes, cluster_count, root_cluster,
                          # volume_label, volume_serial, fs_revision
    def list_entries(self, include_deleted: bool = True) -> list["Entry"]
    def read_entry_data(self, e: "Entry", max_bytes: int | None = None) -> tuple[bytes, bool]
        # 返回 (数据, 是否完整/可信)；删除文件 FAT 链可能已清零 → 按连续簇回退读取并标记不可信
    def recover(self, outdir: str, include_deleted: bool = True) -> dict
        # 输出恢复到 outdir（目录结构保留），返回 {"files":[{name,size,path,deleted,recovered_full,...}], "count": n, "errors":[...]}
class Entry:
    name: str; size: int; is_dir: bool; deleted: bool
    first_cluster: int; attributes: int
    mtime: int | None; ctime: int | None
```
要求：
- 解析 exFAT VBR（含 bytes-per-sector-shift / sectors-per-cluster-shift / FAT 偏移 / ClusterHeap 偏移 / 根目录簇等），支持 offset != 0。
- 目录项解析：File entry（0x85/0x05）+ Stream extension（0xC0/0x40）+ File name（0xC1/0x41），UTF-16LE 文件名；识别删除（0x80 位清零）。
- 文件数据：正常文件沿 FAT 链读；删除文件若 FAT 链无效则按连续簇回退（标记 `recovered_full=False` 并在结果中说明）。
- 不要求写 exFAT/NTFS；只读。
- **测试**：`tests/gen_exfat_image.py` 手工构造最小合法 exFAT 镜像（512B 扇区、4KB 簇、1 FAT、根目录 1 簇，含 1 个正常文件 + 1 个删除文件），`tests/test_exfat.py` 验证 `info` 字段、`list_entries` 数量/名字/删除标记、`recover` 输出的文件内容逐字节一致。**注意**：exFAT 有 boot checksum、SetChecksum 等字段，生成器可按规范写正确值（校验值我们的解析器不强制校验，但请按规范生成以便测试可信）。

## CLI 约定（Lead 负责，供参考）

```
python filetool.py probe <image|device>                          # 识别文件系统/扇区信息
python filetool.py repair <broken.docx> -o out.docx [--template good.docx]
python filetool.py carve <image|device> -o outdir [--types docx,zip,png] [--text] [--start N] [--length N]
python filetool.py exfat <image|device> -o outdir [--no-deleted]
python filetool.py capacity <dir-or-device> [--write-gb N]
```

## 交付标准

每个模块：能 import、有 tests、`python tests/test_xxx.py` 退出码 0；在报告中说明做了什么、怎么测试、还有什么限制。
