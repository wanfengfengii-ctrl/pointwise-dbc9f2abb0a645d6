# 历史训练特征的局部回填设计

本轮只设计、不实现增量器。原有 `observe.py`、`warehouse.py`、`source.py`、`features.py`、
`checks.py` 与样例全部保留；新增的只有本文和运行清单的 `verify` 入口。

## 1. 目标与计算语义

每个样本是 `[sample_id, entity, T, C]`：`T` 是预测时刻，`C` 是知识截止（cutoff）。
每条版本是 `[id, rev, entity, event_time, available_at, amount, deleted]`，版本身份是
`(id, rev)`。对一个样本，对每个事件 `id`：

1. 只在 `available_at <= C` 的版本中选 `rev` 最大的版本；墓碑（`deleted` 为真）参与这次选择；
2. 选中的版本只有同时满足“未删除、`entity` 等于样本实体、`T-86400 <= event_time < T`”
   才贡献 `(amount, 1)`，否则贡献 `(0, 0)`；
3. 汇总所有 `id` 的贡献。零金额仍贡献一笔，因此 `count` 增加；没有事件、空窗口或所有事件
   都不满足条件时结果是 `[sample_id, 0, 0]`。

区间端点固定如下：

| 条件 | 端点 | 含义 |
| --- | --- | --- |
| 知识截止 | `available_at <= C` | 恰在 `C` 可用的版本可见 |
| 事件窗口下界 | `event_time >= T - 86400` | 下界闭 |
| 事件窗口上界 | `event_time < T` | 上界开 |
| 胜出区间 | `[lo, hi)`，`hi` 为空表示 `+inf` | `lo` 闭，`hi` 开 |

现有参照实现就在这些位置：`features.py:3` 的 `contribution` 实现步骤 2，
`features.py:17` 到 `features.py:20` 的循环实现步骤 1（`available_at <= cutoff`、
`rev > winners[rid][1]`），`features.py:12` 的 `compute` 对全部样本逐个执行。
`checks.py:8` 的 `oracle` 是测试专用、故意慢的逐样本参照，选择与贡献语义必须与它一致。

## 2. 现有调用链与接入点

当前一次批处理的链路是：

1. `observe.py:9` 的 `run_case` 创建 `Warehouse`，在 `observe.py:14` 对每个 batch 调用
   `apply`；
2. `warehouse.py:12` 的 `apply` 先 `read()` 读取批前特征（`warehouse.py:8`），
   然后在 `warehouse.py:15` 的 `with self.db:` 事务中：
   `source.import_versions`（`source.py:48`）校验、查重、冲突回滚并插入版本；
   `source.all_versions`（`source.py:66`）全表读版本；
   `source.all_samples`（`source.py:71`）全表读样本；
   `features.compute`（`features.py:12`）全量重算；
   最后逐行 `UPDATE features`（`warehouse.py:20`）；
3. `warehouse.py:25` 和 `warehouse.py:29` 用批前 `before` 对比批后结果，得到 `changed`；
4. 观察入口在 `observe.py:25` 输出。

局部回填的接入点是 `warehouse.py:15` 事务内部的 `apply` 主体：保留
`source.import_versions`（它已经实现“同身份同内容重放忽略、异内容整批
`REVISION_CONFLICT`”），把 `source.py:66` 和 `source.py:71` 两次全表读取及
`features.py:12` 的全量 `compute` 替换为“只处理本批 id → 求胜出区间差 → 检索候选样本 →
只重算候选”的路径。`features.compute` 与 `checks.py:8` 的逐样本语义继续作为正确性参照，
新路径复用 `features.py:3` 的 `contribution`，不另写一套窗口判断。
`warehouse.py:8` 的 `read()` 只保留给观察/展示输出；候选和 `changed` 不得靠它取得。

## 3. 胜出函数与胜出区间

对事件 id 定义

```
W_id(C) = argmax_{v: v.id=id, v.available_at <= C} v.rev
```

没有合格版本时 `W_id(C) = None`。因为 `available_at` 与 `rev` 顺序无关，`W_id` 是
随 `C` 单调推进的分段常函数：把该 id 的版本按 `(available_at, rev)` 排序，只有“刷新了
历史最大 rev”的版本才会成为新区间的胜者；相同 `available_at` 的多个版本中只有最高 rev
能成为胜者。胜出区间表为：

```sql
CREATE TABLE winner_spans(
  rid         TEXT    NOT NULL,
  lo          INTEGER NOT NULL,   -- 闭区间起点
  hi          INTEGER,            -- 开区间终点；NULL 表示 +inf
  rev         INTEGER NOT NULL,
  entity      TEXT    NOT NULL,
  event_time  INTEGER NOT NULL,
  amount      INTEGER NOT NULL,
  deleted     INTEGER NOT NULL,
  PRIMARY KEY(rid, lo)
);
```

`load_spans(touched)` 以 `rid` 主键前缀读取本批 id 的旧区间，不需要额外索引。
（若未来要做“按实体审计某时刻的胜者”，可另加 `(entity, event_time, lo)` 索引；
它不在本方案的每批访问路径上。）

对每个 id，区间从第一个成为胜者的 `available_at` 开始，`hi` 是下一个刷新最大 rev 的
`available_at`；第一个区间之前没有合格版本，最后一个区间的 `hi` 为 `NULL`。墓碑也被
存成一个区间（`deleted=1`），因为“胜者是墓碑”与“没有版本”是两种不同状态，后续版本
必须替换的是墓碑这个胜者。

区间重建（首次全量或对少量 id 增量重建）的规则：

```python
def build_spans(versions_of_one_id):
    # 必须先按 available_at 排序，rev 只用于同一时刻取最高
    ordered = sorted(versions_of_one_id, key=lambda v: (v.available_at, v.rev))
    spans, best = [], None
    for v in ordered:
        if best is None or v.rev > best.rev:
            if spans and spans[-1].lo == v.available_at:
                spans.pop()                 # 同一 available_at，高 rev 覆盖低 rev
            spans.append(Span(lo=v.available_at, hi=None, version=v))
            best = v
    for i in range(len(spans) - 1):
        spans[i].hi = spans[i + 1].lo
    return spans
```

这个构造直接体现“不能先过滤墓碑/实体/事件时间再选版本”：过滤只发生在选出胜者之后的
`contribution` 里。`winner_spans` 的每一行只记录胜者版本，不记录“当前全局最高 rev”，
因此低版本迟到也能在前缀区间成为胜者。

### 首次全量构建（只做一次）

首次部署允许全量扫描一次版本表来建辅助索引：

```python
def bootstrap_spans(db):
    cur = db.execute("""
        SELECT id, rev, entity, event_time, available_at, amount, deleted
        FROM versions ORDER BY id, available_at, rev
    """)                                  # 唯一一次全量事件扫描
    for rid, group in groupby(cur, key=lambda r: r[0]):
        for s in build_spans(list(group)):
            db.execute("INSERT INTO winner_spans VALUES(?,?,?,?,?,?,?,?)", ...)
```

之后再建 `versions_by_id_available`、`versions_by_entity_event`、
`samples_by_entity_time` 等索引；索引自身的首次建立会各扫一次表，这是一次性成本，
不是每批成本。

### 后续增量维护（只碰本批 id）

一批只会改变本批涉及的 id 的胜出函数，因此：

```python
touched = {row[0] for row in batch_rows}
old_spans = SELECT * FROM winner_spans WHERE rid IN touched ORDER BY rid, lo
DELETE FROM winner_spans WHERE rid IN touched
for rid in touched:
    versions = SELECT * FROM versions WHERE id=? ORDER BY available_at, rev
    for s in build_spans(versions):
        INSERT INTO winner_spans VALUES(...)
```

`DELETE`/`INSERT` 都以 `rid` 为主键前缀，读取版本只走本批 id；不触碰其他 id，
也不扫描全部事件或全部样本。批中含有同一 id 的多个版本时，这里一次性用批后完整集合
重建，绝不逐行重建。

## 4. 批事务与“批前/批后完整状态”

`Warehouse.apply` 的事务包住版本导入、辅助索引、特征物化三件事（`warehouse.py:15`）。
关键顺序如下：

```python
def apply(batch_rows):
    validate_all(batch_rows)                     # source.py:19 的规则
    with db:                                     # warehouse.py:15，异常整批回滚
        accepted = import_versions(db, batch_rows)   # source.py:48

        touched = {row[0] for row in batch_rows}
        old_spans = load_spans(touched)          # 批前完整状态
        new_spans = build_all_spans_from_versions(touched)  # 批后完整状态（含本批所有行）

        changes = diff_spans(old_spans, new_spans)       # [(rid, lo, hi, old_w, new_w)]
        if not changes:
            return accepted, read(), [], trace   # 重放或完全遮蔽：无候选

        replace_spans(touched, new_spans)

        candidates = probe_candidates(changes)   # 5 节，只用索引
        before = read_feature_values(candidates) # 只读候选的旧聚合值
        for sid in candidates:
            total, n = reaggregate(sid)          # 7 节
            UPDATE features SET total=?, n=? WHERE sample_id=?
        changed = [sid for sid in sorted(candidates)
                   if (total, n) differs from before[sid]]
```

要点：

* `import_versions` 先把整批写入。冲突在写入过程中抛出，`with db` 回滚版本、区间和特征，
  与现有回归一致（`checks.py:61` 到 `checks.py:76` 验证冲突后 `read()` 和 `versions`
  都回到批前）。
* `old_spans` 虽然是在 `import_versions` 之后读的，但读的是**尚未更新**的
  `winner_spans` 表，所以它仍是批前完整状态；`new_spans` 直接用“版本表批后内容”重建。
  二者都不依赖同批行的导入顺序。
* 同身份同内容重放不插入新行，`accepted` 不增加，批前/批后集合相同，`changes` 为空，
  候选为空；这对应 `normal` 的 `replay` 和 `history` 的 `replay`。
* 同批多个版本在 `build_all_spans_from_versions` 中作为一个完整集合处理，不产生中间
  候选。例如 `shadowed` 的 `x rev6` 与 `x rev7` 同一 `available_at`，最终只有 rev7
  是胜者，不能出现“rev6 短暂胜出”的候选。
* `changed` 用的是候选样本的批前物化值与批后重算值；读的是候选主键行，不是全表
  `read()`。`read()` 在返回值里仍可用于展示完整特征，这是允许的观察输出。

## 5. 胜出区间变化与候选检索

把 `diff_spans` 的结果展开成“每个发生变化的一侧”临时表：

```sql
CREATE TEMP TABLE batch_change(
  rid        TEXT NOT NULL,
  lo         INTEGER NOT NULL,
  hi         INTEGER,
  side       TEXT NOT NULL,       -- 'old' 或 'new'，只用于审计/策略 A
  rev        INTEGER NOT NULL,
  entity     TEXT NOT NULL,
  event_time INTEGER NOT NULL,
  amount     INTEGER NOT NULL,
  PRIMARY KEY(rid, lo, side)
);
CREATE INDEX batch_change_probe ON batch_change(entity, event_time);
```

策略 B 只用 `entity/event_time/lo/hi`；`rev/amount` 留给策略 A 算 delta 和审计。
`batch_change` 是每批的小临时表，不做持久化。

`diff_spans` 自己只处理本批 id 的区间。对两个区间列表，取双方 `lo` 端点（以及最后一侧
的 `None` 收尾）排序后逐段比较：

```python
def diff_spans(old, new):
    points = sorted({s.lo for s in old} | {s.lo for s in new})
    old_at, new_at = index_spans(old), index_spans(new)  # 二分：lo <= C < hi
    for p, q in pairs(points):          # q 是下一个端点，最后一个为 None
        a, b = winner_at(old_at, p), winner_at(new_at, p)
        if a != b:                      # 版本身份/内容不同即变化
            yield Change(rid, lo=p, hi=q, old=a, new=b)
```

`q` 是相邻的下一个端点，所以 `[p, q)` 上旧、新胜者都各自保持不变，整段用一个变化记录
表示；`q=None` 表示延伸到 `+inf`。`winner_at` 做区间端点判断：`lo <= C < hi`
（`hi=None` 视为无穷）。然后只把“批前或批后
至少一侧能贡献”的胜者写入 `batch_change`：墓碑一侧不写，因为它不贡献；非墓碑一侧写
`(entity, event_time, lo, hi)`。候选检索 SQL 为：

```sql
SELECT DISTINCT s.sample_id
FROM batch_change c
JOIN samples s
  ON s.entity = c.entity
 AND s.prediction_time >  c.event_time
 AND s.prediction_time <= c.event_time + 86400WHERE s.cutoff >= c.lo
  AND (c.hi IS NULL OR s.cutoff < c.hi)
ORDER BY s.sample_id;
```

四个条件恰好是“该侧胜者对这个样本能贡献一笔”：实体相等；`event_time` 落在
`[T-86400, T)`（写成 `T > event_time` 且 `T <= event_time+86400`）；样本的 `C` 落在
胜者发生变化的 `[lo, hi)`。墓碑侧不满足“未删除”，所以不产生候选；两侧都墓碑的区间
整段跳过。

需要的索引及列顺序：

```sql
CREATE INDEX versions_by_id_available   ON versions(id, available_at, rev);
CREATE INDEX versions_by_entity_event   ON versions(entity, event_time, id);
CREATE INDEX samples_by_entity_time     ON samples(entity, prediction_time, cutoff);
```

`samples_by_entity_time` 让候选查询以 `entity` 等值前缀进入，再对 `prediction_time` 做
B-tree 范围扫描；`cutoff` 作为第三列做 SQL 残余过滤（SQLite 一个索引只能有一个范围
条件，`cutoff` 不能同时成为第二个范围）。因此每次变化侧的检索成本是
`O(log N + 命中的候选数)`，而不是全表样本扫描。`batch_change_probe` 用于让变化侧作为
外层循环，按实体做等值探测。

一个区间两侧都非墓碑时，两侧分别检索；候选用 `SELECT DISTINCT`/集合去重，最后按
`sample_id` 排序。候选包含“贡献抵消、同金额换版”的样本是允许的保守多选；候选不等于
`changed`，`changed` 是重算后数值真正不同的子集。

## 6. 两种局部回填策略及取舍

### 策略 A：直接调整贡献（delta）

`diff_spans` 已经给出旧胜者 `a` 和新胜者 `b`。对候选样本直接算

```
d_total = contribution(b, sample).amount - contribution(a, sample).amount
d_n     = contribution(b, sample).count  - contribution(a, sample).count
UPDATE features SET total = total + d_total, n = n + d_n WHERE sample_id = ?
```

它无须重读该样本窗口内的其他事件：一个事件胜者变化对聚合值的影响只由旧的
`contribution(a)` 与新的 `contribution(b)` 决定，其余事件的贡献没有变化。优点是非常省
读取；缺点是不能只靠区间“大致命中”：同一候选可能被多个区间/多个 id 命中，必须先把
所有 delta 累加到该样本再写一次；`total` 是十进制文本（`source.py:13`），必须先在
Python 整数中累加；一旦漏掉一个变化区间，误差会永久留在物化表里，后续查询无法自愈。
此外要保证旧胜者 `a` 在批后已被删除的区间里仍可取到其旧贡献，必须保留旧区间快照
（上面的 `old_spans` 在覆盖前读取）。它对实现细节很敏感，正确性论证成本高。

### 策略 B：候选重汇总（推荐）

先用同一套 `batch_change` 检索候选，再对每个候选样本按 §1 的完整语义重算：

```python
def reaggregate(sample):
    ids = SELECT DISTINCT id FROM versions
          WHERE entity = sample.entity
            AND event_time >= sample.T - 86400
            AND event_time <  sample.T          # 索引 versions_by_entity_event
    rows = SELECT * FROM versions
           WHERE id IN ids AND available_at <= sample.C  # 索引 versions_by_id_available
    winners = {}
    for v in rows:
        if v.id not in winners or v.rev > winners[v.id].rev:
            winners[v.id] = v
    total = n = 0
    for v in winners.values():
        amount, count = contribution(v, sample)  # features.py:3，复用原语义
        total += amount; n += count
    return total, n
```

**推荐 B。** 理由：

1. 它对“同批多版本、墓碑、实体/时间移动、贡献抵消”天然正确：每次都从胜者重新应用
   `contribution`，不依赖“旧值一定等于新值减 delta”的簿记；
2. 它可以直接复用 `features.py:3` 的 `contribution`，与 `features.py:12` 的全量参照
   保持同一语义，差异只在于“算哪些样本”而不是“怎么算”；
3. 候选数由变化区间命中数界定，通常远小于样本总量；每个候选只读自己窗口内的事件，
   不会退化成全表扫描。代价是该窗口的事件都要读一遍，换来无需维护 delta 漂移。

A 适合候选极少、追求写入/读取最小化的场景；本题把正确性放在首位，选 B。两种策略只在
“候选样本如何更新特征”上不同，候选检索（§5）和胜出区间维护（§3）相同。

## 7. 候选重汇总的访问边界与防错

`reaggregate` 的两步都必须避免全表：

* 第一步只按 `(entity, event_time)` 找“窗口内至少有一个版本”的 id。窗口外的 id 不可能
  贡献，因此不需要；这一步用 `versions_by_entity_event`，是索引范围扫描。
* 第二步对第一步得到的 id 的**全部**版本取 `available_at <= C` 的部分，而不是只取窗口
  内、未删除或实体匹配的版本。原因是胜者可能是窗口外、墓碑或别的实体的版本；如果先按
  窗口或墓碑过滤再选最高 rev，就会选错历史胜者。索引 `versions_by_id_available`
  以 `id` 为前缀，每个 id 的读取是局部的；取回后在 Python/临时表里按 `id` 取最高 rev，
  再交给 `contribution` 过滤。
* 注意 `versions_by_id_available(id, available_at, rev)` 的 `available_at` 是范围列，
  不能在一条语句里对每个 id 同时做范围加 `rev` 排序的索引加速；实现上按 id 分组后
  对每组的 `available_at` 做范围读取，`rev` 由 `ORDER BY rev DESC LIMIT 1` 或内存
  比较得到。这是允许的残余成本。
* 为了防错，重算结果必须与 `checks.py:8` 的 `oracle` 在有限样例上一致（`checks.py:79`
  的 `review` 会重放证据并用它检查候选边界）；但生产算法不是对全部样本做 oracle 扫描。

访问量可以这样界定。设本批涉及 `K` 个 id，这些 id 的总版本数为 `V_K`，胜出区间数为
`S_K`，变化区间产生的候选为 `M`：

| 步骤 | 读取/索引 | 量级 |
| --- | --- | --- |
| 导入与冲突检查 | `versions` 主键 `(id,rev)`（`source.py:8`） | `O(K log V)` 次点查 |
| 旧区间 | `winner_spans` 主键前缀 `rid` | `O(S_K)` |
| 新区间 | 仅本批 id 的全部版本 | `O(V_K)` |
| 区间差 | 内存归并 | `O(S_K)` |
| 候选检索 | `samples_by_entity_time` 等值+范围，`cutoff` 残余 | 每个变化侧 `O(log N + 命中数)` |
| 候选重算 | 每个候选窗口 id 数 `E_s`、这些 id 的版本数 `V_s` | `Σ(O(log V + E_s + V_s))` |
| 物化 | `features` 主键（`source.py:12`） | `O(M)` 读 + `O(M)` 写 |

全程不调用 `source.all_versions`（`source.py:66`）或 `source.all_samples`
（`source.py:71`）；不靠 `SELECT * FROM samples` 再在内存里过滤。`read()` 只作为
观察输出，不能参与候选计算。这里不承诺二维最优查询复杂度：候选查询受制于一个索引只能
有一个范围，`prediction_time` 用 B-tree 范围，`cutoff` 只能残余过滤；这是题目允许的
“前导范围扫描 + 另一维 SQL 残余过滤”。

## 8. 样例逐项核对

以下 `changed` 是原 `observe.py` 的实际输出，候选是本文方案的预测。候选允许保守，
所以会出现“候选包含但 `changed` 不包含”的样本（`shadowed`、`cancel` 的部分样本）。

### 低版本迟到：`history/late_low`

seed 已导入 `x rev3 available_at=30 amount=30`。`late_low` 迟到的是
`x rev1 available_at=10 amount=7`，全局最高 rev 仍是 3，但它比 rev3 更早可用，因此
胜出函数新增前缀区间 `[10,30)`：`W_x(C)=rev1`（`C<10` 无胜者，`C>=30` 仍是 rev3）。
候选是 `a10,a19,a20,a29`：它们的 `T=86400`、`C` 在 `[10,30)`，且 rev1 的
`event_time=0` 落在 `[0,86400)`。`a30` 的 `C=30` 已回到 rev3，`a_shift/a_slice` 的
窗口下界分别是 10/15，`event_time=0` 不在窗口内，均不受影响。这正是“全局最高 rev
未变仍会影响历史样本”的例子（另见 `tombstone`：rev2 低于 rev3，但 rev3 在 `C<30`
不可见，rev2 在 `[20,30)` 成为胜者）。

### 墓碑：`history/tombstone`

`x rev2 available_at=20 deleted=true` 在 `[20,30)` 成为胜者。墓碑参与选择，所以
`a20`（`C=20`）和 `a29` 的胜者从 rev1 变成墓碑，贡献从 `(7,1)` 变为 `(0,0)`；
`a30` 的 `C=30` 已由 rev3 胜出，墓碑从未成为它的胜者。若先过滤墓碑再选版本，会错误地
让 rev1 继续贡献，这就是必须“墓碑参与选择”的原因。

### 实体与时间移动：`history/move`

`x rev4 available_at=25 entity=B event_time=86400 amount=11`：

* `[25,30)`：旧胜者是墓碑 rev2（rev3 尚未可用），新胜者是 rev4（B，`event_time=86400`，
  11）。这一段的 `C` 都小于 30：`b_next` 的 `C=30` 恰好落在下一段的闭端点 `lo`，按
  `C < hi` 不归入本段；`b40` 的 `C=40` 同理。A 样本在本段看到的旧、新胜者都不可贡献
  （旧者是墓碑，新者属于 B），所以这一段本身不产生候选。
* `[30,+inf)`：旧胜者是 rev3（A，`event_time=0`，30），新胜者是 rev4。A 样本
  `a30,a40` 的 `C>=30`，旧贡献为 30，新胜者属于 B，贡献为 0，所以数值下降。
  B 样本 `b_next`（`T=86401`，窗口 `[1,86401)`）和 `b40`（`T=86401`）的 `C` 分别是
  30、40，都落在 `[30,+inf)`，看到 rev4 的 `event_time=86400` 在窗口上界之前，贡献 11。
  `b30` 的 `T=86400` 窗口是 `[0,86400)`，`event_time=86400` 因上界开而被排除，所以
  `b30` 仍是 `[0,0]`，不进候选。
  候选 `a30,a40,b40,b_next` 与实际 `changed` 一致。这里也演示了胜出区间的半开端点：
  `C=30` 属于后一段而不是前一段。

### 撤销后复活与零金额：`history/revive_zero`

`x rev5 available_at=40 entity=A event_time=0 amount=0 deleted=false` 在 `[40,+inf)`
替换 rev4。对 `a40`（`C=40`），旧胜者 rev4 属于 B 不贡献，新胜者未删除、金额 0，仍
贡献 `(0,1)`，所以 `a40` 从 `[0,2]` 变成 `[0,3]`：零金额事件增加 `count`，这正是
`features.py:8` 的语义。对 `b40`，旧胜者 rev4 贡献 11，新胜者属于 A 不贡献，变成
`[0,0]`。`b_next` 的 `C=30` 仍看到 rev4，因此不变。

### 同批多版本与完全遮蔽：`history/shadowed`

同一批含 `x rev6 entity=B amount=99, available_at=35` 和
`x rev7 entity=A amount=0, available_at=35`。批后完整状态下 `available_at=35` 的最高
rev 是 7，rev6 从不成为任何 `C` 的胜者；不能按行处理造出中间候选。
`[35,40)` 的胜者从 rev4（B，amount=11）变为 rev7（A，amount=0）：B 样本的 `C` 都不在
该区间（`b_next C=30`、`b40 C=40`），A 样本中 `a40 C=40` 也不在该区间，所以无候选。
`[40,+inf)` 的胜者从 rev5（A，amount=0）变为 rev7（A，amount=0），旧、新胜者都能给
`a40` 贡献 `(0,1)`，因此本文保守地把 `a40` 放进候选；但两者金额和计数相同，实际
`changed` 为空。`shadowed` 的候选 `[a40]` 是允许的保守多选，不是最小候选集。

### 贡献抵消：`history/cancel`

`y rev2`（`event_time=10`，5→8）和 `z rev2`（`event_time=20`，-5→-8）都在
`available_at=20` 生效，变化区间 `[20,+inf)`。对 `a10/a19`，`C<20` 看不到新版本；
对 `a20,a29,a30,a40,a_shift`，两个事件都在各自窗口内，金额变化 `+3` 与 `-3` 抵消，
聚合值不变；对 `a_slice`（`T=86415`，窗口下界 `T-86400=15`），`y` 的
`event_time=10` 在窗口外，只有 `z` 从 -5 变为 -8，所以 `a_slice` 从 `[-5,1]` 变为
`[-8,1]`。候选是 `a20,a29,a30,a40,a_shift,a_slice`，其中只有 `a_slice` 在 `changed`
里——这就是“候选 ≠ changed”的典型例子。`a09`、`a_upper` 不进候选：`a09` 的 `C=9`
早于所有版本，`a_upper` 的 `T=0` 窗口 `[-86400,0)` 为空。

### `normal` 与重放

`normal/first` 的 `e rev1 available_at=10` 对 `n`（`T=86400,C=10`）贡献 `(7,1)`，
候选 `[n]`；`normal/replay` 同身份同内容，`accepted=0`，批前批后胜出区间完全相同，
候选 `[]`。`history/replay` 同理。`seed` 批是首次导入，胜出区间从无到有，候选是
8 个受影响的 A 样本；`a09`（`C=9`）、`a_upper`（`T=0` 空窗）和所有 B 样本都不在候选。

## 9. 可重跑文档证据

下面的 JSON 块由 `python3 checks.py review` 重放：`observed` 是 `observe.py:19` 的
`observe("all")` 完整实际输出（`checks.py:86` 比较相等），`candidates` 是按场景名、
批名两层组织的方案预测（`checks.py:88` 到 `checks.py:114` 用测试专用 oracle 检查
“必须覆盖 changed、不得超出允许的保守边界”，并要求排序去重）。候选是设计预测，
不是已经实现的增量输出；本轮没有实现增量器。

`review` 还会检查本文引用的 `*.py:line` 都存在（`checks.py:115` 到 `checks.py:119`），
但它只核对可重放事实，不证明设计正确、访问范围合规或 SQL 索引有效；这些结论由 §3–§7
的推导、伪代码和访问路径说明支持，仍需人工复核。

~~~json
{
  "candidates": {
    "history": {
      "cancel": [
        "a20",
        "a29",
        "a30",
        "a40",
        "a_shift",
        "a_slice"
      ],
      "late_low": [
        "a10",
        "a19",
        "a20",
        "a29"
      ],
      "move": [
        "a30",
        "a40",
        "b40",
        "b_next"
      ],
      "replay": [],
      "revive_zero": [
        "a40",
        "b40"
      ],
      "seed": [
        "a10",
        "a19",
        "a20",
        "a29",
        "a30",
        "a40",
        "a_shift",
        "a_slice"
      ],
      "shadowed": [
        "a40"
      ],
      "tombstone": [
        "a20",
        "a29"
      ]
    },
    "normal": {
      "first": [
        "n"
      ],
      "replay": []
    }
  },
  "observed": {
    "history": [
      {
        "accepted": 3,
        "batch": "seed",
        "changed": [
          "a10",
          "a19",
          "a20",
          "a29",
          "a30",
          "a40",
          "a_shift",
          "a_slice"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            0,
            2
          ],
          [
            "a19",
            0,
            2
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            30,
            3
          ],
          [
            "a40",
            30,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            0,
            0
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            3
          ],
          [
            "version_lookup",
            "y",
            1
          ],
          [
            "version_lookup",
            "z",
            1
          ],
          [
            "versions_full_scan",
            3
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 1,
        "batch": "late_low",
        "changed": [
          "a10",
          "a19",
          "a20",
          "a29"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            7,
            3
          ],
          [
            "a29",
            7,
            3
          ],
          [
            "a30",
            30,
            3
          ],
          [
            "a40",
            30,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            0,
            0
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            1
          ],
          [
            "versions_full_scan",
            4
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 1,
        "batch": "tombstone",
        "changed": [
          "a20",
          "a29"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            30,
            3
          ],
          [
            "a40",
            30,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            0,
            0
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            2
          ],
          [
            "versions_full_scan",
            5
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 1,
        "batch": "move",
        "changed": [
          "a30",
          "a40",
          "b40",
          "b_next"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            0,
            2
          ],
          [
            "a40",
            0,
            2
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            11,
            1
          ],
          [
            "b_next",
            11,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            4
          ],
          [
            "versions_full_scan",
            6
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 1,
        "batch": "revive_zero",
        "changed": [
          "a40",
          "b40"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            0,
            2
          ],
          [
            "a40",
            0,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            11,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            5
          ],
          [
            "versions_full_scan",
            7
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 2,
        "batch": "shadowed",
        "changed": [],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            0,
            2
          ],
          [
            "a40",
            0,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -5,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            11,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "x",
            6
          ],
          [
            "version_lookup",
            "x",
            7
          ],
          [
            "versions_full_scan",
            9
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 2,
        "batch": "cancel",
        "changed": [
          "a_slice"
        ],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            0,
            2
          ],
          [
            "a40",
            0,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -8,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            11,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "y",
            2
          ],
          [
            "version_lookup",
            "z",
            2
          ],
          [
            "versions_full_scan",
            11
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      },
      {
        "accepted": 0,
        "batch": "replay",
        "changed": [],
        "features": [
          [
            "a09",
            0,
            0
          ],
          [
            "a10",
            7,
            3
          ],
          [
            "a19",
            7,
            3
          ],
          [
            "a20",
            0,
            2
          ],
          [
            "a29",
            0,
            2
          ],
          [
            "a30",
            0,
            2
          ],
          [
            "a40",
            0,
            3
          ],
          [
            "a_shift",
            0,
            2
          ],
          [
            "a_slice",
            -8,
            1
          ],
          [
            "a_upper",
            0,
            0
          ],
          [
            "b30",
            0,
            0
          ],
          [
            "b40",
            0,
            0
          ],
          [
            "b_next",
            11,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "y",
            2
          ],
          [
            "versions_full_scan",
            11
          ],
          [
            "samples_full_scan",
            13
          ],
          [
            "feature_writes",
            13
          ]
        ]
      }
    ],
    "normal": [
      {
        "accepted": 1,
        "batch": "first",
        "changed": [
          "n"
        ],
        "features": [
          [
            "n",
            7,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "e",
            1
          ],
          [
            "versions_full_scan",
            1
          ],
          [
            "samples_full_scan",
            1
          ],
          [
            "feature_writes",
            1
          ]
        ]
      },
      {
        "accepted": 0,
        "batch": "replay",
        "changed": [],
        "features": [
          [
            "n",
            7,
            1
          ]
        ],
        "trace": [
          [
            "version_lookup",
            "e",
            1
          ],
          [
            "versions_full_scan",
            1
          ],
          [
            "samples_full_scan",
            1
          ],
          [
            "feature_writes",
            1
          ]
        ]
      }
    ]
  }
}
~~~
