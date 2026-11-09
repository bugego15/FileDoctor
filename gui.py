#!/usr/bin/env python3
"""FileDoctor GUI（Windows 图形界面，PySide6）。

用法：
    python gui.py                # 打开图形界面
    python gui.py --self-test    # 无界面自检（供 CI/PyInstaller 包验证），结果写 JSON 到
                                 #   %FILEDOCTOR_SELFTEST_OUT% 或 %TEMP%\\filedoctor_selftest.json

功能页：
1. NTFS 恢复：扫描 NTFS 镜像/分区/U 盘，列出（含已删除）文件，选中恢复；
2. 文件修复：修复损坏的 ZIP/DOCX（可用同源健康文件做模板补结构/图片关系）；
3. 镜像雕刻：从镜像/设备按特征码恢复 docx/zip/png/jpg/pdf/exe，可选文本碎片；
4. U 盘体检：写入/读回完整性检测（扩容盘/坏盘），估算真实容量。

依赖：PySide6（GUI）、pytsk3（NTFS 恢复，可选，缺失时对应页会提示安装）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from fdlib import __version__  # noqa: E402
from fdlib import capacity, carve, ntfs, ziprepair  # noqa: E402

APP_TITLE = f"FileDoctor 文件恢复工具箱 v{__version__}"


# --------------------------------------------------------------------------- 后台线程
class Worker(QThread):
    log = Signal(str)
    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._kwargs.setdefault("log", self.log.emit)

    def run(self):  # noqa: D401
        try:
            data = self._fn(*self._args, **self._kwargs)
            self.result.emit(data)
        except Exception as exc:  # noqa: BLE001
            detail = traceback.format_exc()
            self.log.emit(detail)
            self.error.emit(f"{type(exc).__name__}: {exc}")


def _fmt_size(n) -> str:
    try:
        n = int(n)
    except Exception:  # noqa: BLE001
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class LogBox(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)

    def info(self, msg: str) -> None:
        self.appendPlainText(str(msg))


# --------------------------------------------------------------------------- NTFS 页
class NtfsTab(QWidget):
    def __init__(self, log: LogBox, parent=None):
        super().__init__(parent)
        self.log = log
        self.entries: list[dict] = []
        self.worker = None

        layout = QVBoxLayout(self)
        src_box = QGroupBox("源：NTFS 镜像 / 分区 / U 盘设备路径")
        form = QFormLayout(src_box)
        row = QHBoxLayout()
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText(r"例如 D:\usb.img 或 \\.\G: 或 \\.\PhysicalDrive2")
        b1 = QPushButton("浏览镜像…")
        b1.clicked.connect(self._browse_src)
        row.addWidget(self.src_edit, 1)
        row.addWidget(b1)
        form.addRow("镜像/设备：", row)
        self.offset_edit = QLineEdit()
        self.offset_edit.setPlaceholderText("自动探测（可填十进制字节偏移，如 1048576）")
        form.addRow("分区偏移：", self.offset_edit)
        opts = QHBoxLayout()
        self.inode_scan = QCheckBox("扫描 MFT 未分配记录（孤儿删除文件）")
        self.inode_scan.setChecked(True)
        self.include_system = QCheckBox("显示 NTFS 系统文件")
        opts.addWidget(self.inode_scan)
        opts.addWidget(self.include_system)
        opts.addStretch(1)
        form.addRow("选项：", opts)
        layout.addWidget(src_box)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "已删除", "inode", "来源", "路径"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 2)

        btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("① 扫描")
        self.scan_btn.clicked.connect(self.start_scan)
        self.recover_btn = QPushButton("② 恢复选中项…")
        self.recover_btn.clicked.connect(self.recover_selected)
        self.recover_all_btn = QPushButton("恢复全部已删除文件…")
        self.recover_all_btn.clicked.connect(self.recover_all)
        btn_row.addWidget(self.scan_btn)
        btn_row.addWidget(self.recover_btn)
        btn_row.addWidget(self.recover_all_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        if not ntfs.HAS_PYTSK3:
            tip = QLabel("提示：NTFS 恢复需要 pytsk3，安装命令：python -m pip install pytsk3")
            tip.setStyleSheet("color:#b00;")
            layout.addWidget(tip)

    def _browse_src(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 NTFS 镜像文件", "", "磁盘镜像 (*.img *.raw *.dd *.bin);;所有文件 (*)")
        if path:
            self.src_edit.setText(path)

    def _options(self):
        offset = self.offset_edit.text().strip()
        try:
            offset_val = int(offset) if offset else None
        except ValueError:
            raise ValueError("分区偏移必须是数字（字节）") from None
        return {
            "offset": offset_val,
            "inode_scan": self.inode_scan.isChecked(),
            "include_system": self.include_system.isChecked(),
        }

    def _run_worker(self, fn, on_done, **kwargs):
        self.set_busy(True)
        self.worker = Worker(fn, **kwargs)
        self.worker.log.connect(self.log.info)
        self.worker.result.connect(on_done)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(lambda: self.set_busy(False))
        self.worker.start()

    def set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        for b in (self.scan_btn, self.recover_btn, self.recover_all_btn):
            b.setEnabled(not busy)

    def _on_error(self, msg):
        self.log.info(f"❌ 出错：{msg}")
        QMessageBox.critical(self, "操作失败", msg)

    def start_scan(self):
        path = self.src_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请先选择镜像文件或输入设备路径")
            return
        try:
            opts = self._options()
        except ValueError as exc:
            QMessageBox.warning(self, "提示", str(exc))
            return
        self.log.info(f"开始扫描：{path} …")

        def task(offset=None, inode_scan=True, include_system=False, log=None):
            with ntfs.open_volume(path, offset=offset) as vol:
                entries = vol.list_entries(include_deleted=True, limit=None,
                                           scan_unallocated=inode_scan,
                                           include_system=include_system)
                return {"info": vol.info, "entries": entries}

        self._run_worker(task, self._scan_done, **opts)

    def _scan_done(self, data):
        self.entries = data.get("entries") or []
        info = data.get("info") or {}
        self.log.info(f"扫描完成：{info.get('fs_type')} offset={info.get('offset')} "
                      f"block={info.get('block_size')}，共 {len(self.entries)} 项")
        self.table.setRowCount(len(self.entries))
        for i, e in enumerate(self.entries):
            vals = [e.get("name"), _fmt_size(e.get("size")), "是" if e.get("deleted") else "否",
                    str(e.get("inode") or ""), e.get("source") or "", e.get("path") or ""]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                item.setData(Qt.UserRole, i)
                self.table.setItem(i, c, item)
        self.table.resizeColumnsToContents()

    def _selected_indices(self) -> list[int]:
        return sorted({self.table.item(r, 0).data(Qt.UserRole) for r in
                       {idx.row() for idx in self.table.selectedIndexes()} if
                       self.table.item(r, 0) is not None})

    def _choose_outdir(self, title="选择恢复输出目录") -> str:
        d = QFileDialog.getExistingDirectory(self, title)
        return d or ""

    def _recover_entries(self, which: list[dict], outdir: str):
        path = self.src_edit.text().strip()
        opts = self._options()

        def task(offset=None, inode_scan=True, include_system=False, log=None):
            with ntfs.open_volume(path, offset=offset) as vol:
                files = []
                errors = []
                total = len(which)
                for idx, want in enumerate(which):
                    log(f"[{idx + 1}/{total}] 读取 inode {want.get('inode')} …")
                    rec = vol.read_entry(want)
                    entry = {"name": want.get("name"), "inode": want.get("inode"),
                             "size": want.get("size"), "deleted": want.get("deleted"),
                             "recovered": False, "recovered_full": bool(rec["recovered_full"]),
                             "note": rec["note"], "path": None}
                    if rec["data"]:
                        out = ntfs.output_path_for(outdir, want)
                        try:
                            with open(out, "wb") as fh:
                                fh.write(rec["data"])
                            entry.update(recovered=True, path=out, recovered_size=len(rec["data"]))
                        except OSError as exc:
                            errors.append(str(exc))
                    files.append(entry)
                return {"count": len(files), "files": files, "errors": errors,
                        "recovered_count": sum(1 for f in files if f["recovered"])}

        self._run_worker(task, self._recover_done, **opts)

    def _recover_done(self, data):
        n = data.get("recovered_count", 0)
        self.log.info(f"恢复完成：成功 {n}/{data.get('count')} 个，输出目录：{data.get('outdir', '')}")
        for f in data.get("files", []):
            if f.get("recovered"):
                self.log.info(f"  ✔ {f.get('name')} -> {f.get('path')}")
            else:
                self.log.info(f"  ✘ {f.get('name')}：{f.get('note')}")
        if data.get("errors"):
            self.log.info(f"  错误：{data['errors']}")
        QMessageBox.information(self, "恢复完成", f"成功恢复 {n} 个文件")

    def recover_selected(self):
        idxs = self._selected_indices()
        if not idxs:
            QMessageBox.warning(self, "提示", "请先在表格里选中要恢复的文件")
            return
        outdir = self._choose_outdir()
        if not outdir:
            return
        self._recover_entries([self.entries[i] for i in idxs if i < len(self.entries)], outdir)
        self._last_outdir = outdir

    def recover_all(self):
        outdir = self._choose_outdir()
        if not outdir:
            return
        which = [e for e in self.entries if e.get("deleted")]
        if not which:
            QMessageBox.information(self, "提示", "没有已删除文件可恢复（可先扫描）")
            return
        self._recover_entries(which, outdir)


# --------------------------------------------------------------------------- 文件修复页
class RepairTab(QWidget):
    def __init__(self, log: LogBox, parent=None):
        super().__init__(parent)
        self.log = log
        self.worker = None
        layout = QVBoxLayout(self)
        box = QGroupBox("修复损坏的 ZIP / DOCX")
        form = QFormLayout(box)
        r1 = QHBoxLayout()
        self.src = QLineEdit()
        self.src.setPlaceholderText(r"例如 broken.docx")
        b = QPushButton("浏览…")
        b.clicked.connect(lambda: self._browse(self.src, "选择损坏文件"))
        r1.addWidget(self.src, 1)
        r1.addWidget(b)
        form.addRow("损坏文件：", r1)
        r2 = QHBoxLayout()
        self.template = QLineEdit()
        self.template.setPlaceholderText("可选：同源健康 docx（补 [Content_Types].xml / 图片关系）")
        b2 = QPushButton("浏览…")
        b2.clicked.connect(lambda: self._browse(self.template, "选择同源健康文件（可选）"))
        r2.addWidget(self.template, 1)
        r2.addWidget(b2)
        form.addRow("健康模板：", r2)
        r3 = QHBoxLayout()
        self.out = QLineEdit()
        b3 = QPushButton("另存为…")
        b3.clicked.connect(self._browse_out)
        r3.addWidget(self.out, 1)
        r3.addWidget(b3)
        form.addRow("输出文件：", r3)
        layout.addWidget(box)
        btn = QPushButton("开始修复")
        btn.clicked.connect(self.start)
        layout.addWidget(btn)
        layout.addWidget(QLabel("提示：修复是只读的，不会改动原文件；输出请存到其它磁盘。"))
        layout.addStretch(1)

    def _browse(self, edit: QLineEdit, title: str):
        path, _ = QFileDialog.getOpenFileName(self, title, "", "文档 (*.docx *.zip *.doc *.xlsx);;所有文件 (*)")
        if path:
            edit.setText(path)

    def _browse_out(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存修复结果", "", "Word 文档 (*.docx);;ZIP (*.zip);;所有文件 (*)")
        if path:
            self.out.setText(path)

    def start(self):
        src = self.src.text().strip()
        out = self.out.text().strip()
        if not src or not out:
            QMessageBox.warning(self, "提示", "请选择损坏文件和输出文件")
            return
        template = self.template.text().strip() or None
        self.log.info(f"开始修复：{src}")

        def task(log=None):
            is_docx = src.lower().endswith(".docx") or out.lower().endswith(".docx")
            if is_docx:
                result = ziprepair.repair_docx(src, out, template)
            else:
                result = ziprepair.repair_zip(src, out)
            result["output"] = os.path.abspath(out)
            return result

        self.worker = Worker(task)
        self.worker.log.connect(self.log.info)
        self.worker.result.connect(self._done)
        self.worker.error.connect(lambda m: QMessageBox.critical(self, "修复失败", m))
        self.worker.start()

    def _done(self, result):
        self.log.info(json.dumps(result, ensure_ascii=False, indent=1))
        QMessageBox.information(self, "修复完成", f"输出：{result.get('output')}")


# --------------------------------------------------------------------------- 雕刻页
class CarveTab(QWidget):
    def __init__(self, log: LogBox, parent=None):
        super().__init__(parent)
        self.log = log
        self.worker = None
        layout = QVBoxLayout(self)
        box = QGroupBox("从镜像 / 原始设备按特征码雕刻恢复")
        form = QFormLayout(box)
        r1 = QHBoxLayout()
        self.src = QLineEdit()
        self.src.setPlaceholderText(r"镜像文件或 \\.\G: 设备")
        b = QPushButton("浏览…")
        b.clicked.connect(self._browse_src)
        r1.addWidget(self.src, 1)
        r1.addWidget(b)
        form.addRow("源：", r1)
        r2 = QHBoxLayout()
        self.out = QLineEdit()
        b2 = QPushButton("选择输出目录…")
        b2.clicked.connect(self._browse_out)
        r2.addWidget(self.out, 1)
        r2.addWidget(b2)
        form.addRow("输出目录：", r2)
        r3 = QHBoxLayout()
        self.types = {}
        for t in ("docx", "zip", "png", "jpg", "pdf", "exe"):
            cb = QCheckBox(t)
            cb.setChecked(True)
            self.types[t] = cb
            r3.addWidget(cb)
        self.text_cb = QCheckBox("文本碎片")
        r3.addWidget(self.text_cb)
        form.addRow("类型：", r3)
        r4 = QHBoxLayout()
        self.start_sb = QSpinBox()
        self.start_sb.setRange(0, 2 ** 31 - 1)
        self.start_sb.setSingleStep(1024 * 1024)
        self.length_sb = QSpinBox()
        self.length_sb.setRange(0, 2 ** 31 - 1)
        self.length_sb.setSingleStep(1024 * 1024)
        r4.addWidget(QLabel("起始字节(0=开头)："))
        r4.addWidget(self.start_sb)
        r4.addWidget(QLabel("长度(0=到结尾)："))
        r4.addWidget(self.length_sb)
        form.addRow("范围：", r4)
        layout.addWidget(box)
        btn = QPushButton("开始雕刻")
        btn.clicked.connect(self.start)
        layout.addWidget(btn)
        layout.addWidget(QLabel("提示：只读扫描；对整块 U 盘扫描建议先限制长度（如 256MB）试跑。"))
        layout.addStretch(1)

    def _browse_src(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择镜像", "", "镜像 (*.img *.raw *.dd *.bin);;所有文件 (*)")
        if path:
            self.src.setText(path)

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.out.setText(d)

    def start(self):
        src = self.src.text().strip()
        out = self.out.text().strip()
        if not src or not out:
            QMessageBox.warning(self, "提示", "请选择源和输出目录")
            return
        types = [t for t, cb in self.types.items() if cb.isChecked()]
        start = self.start_sb.value()
        length = self.length_sb.value() or None
        self.log.info(f"开始雕刻：{src} -> {out} 类型={types}")

        def task(log=None):
            return carve.carve_image(src, out, types=types or None, text=self.text_cb.isChecked(),
                                     start=start, length=length, min_size=1024, max_files=500,
                                     progress=lambda s, t: log(f"扫描进度 {s / 1e9:.2f}/{t / 1e9:.2f} GB"))

        self.worker = Worker(task)
        self.worker.log.connect(self.log.info)
        self.worker.result.connect(lambda r: (
            self.log.info(f"雕刻完成：{r.get('by_type')} 文件={len(r.get('files', []))} 文本={len(r.get('texts', []))}"),
            QMessageBox.information(self, "雕刻完成", json.dumps(r.get("by_type"), ensure_ascii=False))))
        self.worker.error.connect(lambda m: QMessageBox.critical(self, "雕刻失败", m))
        self.worker.start()


# --------------------------------------------------------------------------- 体检页
class CapacityTab(QWidget):
    def __init__(self, log: LogBox, parent=None):
        super().__init__(parent)
        self.log = log
        self.worker = None
        layout = QVBoxLayout(self)
        box = QGroupBox("U 盘 / 磁盘容量与写入完整性检测")
        form = QFormLayout(box)
        r1 = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText(r"例如 G:\ 或 D:\temp\cptest")
        b = QPushButton("选择目录…")
        b.clicked.connect(self._browse)
        r1.addWidget(self.target, 1)
        r1.addWidget(b)
        form.addRow("目标目录：", r1)
        r2 = QHBoxLayout()
        self.gb = QSpinBox()
        self.gb.setRange(0, 4000)
        self.gb.setValue(0)
        r2.addWidget(QLabel("写入 GB（0=只做 256MB 快速体检）："))
        r2.addWidget(self.gb)
        form.addRow("测试量：", r2)
        self.confirm = QCheckBox("我确认该盘没有需要保留的数据（写满测试会占满磁盘）")
        form.addRow("", self.confirm)
        layout.addWidget(box)
        btn = QPushButton("开始体检")
        btn.clicked.connect(self.start)
        layout.addWidget(btn)
        layout.addWidget(QLabel("原理：按偏移写入不同的伪随机数据再读回比对；扩容盘会在真实容量处开始不一致。"))
        layout.addStretch(1)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择要检测的目录（如 U 盘根目录）")
        if d:
            self.target.setText(d)

    def start(self):
        target = self.target.text().strip()
        if not target:
            QMessageBox.warning(self, "提示", "请选择目标目录")
            return
        gb = self.gb.value()
        if gb > 0 and not self.confirm.isChecked():
            QMessageBox.warning(self, "提示", "大容量写入测试需要勾选确认")
            return
        self.log.info(f"开始体检：{target}（写入 {gb if gb else 0.256} GB）")

        def task(log=None):
            return capacity.test_capacity(target, write_bytes=(gb * 10 ** 9 or None),
                                          progress=lambda stage, done, total: log(f"{stage} {done}/{total}"))

        self.worker = Worker(task)
        self.worker.log.connect(self.log.info)
        self.worker.result.connect(self._done)
        self.worker.error.connect(lambda m: QMessageBox.critical(self, "体检失败", m))
        self.worker.start()

    def _done(self, r):
        self.log.info(json.dumps(r, ensure_ascii=False, indent=1))
        if r.get("ok"):
            QMessageBox.information(self, "体检完成", "写入/读回校验通过 ✅")
        else:
            QMessageBox.warning(self, "体检完成", f"发现问题 ❌\n{r.get('notes')}")


# --------------------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 760)
        self.log = LogBox()
        tabs = QTabWidget()
        tabs.addTab(NtfsTab(self.log), "NTFS 恢复")
        tabs.addTab(RepairTab(self.log), "文件修复")
        tabs.addTab(CarveTab(self.log), "镜像雕刻")
        tabs.addTab(CapacityTab(self.log), "U 盘体检")
        help_tab = QWidget()
        hv = QVBoxLayout(help_tab)
        help_label = QLabel(
            "<h3>FileDoctor 使用提示</h3>"
            "<ol>"
            "<li><b>NTFS 恢复</b>：选择 NTFS 镜像或直接输入 <code>\\\\.\\G:</code> 这类设备路径"
            "（读物理盘/固定卷需要管理员；可移动盘通常只读即可）。扫描后选中条目恢复；"
            "“孤儿删除文件”来自 MFT 未分配记录，名字可能丢失但内容有时还能恢复。</li>"
            "<li><b>文件修复</b>：损坏的 docx/zip（保存中断、复制损坏、打不开）可尝试修复；"
            "有同源健康文件时填“健康模板”，可自动补齐结构件与图片关系。</li>"
            "<li><b>镜像雕刻</b>：文件系统已损坏/被格式化时，按特征码从原始字节里捞文件。</li>"
            "<li><b>U 盘体检</b>：新买的 U 盘/存储卡先测容量真实性，扩容盘会在真实容量处开始校验失败。</li>"
            "</ol>"
            "<p>恢复出的文件请保存到<b>另一块磁盘</b>；发现数据丢失后不要继续往源盘写东西。</p>"
            "<p>依赖：NTFS 恢复需要 <code>pytsk3</code>（pip install pytsk3，Windows 有预编译 wheel）。</p>")
        help_label.setWordWrap(True)
        help_label.setOpenExternalLinks(True)
        hv.addWidget(help_label)
        hv.addStretch(1)
        tabs.addTab(help_tab, "帮助")

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(tabs)
        splitter.addWidget(self.log)
        splitter.setSizes([560, 180])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage(f"FileDoctor v{__version__} ready")

    def closeEvent(self, event):  # noqa: N802
        for tab in [self.centralWidget().widget(0).widget(i) for i in range(4)]:
            worker = getattr(tab, "worker", None)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
        super().closeEvent(event)


# --------------------------------------------------------------------------- 入口
def _self_test() -> int:
    """无界面自检：验证依赖与主窗口可创建，结果写 JSON 并退出。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = {
        "app": "FileDoctor",
        "version": __version__,
        "python": sys.version,
        "pytsk3": ntfs.HAS_PYTSK3,
        "pytsk3_error": ntfs.PYTSK3_IMPORT_ERROR,
    }
    try:
        from PySide6 import __version__ as pyside_version  # noqa: PLC0415

        result["pyside6"] = pyside_version
        app = QApplication.instance() or QApplication([])
        win = MainWindow()
        result["tabs"] = win.centralWidget().widget(0).count()
        result["ok"] = True
        win.close()
        app.processEvents()
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    out = os.environ.get("FILEDOCTOR_SELFTEST_OUT") or os.path.join(
        tempfile.gettempdir(), "filedoctor_selftest.json")
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result["selftest_out"] = out
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--self-test", action="store_true", help="无界面自检后退出")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    app = QApplication(sys.argv if argv is None else [sys.argv[0]] + list(argv))
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
