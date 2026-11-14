"""matcher.py — 可随时启停规则的流式多模式匹配器。

只使用 Python 标准库。

结构概述
========
1. 在 bytes 上构建 Aho-Corasick 自动机（稀疏 trie + failure 链）。
   构建期用一张临时 memo 表把每条 goto 计算降为均摊 O(1)，构建结束即
   释放；常驻只有 O(节点数) 的稀疏出边与 failure 数组。
2. 扫描期 goto 采用“稀疏出边 + failure 上溯”，以状态深度为势函数可证明
   处理 L 字节的转移总开销 O(L)（每次失败上溯使深度严格下降，而每字节
   深度至多 +1），且不随日志增长分配任何新状态。
3. failure 链构成一棵 failure 树。规则 id 挂在其末端 trie 节点上；
   当前状态 v 命中的规则，恰好是 failure 树上根 -> v 路径上的“已启用”
   末端节点。
4. 对 failure 树做重链剖分（HLD），根 -> v 路径被切成 O(log M) 个
   连续位置段（M 为 trie 节点数）。
5. 两个 Fenwick 树层级：
   - 全局 BIT：在 HLD 位置上记录“是否有启用 id 的末端节点”；
   - 每个末端节点一个局部 BIT：记录该节点下哪些 id 处于启用状态。
   启停只改 BIT，绝不重建自动机；全停用时扫描只走 goto，零枚举开销。

语义要点
========
- 匹配是否产出，只看匹配“结束那一刻”该 id 是否启用（feed 迭代期间
  禁止启停，由调用约定保证）。
- 同一 end 位置的多个 id 按 Unicode 码位字典序（Python str 默认序）输出。
- 自动机状态跨 feed 保留，因此跨 chunk 匹配天然支持。
- 不修改传入的 patterns。
"""

from collections import deque

_ALPHABET = 256  # bytes 的字节取值数，是常数


class _BIT:
    """Fenwick 树，下标从 0 开始，支持点更新 / 前缀和 / 第 k 个 1。"""

    __slots__ = ("n", "tree")

    def __init__(self, n):
        self.n = n
        self.tree = [0] * (n + 1)

    def add(self, i, delta):
        """对 0-based 位置 i 加 delta。"""
        i += 1
        n = self.n
        t = self.tree
        while i <= n:
            t[i] += delta
            i += i & -i

    def prefix(self, i):
        """闭区间 [0, i] 的和；i < 0 时返回 0。"""
        if i < 0:
            return 0
        i += 1
        s = 0
        t = self.tree
        while i > 0:
            s += t[i]
            i -= i & -i
        return s

    def kth(self, k):
        """返回满足 prefix(i) > k 的最小 0-based 下标 i（k 从 0 起）。"""
        idx = 0
        bit = 1 << (self.n.bit_length() - 1)
        t = self.tree
        n = self.n
        while bit:
            nxt = idx + bit
            if nxt <= n and t[nxt] <= k:
                idx = nxt
                k -= t[nxt]
            bit >>= 1
        # idx 是 Fenwick 的 1-based 位置减 1，即目标的 0-based 下标
        return idx


class Scanner:
    """流式多模式匹配器。

    参数
    ----
    patterns: {id: 非空 bytes}，id 为互异字符串；构造后不再读取或修改它。

    用法
    ----
    sc = Scanner({"A": b"aba"})     # 初始全部停用
    sc.enable("A")
    for end, pid in sc.feed(b"xaba"):
        ...                         # -> (4, "A")
    """

    def __init__(self, patterns):
        # ---------- 1. 构建 trie（并行数组表示） ----------
        nxt = [{}]          # nxt[u] = {字节: 子节点}
        fail = [0]          # failure 链
        end = [()]          # end[u] = 恰在 u 结束的 id 元组（按字典序）

        # 按 id 字典序遍历，保证每个 end[u] 元组天然有序
        for pid in sorted(patterns):
            pat = patterns[pid]
            u = 0
            for c in pat:
                v = nxt[u].get(c)
                if v is None:
                    v = len(nxt)
                    nxt[u][c] = v
                    nxt.append({})
                    fail.append(0)
                    end.append(())
                u = v
            end[u] = end[u] + (pid,) if end[u] else (pid,)

        m_nodes = len(nxt)

        # ---------- 2. failure 链（BFS）+ 构建期临时记忆化 ----------
        # memo 仅在本阶段存活：保证每条 goto 计算均摊 O(1)，构建完即释放。
        # 根节点对所有字节都有定义（无出边即回根），预先填满避免失败链误判。
        # fail[0] 用哨兵 -1：根的转移“命中根本身出边”与“沿 fail 上溯”必须区分
        memo = [None] * m_nodes
        root_memo = [0] * _ALPHABET
        for c, v in nxt[0].items():
            root_memo[c] = v
        memo[0] = root_memo
        fail[0] = -1
        order = [0]
        q = deque(order)
        while q:
            u = q.popleft()
            if memo[u] is None:
                memo[u] = {}
            for c, v in nxt[u].items():
                fail[v] = self._build_goto(memo, nxt, fail, fail[u], c)
                memo[u][c] = v
                order.append(v)
                q.append(v)
        fail[0] = 0  # 对外恢复成自指，failure 树构建与 HLD 路径终止均依赖此约定
        del memo     # 释放全部构建期缓存，常驻状态不保留 goto memo

        # ---------- 3. failure 树的邻接表 ----------
        children = [[] for _ in range(m_nodes)]
        for u in range(1, m_nodes):
            children[fail[u]].append(u)

        # ---------- 4. 重链剖分 ----------
        # BFS 序中父节点先于子节点，逆序即为自底向上
        size = [1] * m_nodes
        heavy = [-1] * m_nodes
        for u in reversed(order):
            best = 0
            for v in children[u]:
                size[u] += size[v]
                if size[v] > best:
                    best = size[v]
                    heavy[u] = v

        head = [0] * m_nodes
        pos = [0] * m_nodes
        at_pos = [0] * m_nodes
        cur = 0
        # 标准 HLD 位置分配：处理一个链头时，先完整走完整条重链
        # （同链节点位置必须连续），再处理沿途收集到的轻儿子链
        chain_starts = [0]
        while chain_starts:
            start = chain_starts.pop()
            u = start
            while u != -1:
                head[u] = start
                pos[u] = cur
                at_pos[cur] = u
                cur += 1
                hv = heavy[u]
                for v in children[u]:
                    if v != hv:
                        chain_starts.append(v)  # 轻儿子另起一链
                u = hv

        # ---------- 5. 两级 Fenwick 树 ----------
        global_bit = _BIT(m_nodes)
        # local_bits[u]：u 为末端节点时是局部 BIT，否则为 None
        local_bits = [None] * m_nodes
        node_active = [0] * m_nodes   # 每个末端节点当前启用的 id 数
        id_info = {}                  # id -> (末端节点, 局部下标)
        for u in range(m_nodes):
            if end[u]:
                ids = end[u]
                local_bits[u] = _BIT(len(ids))
                for li, pid in enumerate(ids):
                    id_info[pid] = (u, li)

        # 持久化到实例（不含任何 goto 缓存）
        self._nxt = nxt
        self._fail = fail
        self._end = end
        self._head = head
        self._pos = pos
        self._at_pos = at_pos
        self._global_bit = global_bit
        self._local_bits = local_bits
        self._node_active = node_active
        self._id_info = id_info
        self._enabled = set()
        self._state = 0          # 当前自动机状态
        self._total = 0          # 已消费的累计字节数

    # ------------------------------------------------------------------
    # 自动机转移
    # ------------------------------------------------------------------
    @staticmethod
    def _build_goto(memo, nxt, fail, u, c):
        """构建期 goto：临时 memo 保证均摊 O(1)，可识别 fail 哨兵 -1。"""
        start = u
        while True:
            if u == -1:
                # 已越过根：根没有 c 的真实出边（真实出边在 memo[0] 中），结果为根
                ans = 0
                break
            mu = memo[u]
            # 根节点的 memo 是填满的 list；其余节点是 dict
            cached = mu[c] if isinstance(mu, list) else mu.get(c)
            if cached is not None:
                ans = cached
                break
            direct = nxt[u].get(c)
            if direct is not None:
                ans = direct
                break
            u = fail[u]
        # 沿本次走过的 failure 路径统一记忆化（根节点已全量缓存，跳过）
        w = start
        while w not in (u, -1):
            memo[w][c] = ans
            w = fail[w]
        if u > 0 and memo[u].get(c) is None:
            memo[u][c] = ans
        return ans

    def _goto(self, u, c):
        """扫描期 goto：稀疏出边 + failure 上溯，无额外缓存、无新分配。

        摊还 O(1)/字节：设 d(u) 为 trie 深度。每次沿 fail 上溯都使深度
        严格下降，而每个字节转移后深度至多增加 1，故 L 个字节内上溯总
        步数不超过 O(L)。
        """
        nxt = self._nxt
        fail = self._fail
        while True:
            v = nxt[u].get(c)
            if v is not None:
                return v
            if u == 0:
                return 0
            u = fail[u]

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def enable(self, id):
        """启用规则；幂等。"""
        if id in self._enabled:
            return
        self._enabled.add(id)
        u, li = self._id_info[id]
        self._local_bits[u].add(li, 1)
        self._node_active[u] += 1
        if self._node_active[u] == 1:
            # 该节点从无启用 id 变为有：挂上 failure 树路径
            self._global_bit.add(self._pos[u], 1)

    def disable(self, id):
        """停用规则；幂等。"""
        if id not in self._enabled:
            return
        self._enabled.discard(id)
        u, li = self._id_info[id]
        self._local_bits[u].add(li, -1)
        self._node_active[u] -= 1
        if self._node_active[u] == 0:
            self._global_bit.add(self._pos[u], -1)

    # ------------------------------------------------------------------
    # 输出枚举：failure 树根 -> state 路径上所有启用的末端节点
    # ------------------------------------------------------------------
    def _emit(self, state):
        gbit = self._global_bit
        lbits = self._local_bits
        head = self._head
        pos = self._pos
        at_pos = self._at_pos
        end = self._end
        node_active = self._node_active
        out = []
        v = state
        while True:
            h = head[v]
            lo = pos[h]
            hi = pos[v]
            base = gbit.prefix(lo - 1)
            seg_total = gbit.prefix(hi) - base
            for j in range(seg_total):
                u = at_pos[gbit.kth(base + j)]
                lbit = lbits[u]
                cnt = node_active[u]
                for li in range(cnt):
                    out.append(end[u][lbit.kth(li)])
            if h == 0:
                break
            v = self._fail[h]
        return out

    # ------------------------------------------------------------------
    # 流式喂入
    # ------------------------------------------------------------------
    def feed(self, chunk):
        """惰性产出 (end, id)；end 为从 0 起累计的匹配末端后一字节位置。

        约定：调用者耗尽当前生成器后才允许 enable/disable 或再次 feed。
        空 chunk 自然无输出。
        """
        base = self._total
        state = self._state
        goto = self._goto
        emit = self._emit
        active = len(self._enabled)
        for i, c in enumerate(chunk):
            state = goto(state, c)
            self._state = state
            if active:
                hits = emit(state)
                if hits:
                    hits.sort()  # 同位置按 id 的 Unicode 字典序
                    end_pos = base + i + 1
                    for pid in hits:
                        yield (end_pos, pid)
        self._total += len(chunk)
