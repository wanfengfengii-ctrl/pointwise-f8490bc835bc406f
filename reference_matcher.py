"""reference_matcher.py — 独立朴素对照实现。

刻意采用与 matcher.Scanner 完全不同的实现策略：
- 维护无界累积缓冲；
- 每个字节用 bytes.find / 切片直接检查每条启用规则是否在当前位置结束；
- 启停语义、跨 chunk、重叠、同位置字典序等行为与正式实现保持一致。

仅用于小规模正确性对照，不参与任何复杂度承诺。
"""


class ReferenceScanner:
    def __init__(self, patterns):
        # 复制一份，避免外部变化影响；正式实现也不读外部对象
        self._pats = {pid: bytes(b) for pid, b in patterns.items()}
        self._enabled = set()
        self._buf = bytearray()

    def enable(self, id):
        if id not in self._enabled:
            self._enabled.add(id)

    def disable(self, id):
        self._enabled.discard(id)

    def feed(self, chunk):
        start = len(self._buf)
        self._buf.extend(chunk)
        active = [pid for pid in self._pats if pid in self._enabled]
        # 匹配以“结束时的启用状态”判定：迭代期间启停是被禁止的
        for i in range(start, len(self._buf)):
            hits = []
            for pid in active:
                p = self._pats[pid]
                if i + 1 >= len(p) and bytes(self._buf[i + 1 - len(p): i + 1]) == p:
                    hits.append(pid)
            if hits:
                hits.sort()  # Unicode 字典序
                end_pos = i + 1
                for pid in hits:
                    yield (end_pos, pid)
