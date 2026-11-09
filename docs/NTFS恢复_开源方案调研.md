# NTFS 恢复 & 损坏文件修复：开源方案调研与落地建议

日期：2026-11-09　适用平台：Windows 10 / 11（x64）
需求：先支持 **NTFS**（删除文件恢复、乱码/损坏文件的内容恢复），找现成开源工具或可复用的开源库，决定我们自己的工具怎么站到它们肩膀上。

---

## 0. 结论速览

| 问题 | 推荐做法 |
| --- | --- |
| Windows 10/11 上做 NTFS 删除恢复 | **Python + `pytsk3`**（The Sleuth Kit 的 Python 绑定，Apache 2.0，PyPI 有 win_amd64 预编译 wheel，无需编译）→ 集成进现成的 FileDoctor，加 `ntfs` 子命令；或直接用/参考 MIT 许可的原生 Windows 工具 **KVC FileRecoveryTool** |
| 只想“立刻恢复文件”不想开发 | **TestDisk**（NTFS 取消删除） + **PhotoRec**（内容雕刻），或 GUI 的 **Autopsy** / **KVC FileRecoveryTool** |
| 损坏的 docx/zip “乱码/打不开”，但内容还在 | 容器修复：**我们自己已实现的 ZIP 局部头重建** 已能处理；可再叠加 **intactfile-zip-repair**（MIT，TS）思路、**Open XML SDK**（MIT，.NET）或 **LibreOffice**（MPL-2.0）headless 重存 |
| 文本“乱码”（编码错乱） | 不是恢复问题：**charset-normalizer / uchardet / iconv** 做编码检测与转码 |
| 内容被覆盖/写坏、只剩残片 | **特征码雕刻**（PhotoRec / TSK `blkls` / 我们的 `carve`），能救多少算多少 |

一句话：**NTFS 删除恢复用 pytsk3（或现成工具），文件“修内容”用 ZIP/OOXML 修复层 + LibreOffice/OpenXmlSdk，文本乱码用编码检测；不要从零写 NTFS 解析器。**

---

## 1. 先把“乱码/恢复”拆成三类（对应不同技术）

| # | 现象 | 本质 | 技术手段 |
| --- | --- | --- | --- |
| ① | 记事本打开中文是乱码，但文件没坏 | 编码误判（UTF-8/GBK/UTF-16/BOM） | 编码检测 + 转码：`charset-normalizer`(MIT)、`uchardet`(MPL1.1)、`chardet`(LGPL)、`iconv` |
| ② | docx/zip 打不开，或打开内容错乱、图片丢失 | ZIP 中央目录/尾部损坏，条目数据其实还在；或缺 `[Content_Types].xml`/关系文件 | 扫局部文件头(`PK\x03\x04`)+CRC 校验重建 ZIP；用同源健康文件补结构件；LibreOffice/Open XML SDK 再校验 |
| ③ | 文件被删除/覆盖/写坏，原路径没有内容 | 数据在 NTFS 元数据里已释放、或只剩未分配簇 | 读 MFT 删除记录恢复（TSK/pytsk3、TestDisk、KVC）；元数据没了就特征码雕刻（PhotoRec/我们的 carve） |

> 对照三类问题：②类（容器损坏但正文/图片数据完好）可以修复；③类（数据根本没写入，读到的只是旧数据残留）则无法从该文件“解码”出原文。

---

## 2. 现成开源工具对比（可直接用）

| 工具 | 平台 | 许可证 | NTFS 删除恢复 | 内容雕刻 | GUI | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| [The Sleuth Kit (TSK)](https://github.com/sleuthkit/sleuthkit) `fls`/`icat`/`tsk_recover` | Win/Linux/mac | 混合：fstools 为 IBM + CPL，`srch_strings`/`fiwalk` 为 GPL，库用 MIT/BSD 组件 | ✅ 强 | ✅ `blkls` + 外部工具 | ❌ CLI | 事实标准，Autopsy 的引擎 |
| [Autopsy](https://github.com/sleuthkit/autopsy) | Windows 安装包/Java | Apache-2.0 | ✅ | ✅（Scalpel） | ✅ | 完整取证平台，重；适合做“专业版”能力参考 |
| [TestDisk](https://www.cgsecurity.org/wiki/TestDisk) | Windows/Linux/mac | GPLv2+ | ✅ FAT/exFAT/NTFS/ext2 取消删除 | ❌（配 PhotoRec） | TUI | 还能修分区表/引导扇区/MFT 镜像 |
| [PhotoRec](https://www.cgsecurity.org/) | Windows 等 | GPLv2+ | ❌（不看文件系统） | ✅ 300+ 格式 | TUI | 元数据全没时的最后手段 |
| [KVC FileRecoveryTool (wesmar)](https://github.com/wesmar/FileRecoveryTool) | **原生 Windows 10/11 x64/x86** | **MIT** | ✅ MFT + USN 记录 | ✅ 空闲空间雕刻 + RAW 全盘扫描 | ✅ Win32 GUI + CLI | 需管理员；C++ 实现、有 DOCX/PDF/ZIP 识别；2026 新项目，代码可参考/复用 |
| ntfs-3g / `ntfsundelete` | Linux 为主 | GPLv2+ | ✅ | ✅ `ntfscat`/`ntfscp` | ❌ | Windows 上一般走 WSL/Cygwin |
| [RecuperaBit](https://github.com/Topper-crypto/RecuperaBit) | Python | GPLv3 | ✅（重建损坏 NTFS 文件系统） | 部分 | ❌ | 老项目，适合参考 $MFT 重建 |
| [dfir_ntfs](https://github.com/msuhanov/dfir_ntfs) | Python 3 | GPLv3 | 解析 $MFT/$UsnJrnl/$LogFile（取证分析向） | ❌ | ❌ | 纯 Python、解析能力深，但“恢复文件”不是它的定位 |
| [pytsk3_data-recovery](https://github.com/n3r0-b1n4ry/pytsk3_data-recovery) | Win/Linux/mac | **MIT** | ✅（pytsk3 扫 MFT + 碎片重组） | ✅ 15+ 签名，3 层策略 | ❌ CLI | 和我们要做的功能最接近，代码可直接参考 |
| Foremost / binwalk / pyFileCarving | 跨平台 | GPL/MIT 各异 | ❌ | ✅ | ❌ | 纯雕刻参考实现 |

---

## 3. 可复用的开源库（Windows 10/11 上能直接跑）

| 库 | 语言/形态 | 许可证 | Windows 10/11 | 能做什么 | 备注 |
| --- | --- | --- | --- | --- | --- |
| **pytsk3** | Python 扩展（封装 libtsk） | **Apache-2.0** | ✅ PyPI 有 `win_amd64/win32/arm64` 预编译 wheel（cp310–cp314） | NTFS/exFAT/FAT 等文件系统枚举、读删除文件内容、`icat` | 速度/成熟度最好；pip 一条命令；底层 libtsk 是 CPL/IBM 许可，分发时附声明即可 |
| The Sleuth Kit C 库 | C | 见上 | ✅（官方 win32 工程/可自编译） | 同上的 C 接口 | 想 C++/C# P/Invoke 时用 |
| [libfsntfs](https://github.com/libyal/libfsntfs) | C（libyal） | LGPLv3+ | 需自行构建 | 只读解析 NTFS | Windows 构建麻烦，不优先 |
| [DiscUtils](https://github.com/DiscUtils/DiscUtils) | .NET | MIT | ✅ | NTFS/FAT/exFAT 读写、访问 $MFT | 走 C#/WinUI3 时合适；删除恢复要自己解析 MFT 记录 |
| [Open XML SDK](https://github.com/dotnet/Open-XML-SDK) | .NET | MIT | ✅ | docx/xlsx/pptx 包结构校验、重建、读取 | 修 OOXML 的“官方”库 |
| [intactfile-zip-repair](https://www.npmjs.com/package/intactfile-zip-repair) | TypeScript | **MIT** | ✅（Node/浏览器） | 忽略损坏的中央目录，按局部条目修复 ZIP/OOXML | 正是“docx 尾部损坏”的修复引擎，逻辑可移植到 Python |
| [LibreOffice](https://www.libreoffice.org/) headless | C++/Java | MPL-2.0 | ✅ | 打开损坏 OOXML 再另存（修复率不错） | 可作为修复链的第二道保险 |
| python `zipfile`+`zlib` | 标准库 | PSF | ✅ | 自己扫局部头/CRC 重建（FileDoctor `repair` 已实现） | 无第三方依赖 |
| charset-normalizer / uchardet / chardet | Python/C++ | MIT / MPL1.1 / LGPL | ✅ | 编码检测与乱码转换 | 解决第 ① 类“乱码” |
| olefile / oletools | Python | BSD | ✅ | 老 `.doc/.xls`（OLE2）容器解析 | 旧格式文档修复的起点 |
| PhotoRec / binwalk | C/Python | GPLv2+ / MIT | ✅ | 特征码雕刻 | 第 ③ 类最后手段 |

> 许可提醒：`pytsk3` 本身 Apache-2.0，但打包了 CPL/IBM 许可的 libtsk 代码，发布时保留其版权/许可声明即可；TestDisk、PhotoRec、dfir_ntfs、RecuperaBit、ntfs-3g 是 GPL，若你的工具要闭源分发，不要直接链/嵌它们，改用 TSK/CPL 或 MIT 组件（如 KVC、pytsk3 的 TSK 路线需法务确认，或直接把项目也开源为 GPL）。

---

## 4. Windows 10/11 落地要点（NTFS 删除恢复）

1. **引擎选择**：`pytsk3` 一把梭——
   ```python
   import pytsk3
   img = pytsk3.Img_Info(r"\\.\G:")          # 镜像文件；物理盘/卷一般需要管理员
   fs  = pytsk3.FS_Info(img, offset=offset)   # offset = NTFS 分区起始字节
   d   = fs.open_dir(path="/")
   for e in d:
       meta = e.info.meta
       deleted = bool(meta.flags & pytsk3.TSK_FS_META_FLAG_UNALLOC)
       size = meta.size; inode = meta.addr
   f = fs.open_meta(inode=inode)              # 读删除文件内容
   data = f.read_random(0, size)              # 若 runlist 被清零，可能读不出/读错（此时转雕刻）
   ```
2. **分区定位**：物理盘用 `pytsk3.Volume_Info(img)` 枚举分区，挑 NTFS 起始偏移；卷（`\\.\G:`）直接 offset=0 试。
3. **删除判定**：`TSK_FS_META_FLAG_UNALLOC`；文件名从目录项拿（删除后仍可能保留在目录项/文件名属性里）。
4. **内容可信度**：MFT 里的 data run 被清除 → 只能按“连续簇”猜或转雕刻，结果要标 `partial/full`。
5. **权限**：直接读物理盘/固定卷需要管理员；U 盘/移动盘（`\\.\G:`）在 Windows 上只读访问通常无需管理员（本机已实测可读）。读不出来时让用户以管理员运行，或先做整盘镜像再扫镜像文件。
6. **SSD/TRIM 注意**：Windows 对 SSD 删除文件会下发 TRIM，数据可能已物理擦除 → 恢复率远低于 HDD/移动盘，必须提示用户。
7. **千万不要写回源盘**：恢复输出必须写到另一块盘。

---

## 5. 推荐技术路线（按投入从低到高）

### 路线 A（推荐）：继续用 FileDoctor（Python），NTFS 引擎换 pytsk3
- 依赖：`pip install pytsk3`（Windows 免编译，wheels 覆盖 Python 3.10–3.14）。
- 新增：`fdlib/ntfs.py` + `filetool.py ntfs` 子命令：`--list` 列出删除文件、`-o out` 恢复、`--offset` 指定分区、`--filter docx,pdf` 过滤。
- 复用已有：`repair`（ZIP/DOCX 重建）、`carve`（内容雕刻）、`probe`（文件系统识别）、`capacity`（坏盘验真）。
- GUI：PySide6（LGPL）或 Tkinter；打包 PyInstaller（需把 pytsk3 的 DLL 一并 collect）。
- 优点：开发快、功能拼装自然；许可清爽（Apache-2.0 绑定 + 我们自己的代码）。

### 路线 B（Windows 原生，MIT 友好）：C++/C# + 现成 MIT 组件
- 直接参考/复用 **KVC FileRecoveryTool（MIT）** 的 NTFS/MFT/USN + 雕刻引擎；或 C# + DiscUtils（MIT）+ Open XML SDK（MIT）自写。
- GUI 用 WinUI 3 / WPF，体验最好；开发量大。
- 适合想把“工具”做成正式 Windows 软件（可闭源分发）。

### 路线 C（不造轮子）：组合现成工具
- 删除恢复：KVC FileRecoveryTool（MIT，GUI）或 TestDisk（GPL）。
- 深度取证：Autopsy。
- 文档修复：LibreOffice / intactfile / Open XML SDK。
- 我们只写“一键编排 + 报告”的胶水脚本。
- 最快见效，但可定制性和“自己的作品”属性弱。

---

## 6. FileDoctor 的下一步落地计划（路线 A）

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P0 | `fdlib/ntfs.py`（pytsk3）：枚举 NTFS 分区、列出删除文件（名字/inode/大小/时间）、按 inode/条目恢复内容；CLI `ntfs --list/-o` | 在测试镜/卷上删除文件后能列出并恢复，SHA-256 与原文一致 |
| P0 | 测试夹具 `tools/make_ntfs_test_vhd.ps1`：管理员创建 VHDX→格式化 NTFS→写入样本→删除→供扫描 | 脚本可重复生成测试盘 |
| P1 | 修复流水线：文件类型识别 → ①文本编码修复 ②ZIP/OOXML 重建（`repair`）→ LibreOffice 二次校验 → 导出内容 | 用真实损坏 docx 验收 |
| P2 | NTFS 进阶：USN Journal/$LogFile 历史、压缩/稀疏文件、ADS；GUI（PySide6）+ 报告导出 + PyInstaller 单文件 | 可给非技术用户使用 |

## 7. 本次已落地并验证的内容（Route A）

- `fdlib/ntfs.py`：pytsk3 版 NTFS 扫描/恢复（目录遍历 + MFT 未分配记录扫描、名字丢失时用 inode 编号、
  读取失败标记 `recovered_full=false`）。
- CLI：`python filetool.py ntfs <镜像|设备> --list / -o <目录> / --inode-scan / --partitions / --filter`。
- **真实数据验证**（已通过）：
  - 用公开的真实 NTFS 3.x 镜像（dfir_ntfs 的 `ntfs3.raw`，64MB）列出 `/test/1.txt` 并完整恢复；
  - 把 inode 28（`/test/1.txt`，286,671 字节）的 MFT 记录模拟为已删除，`--inode-scan` 找到并通过
    data run 完整恢复，**SHA-256 与原文一致**（`tests/test_ntfs.py` 全绿）；
  - 非 NTFS（ext2）镜像会给出清晰报错。
- GUI：`gui.py`（PySide6，5 个标签页：NTFS 恢复 / 文件修复 / 镜像雕刻 / U 盘体检 / 帮助），
  已通过离屏自检；PyInstaller 打包出 `dist\FileDoctor\FileDoctor.exe`（`FileDoctor.exe --self-test` 可验证）。
- `tools/make_ntfs_test_vhd.ps1`：管理员一键生成 NTFS 测试 VHD，用于真实“复制→删除→恢复”验收（待提权跑一遍）。
- `tools/fetch_test_assets.py`：下载/生成 NTFS 测试素材（含模拟删除记录生成）。
- 现有 `repair`/`carve`/`probe`/`exfat`/`capacity` 保持可用。

> 剩余待办：物理盘/整盘 VHD 的“真实误删”测试（需管理员）、USN Journal/$LogFile 深度恢复、FAT32 删除恢复。

## 8. 参考链接

- The Sleuth Kit：<https://github.com/sleuthkit/sleuthkit> ｜ Autopsy：<https://github.com/sleuthkit/autopsy>
- pytsk3（Windows wheel）：<https://pypi.org/project/pytsk3/> ｜ 源码：<https://github.com/py4n6/pytsk>
- TestDisk / PhotoRec：<https://www.cgsecurity.org/wiki/TestDisk>
- KVC FileRecoveryTool（MIT，原生 Windows）：<https://github.com/wesmar/FileRecoveryTool>
- dfir_ntfs（GPLv3）：<https://github.com/msuhanov/dfir_ntfs> ｜ RecuperaBit（GPLv3）：<https://github.com/Topper-crypto/RecuperaBit>
- pytsk3_data-recovery（MIT，可参考实现）：<https://github.com/n3r0-b1n4ry/pytsk3_data-recovery>
- intactfile-zip-repair（MIT，ZIP/OOXML 修复）：<https://www.npmjs.com/package/intactfile-zip-repair>
- DiscUtils（.NET，MIT）：<https://github.com/DiscUtils/DiscUtils> ｜ Open XML SDK（MIT）：<https://github.com/dotnet/Open-XML-SDK>
- LibreOffice（MPL-2.0）：<https://www.libreoffice.org/>
