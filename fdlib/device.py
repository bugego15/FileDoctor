"""原始设备/镜像读取工具。

支持三类路径：
- 普通镜像文件，例如 ``F:\\img\\usb.img``
- Windows 卷设备，例如 ``\\\\.\\G:``（U 盘/移动硬盘；只读访问通常不需要管理员）
- Windows 物理磁盘，例如 ``\\\\.\\PhysicalDrive3``（一般需要管理员）

裸设备有两个额外限制：读写偏移必须是扇区（512B）对齐，读长度也必须是扇区倍数。
本模块对调用方屏蔽这些细节。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
from typing import Iterator, Optional, Tuple

SECTOR = 512

_RAW_PREFIX = "\\\\.\\"  # 即 \\.\


def is_raw_device(path: str) -> bool:
    """判断路径是否是 Windows 设备路径（\\\\.\\...）。"""
    return bool(path) and path.replace("/", "\\").startswith(_RAW_PREFIX)


def _align_down(value: int, unit: int = SECTOR) -> int:
    return (value // unit) * unit


def _align_up(value: int, unit: int = SECTOR) -> int:
    return ((value + unit - 1) // unit) * unit


def _query_raw_size(path: str) -> Optional[int]:
    """通过 IOCTL_DISK_GET_LENGTH_INFO 查询裸设备/卷的总字节数。失败返回 None。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p
    ]
    kernel32.DeviceIoControl.restype = wt.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

    handle = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
    )
    if not handle or handle == wt.HANDLE(-1).value:
        return None
    try:
        length = ctypes.c_ulonglong(0)
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, IOCTL_DISK_GET_LENGTH_INFO,
            None, 0, ctypes.byref(length), ctypes.sizeof(length), ctypes.byref(returned), None,
        )
        if ok:
            return int(length.value)
        return None
    finally:
        kernel32.CloseHandle(handle)


class DeviceReader:
    """把镜像文件或裸设备统一成“可随机读”的流。"""

    def __init__(self, path: str, chunk_size: int = 4 * 1024 * 1024) -> None:
        if not path:
            raise ValueError("path 不能为空")
        self.path = path
        self.is_raw = is_raw_device(path)
        self.chunk_size = max(_align_up(int(chunk_size)), 64 * 1024) if self.is_raw else max(int(chunk_size), 64 * 1024)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        self._fd = os.open(path, flags)
        self._size = self._detect_size()

    # ------------------------------------------------------------------ 基础属性
    def _detect_size(self) -> Optional[int]:
        if not self.is_raw:
            try:
                return os.fstat(self._fd).st_size
            except OSError:
                return None
        size = _query_raw_size(self.path)
        if size:
            return size
        return None

    @property
    def size(self) -> Optional[int]:
        """总字节数；未知（例如拿不到长度的裸设备）时为 None。"""
        return self._size

    def close(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "DeviceReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ 读取
    def _ensure_open(self) -> None:
        if self._fd is None:
            raise ValueError("DeviceReader 已关闭")

    def _read_raw_span(self, offset: int, length: int) -> bytes:
        """裸设备读取：offset 向下、length 向上对齐到 512。"""
        aligned_off = _align_down(offset)
        pad_head = offset - aligned_off
        aligned_len = _align_up(pad_head + length)
        if self._size is not None and aligned_off >= self._size:
            return b""
        os.lseek(self._fd, aligned_off, os.SEEK_SET)
        out = bytearray()
        while len(out) < aligned_len:
            try:
                chunk = os.read(self._fd, aligned_len - len(out))
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return bytes(out[pad_head:pad_head + length])

    def read_at(self, offset: int, length: int) -> bytes:
        """从 *offset* 读取最多 *length* 字节；越界部分自动截断。"""
        self._ensure_open()
        if offset < 0 or length <= 0:
            return b""
        if self._size is not None and offset >= self._size:
            return b""
        if self._size is not None:
            length = min(length, self._size - offset)
        if not self.is_raw:
            os.lseek(self._fd, offset, os.SEEK_SET)
            out = bytearray()
            while len(out) < length:
                chunk = os.read(self._fd, length - len(out))
                if not chunk:
                    break
                out += chunk
            return bytes(out)
        return self._read_raw_span(offset, length)

    def iter_chunks(self, start: int = 0, length: Optional[int] = None,
                    overlap: int = 0) -> Iterator[Tuple[int, bytes]]:
        """按块产出 ``(绝对偏移, 数据)``，相邻块之间保留 *overlap* 字节重叠。"""
        self._ensure_open()
        total = self._size
        if total is None:
            raise ValueError("无法获知设备大小，请显式传入 length")
        start = max(0, int(start))
        end = total if length is None else min(total, start + int(length))
        if start >= end:
            return
        step = self.chunk_size
        pos = start
        while pos < end:
            want = min(step, end - pos)
            data = self.read_at(pos, want)
            if not data:
                break
            yield pos, data
            if pos + len(data) >= end:
                break
            pos += max(1, len(data) - min(overlap, len(data) - 1))
