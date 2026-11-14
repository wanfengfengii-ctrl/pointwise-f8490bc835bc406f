"""verify.py — 正确性验证：确定性用例 + 随机差分测试（对照 ReferenceScanner）。

运行：python -I verify.py
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from matcher import Scanner
from reference_matcher import ReferenceScanner


def check(name, got, want):
    assert got == want, f"[{name}] got={got!r} want={want!r}"
    shown = repr(got)
    if len(shown) > 120:
        shown = f"<{len(got)} items> {shown[:117]}..."
    print(f"  PASS {name}: {shown}")


def fixed_cases():
    print("== 确定性用例 ==")

    # 题目给的例子：停用状态下读入的前缀，启用后仍可完成匹配
    sc = Scanner({"A": b"aba"})
    r = list(sc.feed(b"ab"))
    check("disabled-prefix", r, [])
    sc.enable("A")
    r = list(sc.feed(b"a"))
    check("enabled-completes-prefix", r, [(3, "A")])

    # 空 chunk 无输出，位置不漂移
    check("empty-chunk", list(sc.feed(b"")), [])

    # 基础多匹配 + 同位置字典序 + 重叠
    sc = Scanner({"B": b"ab", "A": b"a", "C": b"aba", "D": b"b"})
    sc.enable("A"); sc.enable("B"); sc.enable("C"); sc.enable("D")
    got = list(sc.feed(b"aba"))
    want = [(1, "A"), (2, "B"), (2, "D"), (3, "A"), (3, "C")]
    check("overlap-and-order", got, want)

    # 不同 id 同内容
    sc = Scanner({"z": b"x", "a": b"x", "m": b"x"})
    sc.enable("z"); sc.enable("a"); sc.enable("m")
    check("duplicate-patterns", list(sc.feed(b"xx")),
          [(1, "a"), (1, "m"), (1, "z"), (2, "a"), (2, "m"), (2, "z")])

    # 启停幂等
    sc = Scanner({"A": b"a"})
    sc.enable("A"); sc.enable("A")
    check("idempotent-enable", list(sc.feed(b"a")), [(1, "A")])
    sc.disable("A"); sc.disable("A")
    check("idempotent-disable", list(sc.feed(b"a")), [])
    sc.enable("A")
    check("re-enable", list(sc.feed(b"a")), [(3, "A")])

    # 停用后不再产出，重新启用后状态连续
    sc = Scanner({"A": b"ab", "B": b"b"})
    sc.enable("A"); sc.enable("B")
    check("both-on", list(sc.feed(b"ab")), [(2, "A"), (2, "B")])
    sc.disable("A")
    # 第二个 chunk 为位置 3..4 的 b"ab"：仅 pos4 的 'b' 命中 B
    check("a-off", list(sc.feed(b"ab")), [(4, "B")])
    sc.enable("A")
    # 第三个 chunk 为位置 5..6：pos6 处 A 与 B 同时结束
    check("a-on-again", list(sc.feed(b"ab")), [(6, "A"), (6, "B")])

    # 跨很多 chunk 的逐字节喂入
    sc = Scanner({"P": b"hello"})
    sc.enable("P")
    got = []
    for ch in b"xxhelloxhello":
        got.extend(sc.feed(bytes([ch])))
    check("byte-by-byte", got, [(7, "P"), (13, "P")])

    # 仅在匹配结束瞬间判定启用状态：停用阶段读到的内容不产生追溯输出
    sc = Scanner({"A": b"ab"})
    list(sc.feed(b"a"))
    sc.enable("A")
    check("enable-midstream", list(sc.feed(b"b")), [(2, "A")])

    # 字节 0 与 255 等全字节域
    sc = Scanner({"Z": b"\x00\xff", "O": b"\xff\x00"})
    sc.enable("Z"); sc.enable("O")
    check("full-byte-domain", list(sc.feed(b"\x00\xff\x00")),
          [(2, "Z"), (3, "O")])

    # 初始全部停用，大量数据零输出且不膨胀
    sc = Scanner({f"P{i}": bytes([i % 256]) * ((i % 5) + 1) for i in range(50)})
    check("all-disabled", list(sc.feed(b"\x00" * 100 + b"abc")), [])

    # Unicode id 字典序（按码位）
    sc = Scanner({"é": b"q", "a": b"q", "中": b"q", "A": b"q"})
    for pid in sc._id_info:
        sc.enable(pid)
    got = [pid for _, pid in sc.feed(b"q")]
    check("unicode-id-order", got, sorted(got))
    assert got == ["A", "a", "é", "中"], got
    print("  PASS unicode-id-order exact:", got)

    # 末端共享 + 只启用部分 id
    sc = Scanner({"x1": b"ab", "x2": b"ab", "y": b"a"})
    sc.enable("x2")
    check("partial-on-shared-end", list(sc.feed(b"ab")), [(2, "x2")])
    sc.enable("x1"); sc.enable("y")
    check("rest-on-shared-end", list(sc.feed(b"ab")),
          [(3, "y"), (4, "x1"), (4, "x2")])


def random_differential(trials=400):
    print(f"== 随机差分测试（{trials} 轮，对照 ReferenceScanner） ==")
    rng = random.Random(20261114)
    for t in range(trials):
        alpha = rng.sample(range(256), rng.randint(1, 4))
        p = rng.randint(1, 8)
        patterns = {}
        for i in range(p):
            ln = rng.randint(1, 6)
            patterns[f"id{i:02d}"] = bytes(rng.choice(alpha) for _ in range(ln))
        # 允许同内容：再随机塞几个重复
        if rng.random() < 0.3 and patterns:
            src = rng.choice(list(patterns))
            patterns[f"dup{t}"] = patterns[src]

        sc = Scanner(patterns)
        ref = ReferenceScanner(patterns)
        got, want = [], []
        steps = rng.randint(1, 25)
        for _ in range(steps):
            action = rng.random()
            if action < 0.35 and patterns:
                pid = rng.choice(list(patterns))
                sc.enable(pid); ref.enable(pid)
            elif action < 0.55 and patterns:
                pid = rng.choice(list(patterns))
                sc.disable(pid); ref.disable(pid)
            else:
                chunk = bytes(rng.choice(alpha) for _ in range(rng.randint(0, 12)))
                got.extend(sc.feed(chunk))
                want.extend(ref.feed(chunk))
        assert got == want, f"trial {t} mismatch\n got={got}\nwant={want}"
    print(f"  PASS 全部 {trials} 轮差分一致")


def immutability_check():
    print("== patterns 不被修改 ==")
    patterns = {"A": b"ab", "B": b"abc"}
    snapshot = {k: v for k, v in patterns.items()}
    sc = Scanner(patterns)
    sc.enable("A")
    list(sc.feed(b"abc"))
    assert patterns == snapshot and all(
        patterns[k] is snapshot[k] for k in patterns)
    print("  PASS patterns 内容与对象身份均未变")


def adversarial_cases():
    print("== 对抗性/边界用例 ==")

    # 深 failure 链：单字节规则 a 与长串 a^k，输入反复在深状态与根之间弹跳，
    # 压力测试“稀疏 goto + failure 上溯”的摊还界（不能出现退化慢路径）
    k = 500
    patterns = {"single": b"a", "long": b"a" * k}
    sc = Scanner(patterns)
    sc.enable("single"); sc.enable("long")
    text = b"a" * k + b"b" + b"a" * k
    got = list(sc.feed(text))
    ref = ReferenceScanner(patterns)
    ref.enable("single"); ref.enable("long")
    want = list(ref.feed(text))
    check("deep-failure-chain", got, want)
    # 文本 = a^500 + b + a^500：long 在第二个 a^500 跑完时结束，即 end=1001
    assert (1001, "long") in got
    assert got.count((1001, "long")) == 1

    # 超长 trie（远超默认递归深度），确认全程无递归
    deep = {"deep": b"abcde" * 400}
    sc = Scanner(deep)
    sc.enable("deep")
    got = list(sc.feed(b"x" + b"abcde" * 400 + b"y"))
    check("ultra-deep-trie", got, [(2001, "deep")])

    # 大量 id 共享同一条 bytes：局部 BIT 路径与同位置排序
    p = {f"id{i:04d}": b"dup" for i in range(300)}
    sc = Scanner(p)
    for pid in p:
        sc.enable(pid)
    got = [pid for _, pid in sc.feed(b"dup")]
    check("mass-shared-pattern-count", len(got), 300)
    check("mass-shared-pattern-order", got, sorted(got))
    # 停用其中一半后数量精确减半且仍是字典序
    for pid in list(p)[:150]:
        sc.disable(pid)
    got = [pid for _, pid in sc.feed(b"dup")]
    check("mass-shared-half-disabled", (len(got), got == sorted(got)), (150, True))

    # 高频启停交错 + 逐字节喂入，与对照实现逐事件比对
    rng = random.Random(31337)
    base = {f"r{i}": bytes(rng.randrange(3) for _ in range(rng.randint(1, 4)))
            for i in range(20)}
    sc, ref = Scanner(base), ReferenceScanner(base)
    got, want = [], []
    stream = bytes(rng.randrange(3) for _ in range(3000))
    pos = 0
    while pos < len(stream):
        if rng.random() < 0.6:
            pid = rng.choice(list(base))
            if rng.random() < 0.5:
                sc.enable(pid); ref.enable(pid)
            else:
                sc.disable(pid); ref.disable(pid)
        n = rng.randint(1, 3)
        got.extend(sc.feed(stream[pos:pos + n]))
        want.extend(ref.feed(stream[pos:pos + n]))
        pos += n
    check("interleaved-toggle-byte-feed", got, want)

    # 单字节规则覆盖全部 256 个字节值
    full = {f"b{i:03d}": bytes([i]) for i in range(256)}
    sc = Scanner(full)
    for pid in full:
        sc.enable(pid)
    data = bytes(range(256))
    got = list(sc.feed(data))
    check("full-alphabet-256", len(got), 256)
    check("full-alphabet-positions", [e for e, _ in got], list(range(1, 257)))


if __name__ == "__main__":
    fixed_cases()
    random_differential()
    immutability_check()
    adversarial_cases()
    print("\nALL VERIFY PASSED")
