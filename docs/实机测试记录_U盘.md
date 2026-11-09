# 实机测试记录：58 GB NTFS U 盘（Windows 10/11）

日期：2026-11-09
环境：Windows，U 盘通过 `\\.\G:` 只读访问（可移动盘无需管理员），盘为 **NTFS**、容量约 58 GiB。
测试前已把盘上原有文件备份到本地；恢复输出全部写到电脑本地磁盘，未写回 U 盘。

## 测试内容与结果

| # | 项目 | 方法 | 结果 |
| --- | --- | --- | --- |
| 1 | 设备/文件系统识别 | `filetool.py probe \\.\G:` | NTFS，簇 4096 B，原始引导扇区与 Windows 挂载信息一致 ✅ |
| 2 | 准备样本 | 在 U 盘写入样本：UTF-8/GBK 文本、PNG、ZIP、带图片 DOCX、子目录文件、中文+空格文件名、损坏 DOCX/ZIP | ✅ |
| 3 | 模拟误删 | 删除其中多个文件（txt/png/zip/docx/子目录文件/中文名文件），其余保留 | ✅ |
| 4 | 删除文件扫描 | `filetool.py ntfs \\.\G: --inode-scan` | 删除文件全部列出，且**恢复出真实文件名**（目录项 + MFT 未分配记录合并、按 inode 去重） ✅ |
| 5 | 删除文件恢复（第一批） | `filetool.py ntfs \\.\G: -o <目录> --inode-scan` | **4/4 恢复，SHA-256 与删除前原件逐字节一致**（恢复时即校验，早于任何后续写入） ✅ |
| 6 | 文件名与目录结构 | 恢复第二批（含中文+空格文件名、子目录文件） | 中文文件名保持原样；恢复路径保留目录结构（`_filedoctor_test/subdir/delete_me.txt`） ✅ |
| 7 | 损坏 DOCX 修复 | `filetool.py repair .../broken.docx -o fixed.docx --template template.docx` | 抢救 18 个部件，修复后可正常打开（段落/图片完整） ✅ |
| 8 | 损坏 ZIP 修复 | `filetool.py repair .../broken.zip -o fixed.zip` | 抢救 2 个条目，内容完整 ✅ |
| 9 | 原始设备特征码雕刻 | `filetool.py carve \\.\G: --length 128MB --types docx,zip,png,pdf` | 从原始扇区雕刻出 3 个完整 DOCX（不依赖文件系统元数据） ✅ |
| 10 | 容量/写入完整性（快速） | `filetool.py capacity G:\`（256 MB 写入+读回） | 校验通过；写 ~55 MB/s、读 ~287 MB/s；测试后盘上原文件 SHA-256 不变 ✅ |
| 11 | 容量/写入完整性（整盘） | `filetool.py capacity G:\ --write-gb 62 --yes`（约 62 GB 写入+读回，检测扩容/坏块） | 见下方结果 |

## 本次测试带出的改进

1. **删除文件真实文件名恢复**：NTFS 删除后目录项里仍保留 `name + meta_addr`，TSK 的 `entry.info.meta` 可能为
   `None`。新增逻辑用 `meta_addr` 回查 MFT 记录（且必须是“未分配”记录，避免目录项被复用时误报），
   再与 MFT 未分配记录按 inode 去重 → 恢复时直接使用原文件名，而不是 `inode_41`。
2. **恢复保留目录结构**：普通/删除文件按原相对路径恢复（`_filedoctor_test/subdir/delete_me.txt`），
   孤儿 MFT 记录（$OrphanFiles）统一放到 `_orphan/` 下，避免同名覆盖与线索丢失。
3. 上述改进已补进 `tests/test_ntfs.py`（真实 NTFS 镜像 + 模拟删除记录断言）。

## 说明

- 容量测试中/之后的删除恢复结果会受“被删文件簇被复用”影响；第 5 项 SHA-256 校验是在删除后、
  任何大写入之前完成的，结果有效。
- 整盘容量测试耗时较长（约 20 分钟：写 ~62 GB + 读回），结束后会自动删除测试文件；
  若检测到扩容/坏块，会给出第一个坏偏移及真实容量估算。
