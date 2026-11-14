"""demo.py — 可运行的真实演示：日志流式扫描中随时启停规则。

运行：python3 -I demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from matcher import Scanner


def main():
    patterns = {
        "ERR_CRASH": b"crash",
        "ERR_TIMEOUT": b"timeout",
        "WARN_RETRY": b"retry",
        "METRIC_OK": b"ok",
        "ANY_A": b"a",          # 与上面规则存在重叠/包含关系
    }
    sc = Scanner(patterns)
    print("规则全部初始停用，先喂一段日志（无输出）：")
    print("  feed #1:", b"crash then retry")
    got = list(sc.feed(b"crash then retry"))
    print("  输出   :", got)
    assert got == []

    print("\n启用 ERR_CRASH / WARN_RETRY，继续喂：")
    sc.enable("ERR_CRASH")
    sc.enable("WARN_RETRY")
    chunk = b"crash, retry ok"
    print("  feed #2:", chunk)
    for end, pid in sc.feed(chunk):
        print(f"  -> end={end:3d}  id={pid}")

    print("\n跨 chunk 匹配：先读到 'time'（停用状态下读入的前缀），再启用：")
    list(sc.feed(b"time"))
    sc.enable("ERR_TIMEOUT")
    print("  feed #3: b'out'（启用后结束的匹配仍可组成）")
    for end, pid in sc.feed(b"out"):
        print(f"  -> end={end:3d}  id={pid}")

    print("\n幂等启停：重复 enable/disable 不报错、不重复：")
    sc.enable("ERR_CRASH")
    sc.disable("WARN_RETRY")
    sc.disable("WARN_RETRY")
    print("  feed #4: b'retry crash'（retry 已停用）")
    for end, pid in sc.feed(b"retry crash"):
        print(f"  -> end={end:3d}  id={pid}")

    print("\n同位置多命中按 id 的 Unicode 字典序输出：")
    sc2 = Scanner({"z": b"ab", "a": b"ab", "m": b"ab"})
    for pid in ("z", "a", "m"):
        sc2.enable(pid)
    print("  feed   : b'ab'")
    for end, pid in sc2.feed(b"ab"):
        print(f"  -> end={end:3d}  id={pid}")

    print("\nDEMO FINISHED")


if __name__ == "__main__":
    main()
