# FileDoctor —— 自己动手做的文件恢复工具箱（v0.3.1）

![version](https://img.shields.io/badge/version-v0.3.1-blue)
[![release](https://img.shields.io/github/v/release/bugego15/FileDoctor)](https://github.com/bugego15/FileDoctor/releases)
[![tests](https://img.shields.io/badge/tests-9%2F9%20pass-brightgreen)](.)

> 起因：一次真实的数据损坏事故——文档复制到 U 盘后打开全是乱码，排查发现写入的数据没有真正落盘，
> 未写入区域读到的是盘上旧文件的残留数据。于是把排查/恢复过程工具化，做成这个纯 Python 标准库的恢复工具箱。

也提供 **PySide6 图形界面**（`python gui.py`，可打包成 exe），适合非命令行用户。

纯 Python 3.8+，核心功能无第三方依赖；NTFS 恢复需可选安装 `pytsk3`，GUI 需 `PySide6`。
裸设备读取（`\\.\G:`）在 Windows 上通常无需管理员；
读取 `\\.\PhysicalDriveN` 一般需要管理员权限。

## 功能

| 子命令 | 作用 | 典型场景 |
| --- | --- | --- |
| `probe` | 读取引导扇区识别 NTFS/exFAT/FAT32/MBR，并对比 Windows 的挂载结果 | 判断 U 盘是否“原始扇区与挂载文件系统不一致”（扩容盘/固件异常线索） |
| `repair` | 修复损坏的 ZIP/DOCX：扫描局部文件头、CRC 校验抢救条目、重建 ZIP；DOCX 可用同源健康文件补齐 `[Content_Types].xml`/关系文件 | 保存中断、断电、复制损坏后打不开的 Office 文档 |
| `carve` | 按特征码从镜像/原始设备雕刻恢复 docx/zip/png/jpg/pdf/exe，可选文本碎片恢复 | 文件系统损坏、分区被格式化、误删后的数据打捞 |
| `exfat` | 解析 exFAT，列出/恢复文件（含已删除条目，连续簇回退） | U 盘/存储卡误删文件恢复 |
| `ntfs` | 解析 NTFS，列出/恢复文件（含已删除条目；需要 `pytsk3`） | Windows NTFS 磁盘/U 盘误删文件恢复 |
| `capacity` | 按偏移生成伪随机数据写入再读回校验，检测扩容盘/坏盘并估算真实容量 | 新买的 U 盘/存储卡验真 |

## 快速开始

```powershell
cd file-doctor

# 1) 识别 U 盘（G:）：原始扇区 vs Windows 挂载
python filetool.py probe \\.\G:

# 2) 修复损坏的 docx（有同源健康文件时强烈建议给 --template，可补齐结构并自动重建图片关系）
python filetool.py repair broken.docx -o fixed.docx --template good.docx

# 3) 从 U 盘原始设备按特征码雕刻文件（只读，不写盘）
python filetool.py carve \\.\G: -o recovered --types docx,zip,png,jpg,pdf --text
python filetool.py carve usb.img -o recovered --text --start 0 --length 268435456   # 只扫前 256MB 更快

# 4) 从损坏的 exFAT 镜像恢复文件（含删除文件）
python filetool.py exfat usb.img -o out

# 5) U 盘验真：先跑默认 256MB 体检；要测标称容量需加 --write-gb 与 --yes（会写满磁盘）
python filetool.py capacity G:\
python filetool.py capacity G:\ --write-gb 58 --yes
```

## 本机已实测的案例

- `probe \\.\G:`：识别出 NTFS 引导扇区、卷序列号、总容量，并与 Windows 挂载信息比对。
- `repair broken.docx -o fixed.docx --template good.docx`：
  - 从 35,903 字节的真实损坏文件里抢救出 **28 个完好条目**（正文 `word/document.xml` 99,633 字节、19 张图片等）；
  - 自动补齐 `[Content_Types].xml`、重建 `word/_rels/document.xml.rels`，**19 张图片关系全部正确映射**；
  - 修复后文件可正常打开（LibreOffice 渲染校验通过）。
- `capacity F:\temp\cptest`：256 MB 写入+读回校验通过（约 145 MB/s 写、288 MB/s 读）。
- `carve` 对残缺 docx（无中央目录）的抢救：真实损坏样本可用“残缺重建”路径恢复出可打开的 docx。
- `ntfs` 对真实 64MB NTFS 镜像：列出用户文件、恢复 `/test/1.txt`；模拟删除其 MFT 记录后，通过
  MFT 未分配扫描完整恢复（286,671 字节，SHA-256 一致）。
- **U 盘实机测试（58 GB NTFS）**：写入 8 个样本 → 删除 4 个 → 扫描并恢复 **4/4（保留真实文件名，SHA-256 一致）**；
  损坏 DOCX 修复通过；256 MB 容量/写入校验通过；原始设备雕刻出 3 个完整 DOCX。
  详见 [docs/实机测试记录_U盘.md](<docs/实机测试记录_U盘.md>)。
- GUI：`gui.py --self-test` 离屏自检通过（5 个标签页、PySide6 6.12、pytsk3 可用）；
  PyInstaller 打包出 `dist\FileDoctor\FileDoctor.exe`。

## NTFS 删除恢复（v0.2 预览，可选依赖 pytsk3）

引擎选用开源 [The Sleuth Kit](https://github.com/sleuthkit/sleuthkit) 的 Python 绑定
**`pytsk3`**（Apache-2.0，PyPI 有 Windows 10/11 预编译 wheel，`python -m pip install pytsk3` 即可，无需编译）。

```powershell
python filetool.py ntfs \\.\G: --list                             # 列出删除文件（名字/inode/大小/时间）
python filetool.py ntfs \\.\G: -o D:\recovered --filter docx,pdf # 恢复指定类型
python filetool.py ntfs \\.\G: -o D:\recovered --all             # 连未删除文件一起导出
python filetool.py ntfs \\.\PhysicalDrive2 --partitions          # 枚举分区，找 NTFS 偏移（需管理员）
python filetool.py ntfs usb.img --offset 1048576 --list            # 镜像文件 + 手动 offset
```

限制与注意：

- 删除文件能否读出内容取决于 MFT 里 data run 是否还在：读不全时结果会标 `recovered_full=false`，
  此时应改用 `carve` 按特征码雕刻；
- 读物理盘/固定卷需要**管理员权限**；可移动盘（如 `\\.\G:`）通常只读即可；读不出来可先做整盘
  镜像再用镜像文件扫；
- SSD/NVMe 删除文件可能已被 TRIM 物理擦除，恢复率远低于 HDD/移动盘；
- **已在真实 NTFS 镜像上联调通过**：用公开的 64MB NTFS 3.x 测试镜像（dfir_ntfs `ntfs3.raw`）
  验证了文件枚举、普通文件恢复；并把 inode 28（`/test/1.txt`，286671 字节）的 MFT 记录模拟为
  “已删除”后，通过 `--inode-scan` 找到并**完整恢复，SHA-256 与原文一致**（见 `tests/test_ntfs.py`）；
- 物理盘/整盘 VHD 的权限场景（管理员）仍建议按 `tools/make_ntfs_test_vhd.ps1` 跑一遍真实误删测试。

> 完整开源方案调研（TSK/Autopsy/TestDisk/PhotoRec/KVC FileRecoveryTool/dfir_ntfs/pytsk3 等对比、
> 许可证、推荐路线）：见 [docs/NTFS恢复_开源方案调研.md](<docs/NTFS恢复_开源方案调研.md>)。

## 一键演示

```powershell
python examples\demo.py
```

自动完成 9 项检查并输出 `examples/out/DEMO_REPORT.md`：

1. 生成 8MB 模拟“坏 U 盘”镜像（残缺 docx + PNG + 中文文本 + 随机垃圾）；
2. `probe` 识别镜像；
3. `repair` 修复残缺 docx（重建为可打开的 docx）；
4. `carve` 从镜像雕刻恢复 docx / PNG / 文本碎片；
5. 生成 exFAT 镜像并恢复其中“已删除文件”；
6. `capacity` 写入/读回完整性体检。

## 测试

```powershell
python tests\run_all.py
```

当前 9 个测试文件全部通过（退出码 0）：

| 测试 | 覆盖内容 |
| --- | --- |
| `test_ziprepair.py` | 制造“中央目录丢失 + 缺结构件”的 docx，用模板修复；校验正文/图片/关系文件；真实损坏样本抢救 |
| `test_carve.py` | 6MB 合成镜像（跨 4MB 块）：完整 docx、无 EOCD 残缺 docx、PNG、PDF、PE、UTF-8/GBK 中文文本；`start/length` 子区间；真实样本 |
| `test_exfat.py` | 手写最小合法 exFAT 镜像（含不连续 FAT 链、已删除文件），**120 条断言**：VBR/FAT/位图/SetChecksum 独立重算、目录项、删除恢复、子目录、offset≠0 |
| `test_probe.py` | 合成 NTFS/exFAT/FAT32/MBR 引导扇区识别 + CLI 输出 |
| `test_capacity.py` | 16MB 写入/读回校验、清理、非法目标 |
| `test_device.py` | 镜像读取、越界截断、分块与重叠拼接 |
| `test_ntfs_guard.py` | NTFS 模块在缺少 pytsk3 时的错误提示（有依赖时做接口检查） |
| `test_ntfs.py` | 真实 64MB NTFS 镜像：列举/恢复普通文件、模拟删除 MFT 记录后完整恢复并 SHA-256 校验、非 NTFS 报错、CLI 端到端 |
| `test_gui.py` | GUI 离屏自检（PySide6 缺失时自动跳过） |

## 原理（简）

- **ZIP/DOCX 抢救**：ZIP 中央目录在文件尾部，保存中断时最容易丢；局部文件头（`PK\x03\x04`）分散在
  前面，逐个解析、按 header 里的 CRC 校验解压，能救回绝大部分条目。DOCX 还缺 `[Content_Types].xml`、
  `word/_rels/document.xml.rels` 两类“结构件”，可由同源健康文件借来；图片关系用**图片内容哈希 +
  文档中出现顺序**对齐重建。
- **特征码雕刻**：文件系统损坏时，直接在扇区流里找已知文件头/尾（ZIP、PNG、JPEG、PDF、PE），
  校验通过后原样导出；完整 ZIP 用 EOCD 反推起点重建，残缺 ZIP 用 CRC 抢救；文本碎片按可打印
  字符/中文编码连续性切段（UTF-8/GBK 混排，≥256 字节）。
- **exFAT 恢复**：解析 VBR/FAT/目录项（含删除标记），按 FAT 链读文件；删除文件的链若已被清零，
  按“连续簇”假设回退读取并标记 `recovered_full=False`。
- **扩容盘检测**：真实容量小于标称容量的 U 盘，写到真实容量后数据会回卷覆盖；测试块按偏移生成
  不同伪随机内容，读回比对即可定位第一个坏的偏移，估算真实容量。

## 目录结构

```
file-doctor/
  filetool.py           # CLI 入口
  gui.py                # PySide6 图形界面（可 PyInstaller 打包）
  fdlib/
    device.py           # 镜像/裸设备统一读取（512B 对齐、分块、IOCTL 查询容量）
    ziprepair.py        # ZIP/DOCX 抢救与重建
    carve.py            # 特征码雕刻
    exfat.py            # exFAT 解析/删除文件恢复
    ntfs.py            # NTFS 解析/删除文件恢复（pytsk3，可选依赖）
    capacity.py         # 容量/写入完整性检测
  tests/
    test_*.py           # 各模块测试
    gen_exfat_image.py  # 手写最小 exFAT 测试镜像生成器
    run_all.py          # 一键跑全部测试
    assets/             # 测试镜像（下载生成，不入库/不进 zip）
  examples/
    demo.py             # 一键演示（9 项检查）
  tools/
    make_ntfs_test_vhd.ps1  # 生成 NTFS 测试 VHD（管理员，用于验收真实误删）
    fetch_test_assets.py    # 下载/生成 NTFS 测试素材
  docs/
    NTFS恢复_开源方案调研.md # 开源工具/库调研与落地建议
  requirements-optional.txt  # 可选依赖（pytsk3 / PySide6 / PyInstaller）
  SPEC.md               # 模块接口与开发规范
```

## 已知限制（v0.1）

- 删除文件恢复：**exFAT 已支持**；**NTFS 已支持**（pytsk3，已在真实 NTFS 镜像上联调；物理盘/VHD 真实误删待管理员权限验收）；FAT32 尚未实现。
- 删除文件的目录项还在时，会回查 MFT 记录合并出**真实文件名**；只有目录项完全丢失的孤儿记录才显示 `inode_<N>`（data run 还在时可恢复内容，读不全时标 `recovered_full=false`，可转 `carve` 雕刻）。
- `carve` 不支持 ZIP64；完整 ZIP 上限 256MB、salvage 窗口 64MB、PNG 128MB、JPG 64MB、PDF 256MB、EXE 128MB，超限跳过并记 `errors`。
- JPEG 为基本合理性校验（marker + SOF + EOI），progressive JPEG 理论上可能误判；EXE 长度按 section 表推算（不含 overlay）。
- 文本碎片固定 ≥256B 才输出；单段超 512MB 会截断。对超大设备 `--text` 较慢，建议配合 `--start/--length`。
- exFAT 的删除文件按连续簇回退，文件原本碎片化时恢复可能不完整（`recovered_full=False` 已标注）；
  仅支持单 FAT 读取（TexFAT 按 VolumeFlags 选活动 FAT），不强制校验 Boot/SetChecksum。
- `capacity` 只写普通目录，不直接写裸设备；写满整盘测试前请确认盘上没有需要的数据。
- 裸设备读取的权限/行为与 Windows 版本、U 盘固件有关；读 `PhysicalDrive` 需管理员。

## 安全提示

1. 发现数据损坏后**立即停止往该盘写任何东西**；能先做整盘镜像（`dd`/WinHex/`ddrescue`）最好。
2. 恢复出的文件不要写回原盘，先存到其他磁盘。
3. `capacity --write-gb` 会占满磁盘空间，只对空盘/可清空盘使用。

## 路线图

- [x] NTFS 删除文件恢复（基于 pytsk3 / The Sleuth Kit，已在真实 NTFS 镜像上验证）
- [x] PySide6 GUI + PyInstaller 打包 exe
- [ ] FAT32 删除文件恢复（解析 FAT 目录项）
- [ ] NTFS USN Journal / $LogFile 深度恢复、NTFS 压缩/稀疏文件/ADS
- [ ] 分区表（MBR/GPT）自动解析与分区偏移探测（`probe` 已能显示 MBR 分区表）
- [ ] 已知文件类型的“文件尾”校验（提高 carve 精度）
- [ ] 恢复报告导出 HTML/CSV
