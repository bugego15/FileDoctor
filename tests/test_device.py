"""device 模块测试：镜像文件的读取、越界截断、迭代分块与重叠。"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fdlib.device import DeviceReader, is_raw_device

TMP = tempfile.mkdtemp(prefix="fdtest_dev_")
FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("== test_device ==")
    path = os.path.join(TMP, "img.bin")
    size = 3 * 1024 * 1024 + 777
    data = bytes((i * 7 + 3) & 0xFF for i in range(size))
    with open(path, "wb") as f:
        f.write(data)

    check("普通路径不是裸设备", not is_raw_device(path))
    check("裸设备路径识别", is_raw_device(r"\\.\G:"))

    with DeviceReader(path, chunk_size=512 * 1024) as dev:
        check("size 正确", dev.size == size, str(dev.size))
        check("整体读取一致", dev.read_at(0, size) == data)
        check("偏移读取一致", dev.read_at(12345, 1000) == data[12345:13345])
        check("越界截断", dev.read_at(size - 10, 100) == data[-10:])
        check("越过 EOF 返回空", dev.read_at(size + 100, 10) == b"")
        check("非法参数返回空", dev.read_at(-5, 10) == b"" and dev.read_at(0, 0) == b"")

        chunks = list(dev.iter_chunks(1000, 200_000, overlap=1024))
        check("分块非空且首块偏移正确", bool(chunks) and chunks[0][0] == 1000)
        rebuilt = bytearray()
        last_off = None
        last_chunk = b""
        ok_overlap = True
        for off, chunk in chunks:
            if last_off is not None:
                overlap = (last_off + len(last_chunk)) - off
                ok_overlap &= overlap >= 1024
                if overlap > 0:
                    ok_overlap &= last_chunk[len(last_chunk) - overlap:] == chunk[:overlap]
                rebuilt += chunk[overlap:]
            else:
                rebuilt += chunk
            last_off, last_chunk = off, chunk
        check("分块数据可无损拼接", bytes(rebuilt) == data[1000:201000], f"{len(rebuilt)}")
        check("分块间有重叠", ok_overlap)

    shutil.rmtree(TMP, ignore_errors=True)
    if FAILS:
        print(f"\nFAILED: {FAILS}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
