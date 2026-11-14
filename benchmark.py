"""benchmark.py — 规模实测，输出结构化 JSON 到 stdout。

实测维度：
1. 构建耗时与自动机节点数（多档 P / N）；
2. 单次 enable/disable 耗时（含幂等空操作）；
3. 全停用扫描吞吐（停用规则不得拖慢扫描）；
4. 全启用扫描吞吐与每字节命中数（K 驱动的开销）；
5. 状态不随累计日志增长：持续喂入，驻留节点数保持恒定。

运行：python3 -I benchmark.py
"""

import gc
import json
import os
import random
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from matcher import Scanner
from reference_matcher import ReferenceScanner


def make_patterns(p, avg_len, seed):
    """生成共享前缀较多的随机 bytes 规则，逼近最坏情形的 trie 规模。"""
    rng = random.Random(seed)
    patterns = {}
    for i in range(p):
        ln = max(1, int(rng.gauss(avg_len, avg_len * 0.4)))
        patterns[f"id{i:06d}"] = bytes(rng.randrange(256) for _ in range(ln))
    return patterns


def timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    ret = fn(*args, **kwargs)
    return time.perf_counter() - t0, ret


def bench_build(sizes):
    rows = []
    for p, avg in sizes:
        patterns = make_patterns(p, avg, seed=1000 + p)
        n_bytes = sum(map(len, patterns.values()))
        gc.collect()
        t0 = time.perf_counter()
        sc = Scanner(patterns)
        dt = time.perf_counter() - t0
        rows.append({
            "P": p,
            "N_bytes": n_bytes,
            "trie_nodes": len(sc._nxt),
            "build_sec": round(dt, 4),
            "us_per_N_byte": round(dt / n_bytes * 1e6, 3),
        })
    return rows


def bench_toggle(sc, ids, rounds):
    rng = random.Random(7)
    enable_t, disable_t, noop_t = 0.0, 0.0, 0.0
    for _ in range(rounds):
        pid = rng.choice(ids)
        t0 = time.perf_counter()
        sc.enable(pid)
        enable_t += time.perf_counter() - t0
        t0 = time.perf_counter()
        sc.enable(pid)  # 幂等空操作
        noop_t += time.perf_counter() - t0
        t0 = time.perf_counter()
        sc.disable(pid)
        disable_t += time.perf_counter() - t0
    return {
        "rounds": rounds,
        "avg_enable_us": round(enable_t / rounds * 1e6, 3),
        "avg_disable_us": round(disable_t / rounds * 1e6, 3),
        "avg_idempotent_noop_us": round(noop_t / rounds * 1e6, 3),
    }


def bench_scan_disabled(sc, length, seed):
    rng = random.Random(seed)
    data = bytes(rng.randrange(256) for _ in range(length))
    # 分多个 chunk 喂入，同时覆盖跨 chunk 状态保持
    t0 = time.perf_counter()
    total_out = 0
    pos = 0
    while pos < length:
        step = min(4096, length - pos)
        total_out += sum(1 for _ in sc.feed(data[pos:pos + step]))
        pos += step
    dt = time.perf_counter() - t0
    return {
        "L_bytes": length,
        "outputs": total_out,
        "scan_sec": round(dt, 4),
        "MB_per_s": round(length / dt / 1e6, 2),
        "us_per_byte": round(dt / length * 1e6, 3),
    }


def bench_scan_enabled(sc, patterns, length, seed):
    rng = random.Random(seed)
    ids = list(patterns)
    for pid in ids:
        sc.enable(pid)
    # 文本由随机规则片段拼成，制造大量真实命中（含重叠）
    blob = bytearray()
    while len(blob) < length:
        blob += patterns[rng.choice(ids)]
    data = bytes(blob[:length])
    t0 = time.perf_counter()
    total_out = 0
    pos = 0
    while pos < length:
        step = min(4096, length - pos)
        total_out += sum(1 for _ in sc.feed(data[pos:pos + step]))
        pos += step
    dt = time.perf_counter() - t0
    return {
        "L_bytes": length,
        "K_outputs": total_out,
        "scan_sec": round(dt, 4),
        "MB_per_s": round(length / dt / 1e6, 2),
        "us_per_byte": round(dt / length * 1e6, 3),
        "ns_per_output": round(dt / total_out * 1e9, 2) if total_out else None,
    }


def bench_state_stable(patterns, feed_rounds, chunk_len, seed):
    """状态驻留不随累计日志增长：构建前启动 tracemalloc，对比首末两次快照。"""
    rng = random.Random(seed)
    data = bytes(rng.randrange(256) for _ in range(chunk_len))

    def resident(sc):
        # 统计核心结构中 list/dict/set 的 Python 对象驻留条数
        total = 0
        for name in ("_nxt", "_fail", "_end", "_head", "_pos",
                     "_at_pos", "_local_bits", "_node_active"):
            obj = getattr(sc, name)
            total += len(obj)
        total += len(sc._enabled)
        total += len(sc._id_info)
        # 稀疏出边字典中的条目总数（应恒等于 trie 节点数 - 1）
        total += sum(len(d) for d in sc._nxt)
        return total

    tracemalloc.start()
    sc = Scanner(patterns)
    list(sc.feed(data))  # 先消化一个 chunk，消除一次性热路径分配
    snap1 = tracemalloc.take_snapshot()
    size1 = resident(sc)
    for _ in range(feed_rounds):
        list(sc.feed(data))
    snap2 = tracemalloc.take_snapshot()
    size2 = resident(sc)
    cur1 = sum(s.size for s in snap1.statistics("filename"))
    cur2 = sum(s.size for s in snap2.statistics("filename"))
    tracemalloc.stop()
    return {
        "feed_rounds": feed_rounds + 1,
        "cumulative_bytes": (feed_rounds + 1) * chunk_len,
        "resident_entries_first": size1,
        "resident_entries_last": size2,
        "entries_grew": size2 - size1,
        "tracemalloc_bytes_first": cur1,
        "tracemalloc_bytes_last": cur2,
        "malloc_growth_bytes": cur2 - cur1,
    }


def differential_sanity():
    """实测脚本里也跑一次小规模差分，保证实测对象行为正确。"""
    rng = random.Random(99)
    patterns = {f"p{i}": bytes(rng.randrange(4) for _ in range(rng.randint(1, 5)))
                for i in range(12)}
    sc, ref = Scanner(patterns), ReferenceScanner(patterns)
    got, want = [], []
    text = bytes(rng.randrange(4) for _ in range(2000))
    for i in range(0, len(text), 37):
        if rng.random() < 0.5:
            pid = rng.choice(list(patterns))
            if rng.random() < 0.5:
                sc.enable(pid); ref.enable(pid)
            else:
                sc.disable(pid); ref.disable(pid)
        chunk = text[i:i + 37]
        got.extend(sc.feed(chunk))
        want.extend(ref.feed(chunk))
    assert got == want, "benchmark 内差分失败"
    return {"checked_outputs": len(got), "match": True}


def main():
    result = {"python": sys.version.split()[0]}

    print("[1/5] 构建实测 ...", file=sys.stderr)
    result["build"] = bench_build([
        (100, 8), (1_000, 10), (10_000, 12), (50_000, 12),
    ])

    big_p = 10_000
    patterns = make_patterns(big_p, 12, seed=4242)
    sc = Scanner(patterns)
    ids = list(patterns)

    print("[2/5] 启停实测 ...", file=sys.stderr)
    result["toggle"] = bench_toggle(sc, ids, rounds=20_000)
    # 确保测试结束后回到全停用
    for pid in ids:
        sc.disable(pid)

    print("[3/5] 全停用扫描实测 ...", file=sys.stderr)
    result["scan_all_disabled"] = bench_scan_disabled(sc, length=200_000, seed=5)

    print("[4/5] 全启用扫描实测 ...", file=sys.stderr)
    result["scan_all_enabled"] = bench_scan_enabled(
        Scanner(patterns), patterns, length=50_000, seed=6)

    print("[5/5] 状态稳定性 + 差分 ...", file=sys.stderr)
    stable_patterns = make_patterns(2_000, 10, seed=77)
    result["state_stability"] = bench_state_stable(
        stable_patterns, feed_rounds=40, chunk_len=8_192, seed=8)
    result["differential_sanity"] = differential_sanity()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
