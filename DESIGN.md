# 可启停流式多模式匹配器 — 设计与复杂度说明

仅依赖 Python 标准库。核心实现在 `matcher.py`，朴素对照在
`reference_matcher.py`。

## 1. 数据结构与不变量

设规则数为 P、规则总字节数为 N、trie 节点数为 M（M ≤ N+1）。

| 结构 | 形态 | 作用 | 规模 |
|---|---|---|---|
| `nxt` | `list[dict]` | 稀疏 trie 出边，总条目 M−1 | O(M) |
| `fail` | `list[int]` | failure 链；`fail[0]=0` | O(M) |
| `end` | `list[tuple[str,...]]` | 恰在该节点结束的 id，元组按 Unicode 字典序 | O(P) |
| failure 树 `children` | 邻接表（构建用） | HLD 的输入 | O(M) |
| `head/pos/at_pos` | 三个整型 list | 重链剖分：链头、HLD 位置、位置反查 | O(M) |
| 全局 Fenwick | 长度 M | HLD 位置上“该末端节点是否有启用 id” | O(M) |
| 局部 Fenwick | 每末端节点一个 | 该节点下哪些 id 启用；总长度 ≤ P | O(P) |
| `id_info` | dict | id → (末端节点, 局部下标) | O(P) |
| `_enabled` | set | 当前启用 id（幂等判定） | O(P) |
| `_state / _total` | 标量 | 自动机当前状态、累计字节数 | O(1) |

关键不变量：

1. **命中即路径**。AC 自动机读到状态 v 时，所有以当前位置结尾的模式，
   其末端节点恰好是 failure 树上根 → v 路径上的末端节点。
2. **全局 BIT 只记 0/1 型节点活性**。末端节点 u 从“无启用 id”变为
   “有启用 id”时在 `pos[u]` +1，归零则 −1；同节点多 id 只产生一次
   全局翻转，局部 BIT 负责节点内的多 id 枚举。
3. **HLD 路径分段**。根 → v 路径被切成 O(log M) 段，每段在 HLD 位置
   轴上是连续区间，可用全局 BIT 的前缀和 + `kth` 精确枚举区间内的
   活性末端节点，不触碰任何停用节点、也不枚举停用后缀链。
4. **状态不随日志增长**。扫描期 goto 只读取 `nxt/fail`，不写入、不
   分配；构建期的 goto memo 在构造结束时 `del` 释放。

## 2. 构建流程与 O(N log(N+2))

1. 按 id 的 Unicode 字典序插入 trie：O(N) 期望时间（dict 查找），
   同时让每个 `end[u]` 元组天然有序，免去后续排序。
2. BFS 计算 failure 链。构建期使用临时 memo 表，使每条 goto 计算
   均摊 O(1)；memo 在构建结束后立即释放，不进入常驻状态。
3. failure 树自底向上求子树大小与重儿子（复用 BFS 逆序），再做
   标准 HLD 位置分配：O(M)。
4. 建立两级 Fenwick 与 id 索引：O(M+P)。

总计 O(N+M+P) = **O(N)** 期望（满足 O(N log(N+2)) 的要求）；
即使对 dict 做保守的对数界估计，也仍在 O(N log(N+2)) 内。

## 3. 启停 O(log²(N+2))

`enable/disable` 幂等（先查 `_enabled`）。一次有效启停：

- 局部 BIT 点更新：O(log d_u)，d_u 为同末端节点的 id 数；
- 仅当节点活性翻转时，全局 BIT 点更新一次：O(log M)。

两者合计 O(log M+log P) ⊆ **O(log²(N+2))**（实际为 O(log N)，
优于要求的上界）。不重建自动机、不扫描任何规则或后缀链。
幂等空操作为 O(1) 集合判定，实测见 benchmark 的
`avg_idempotent_noop_us`。

## 4. feed 与 O((L+1)log²(N+2) + K log(N+2))

### 转移

扫描期 goto 为“稀疏出边 + failure 上溯”，无缓存写入。以状态深度
d(·) 为势函数：每个字节成功转移使深度至多 +1；每次 failure 上溯使
深度严格下降至少 1。因此 L 个字节内上溯总步数 O(L)，转移合计
**O(L) 摊还**。空 chunk 走一次生成器入口即返回，对应式中的 +1。

### 输出枚举（仅当存在启用 id 时才进入）

对根 → v 的 O(log M) 个 HLD 段，每段做两次 BIT 前缀和定位区间内
活性节点数，再用 `kth` 逐个取出：每段 O(log M) 定位 + 与段内活性
节点数线性相关的枚举。全局层面每字节 O(log² M)；每个被产出的匹配
再做一次局部 BIT 的 `kth`（O(log d_u)）。

### 同位置排序

一个位置收集到的命中 id 在内存中排序。设该位置命中 k 个 id，则
Σ k log k ≤ K log(P)，整体并入 **K log(N+2)** 项。

### 结束时刻语义

自动机状态在每个字节处理后立即写回 `_state`，且 feed 是生成器、
约定调用者耗尽当前迭代器后才启停，因此“按匹配结束时启用状态判定”
天然成立——停用期间读入的前缀仍留在自动机里，启用后到达末端节点
即可正常产出（题目示例 `aba` 的行为由 `verify.py` 固定覆盖）。

### 全停用快速路径

`len(_enabled)==0` 时每字节只做 goto，完全跳过 HLD/BIT 枚举，
停用的海量规则对扫描速度零额外开销。

## 5. 空间 O(N+P)

常驻结构见第 1 节表，全部为 O(M) 或 O(P)，M ≤ N+1，故总状态
**O(N+P)**，与累计日志长度无关。`benchmark.py` 的
`state_stability` 在持续喂入 33 万+ 字节后复查驻留条目数，
`entries_grew=0`。累计字节位置 `_total` 只是一个整数。

## 6. 正确性验证

- `verify.py`：18 个确定性用例（题目示例、重叠、跨 chunk、同内容
  多 id、幂等、全字节域含 0x00/0xFF、Unicode id 序、不可变性等）
  + 400 轮随机差分（随机规则、随机字节、随机启停与分块，逐事件
  对比 `ReferenceScanner`）。
- `reference_matcher.py`：独立朴素实现（无界缓冲 + 逐规则切片
  比较），与正式实现无共享逻辑，仅作对照。
- `benchmark.py` 内嵌一次差分健全性检查。

## 7. 复现方式

```bash
python3 -I run_all.py        # 依次跑 verify/demo/benchmark 并写清单
python3 -I verify.py         # 仅验证
python3 -I demo.py           # 仅演示
python3 -I benchmark.py      # 仅实测（JSON 打到 stdout）
```

运行清单写入 `.pointwise-runtime.json`，各任务完整输出在 `logs/`。
所有命令使用 `-I` 隔离模式，不受当前目录环境变量与第三方包影响。
