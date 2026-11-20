# 滴定曲线当量产品与样品效价核算仓

Python 3.11、SQLite、精确有理数（`fractions.Fraction`）。不连接设备，不套真实药品标准；
所有曲线与数值均为合成数据。信号模型严格限定为题面给出的离散定义，不声称连续曲线拐点。

## 1. 有限模型与算法

对每条严格递增体积 `v`、电位 `e` 的曲线，仅对**内部实测点** `i` 计算

```
L_i = (e_i - e_{i-1}) / (v_i - v_{i-1})
R_i = (e_{i+1} - e_i) / (v_{i+1} - v_i)
s_i = abs(R_i - L_i)
```

候选集合 = 体积落在闭区间 `[a,b]` 且 `s_i >= T` 的内部点。端点永远不是候选。
选择规则（`titration/solver.py`）：

1. 恰好选 `K` 个候选；
2. 任意两个被选体积差 `>= d`；
3. 先最大化强度总和；
4. 总和相同时，升序体积列表**字典序最小**。

实现为按体积排序后的精确有理数动态规划。状态为“从第 `i` 个候选起再选 `c` 个”的最优
`(强度总和, 体积元组)`；比较键 `(-总强度, 体积元组)`，因此在求最大总和的同时完成
字典序裁决。若不存在合法组合，返回明确的 `NO_ENDPOINT`（`feasible=False`、
`objective=None`），绝不返回少于 `K` 个的“成功”结果。

`brute_force_selection` 枚举全部组合，作为校验参照。验证入口对多条少于 10 点的曲线
枚举所有合法组合进行比对（本次运行约 35.5 万组用例）。

## 2. 精确有理数

* 输入接受 `int`、`Fraction` 或 `"n"`、`"n/d"`、`"-1.25"` 形式的字符串；
  **二进制 `float` 被拒绝**，以免隐式引入不可精确表示的数值。
* 存储层全部以规范文本 `n` 或 `n/d` 保存。
* 所有比较、目标值与效价计算都使用 `Fraction`，无浮点参与。
* JSON 输出中的有理数统一为字符串，避免调用方误判精度或单位。

## 3. 曲线与方法管理

* 曲线：严格递增体积、等长电位，最多 300 点，至少 3 点。
* 方法参数：`[a,b]`、`T>=0`、`K>0`、`d>=0`，以及可选的当量序号 `j`（`1<=j<=K`）。
  方法可不声明 `j`；此时效价核算会明确拒绝。
* 曲线与方法均有版本号。`update_curve`/`update_method` 需预期版本（曲线强制），
  每次更新落一条历史版本快照（`curve_versions`、`method_versions`），旧产品仍可按
  旧版本查询输入。

## 4. 端点产品（公开、不可变）

`publish_endpoints(curve_id, method_id)` 每次都从**当前**曲线 + 方法重新计算候选与选择，
生成一条不可变的公开产品记录：

* `selected_volumes`、`selected_indices`、`objective`
* 全部斜率候选（`candidates`）与解释（`explanation`）
* 输入快照 `input`：完整体积/电位、方法参数、模型标识
* 触发它的曲线/方法版本

同一 `(curve, curve_version, method, method_version)` 组合只会有一条内部产品
（唯一索引）。因此：

* 更正曲线或阈值后形成**新版本**，发布会生成**新产品**；
* 若当前版本无法满足 `K` 与间隔约束，抛 `NO_ENDPOINT` 且**不写入**任何记录，
  不会保留新候选却沿用旧端点。

### 解释

`explanation` 包含：

* `candidates`：所有入选候选及其 `L/R/s`；
* `selected`：最终选中的点；
* `excluded`：每个落选候选及原因；
* `objective`：最优总强度。

落选原因不是启发式猜测，而是由前缀/后缀最优 DP 精确算出“包含该点的最佳 K 选择
总强度”，并给出与最优目标的差值 `deficit_vs_optimum`：

* 无任何合法选择可包含它 → 间隔约束不可行；
* 与最优等值 → 仅因字典序裁决落选；
* 与已选点间隔冲突 → 明确给出冲突点；
* 否则给出至少差多少总强度。

### 外部同格式产品

`import_endpoint_product` 接受文档化 JSON（至少 `curve_name`、`method_name`、`k`、
`selected_volumes`）。外部产品与内部产品同表存储，`source='external'`，可按
`external_ref` 幂等导入。

## 5. 样品与批次效价

样品字段：净称量 `m` mg、取样比例 `p`（`0<p<=1`）、滴定剂浓度 `c` mg/mL、
反应系数 `f>0`，以及空白与样品曲线的产品身份（曲线或已发布产品）。

方法声明使用第 `j` 个当量点。样品与空白必须**都成功**且 `K>=j`，否则该样失败。
有效体积与效价：

```
有效体积 = 样品产品.selected_volumes[j-1] - 空白产品.selected_volumes[j-1]
效价     = 有效体积 * c * f / (m * p)
```

* 负有效体积：拒绝（样失败）；
* 零有效体积：合法，效价为 0；
* 不做任何额外单位换算。

效价仓**只读取公开产品的 `selected_volumes`**。测试会删除产品的私有 `candidates_json`，
验证效价仍可计算（`potency_uses_only_public_products`）。

### 批次

* 一个批次最多 30 个平行样。
* 逐样计算，再按净称量加权：

```
平均值 = sum(m_i * potency_i) / sum(m_i)
```

绝不对体积先平均再套平均称量。

* 任一必需样失败 → 报告 `INCOMPLETE`，`average=null`，并在 `gaps` 中列出每个失败样及
  原因；失败样仍保留在 `samples` 中，不静默删除。

### 更正与重算

`compute_sample` / `compute_batch` 默认 `refresh=True`：从当前曲线+方法重新发布/复用
产品，因此修正候选阈值或曲线会产生新产品与**真实改变**的效价。

`refresh=False` 使用样品当前绑定的公开产品 ID。每次刷新计算都会把解析到的产品 ID
写回样品（`bind_sample_products`），因此后续审计可复现。

报告是追加式快照。`replay_report(report_id)` 使用该报告记录的**旧产品 ID** 重算，
证明旧核算绑定旧输入：曲线更正后，旧报告的值与其重放值都不变。

## 6. 目录

```
titration/
  exact.py         精确有理数解析/规范化与错误类型
  solver.py        DP、暴力参照、精确落选分析
  curve_service.py 曲线/方法/候选/发布/解释
  potency.py       样品、批次、加权、重放
  store.py         SQLite 模式、版本快照、不可变产品、报告
  demo.py          端到端合成场景
  validation.py    可复跑校验，写出 results/validation.json
  cli.py           命令行入口
tests/test_titration.py
results/demo.json
results/validation.json
```

## 7. 运行

```bash
python3 -m pytest -q                                   # 35 项单元测试
python3 -m titration.cli --db var/demo.sqlite demo     # 端到端演示 -> results/demo.json
python3 -m titration.cli --db var/validation.sqlite \
    validate --out results/validation.json             # 15 项校验
```

常用子命令：

```bash
python3 -m titration.cli curve-create --name c --volumes 0 1 4 5 8 11 12 16 17 \
    --potentials 0 1 4 64 184 304 344 348 349
python3 -m titration.cli method-create --name m --low-volume 0 --high-volume 30 \
    --threshold 10 --k 2 --min-gap 2 --equivalence-index 2
python3 -m titration.cli publish --curve-id 1 --method-id 1
python3 -m titration.cli explain --curve-id 1 --method-id 1
python3 -m titration.cli batch-eval --batch-id 1
python3 -m titration.cli import-product --file external_product.json
```

## 8. 验证覆盖（对应题面要求）

| 要求 | 覆盖方式 |
|---|---|
| 不等距采样、直接最大斜率陷阱 | `uneven_spacing_pair_beats_raw_max_slope`、单测 |
| 两端点竞争 | `two_endpoint_competition_published`、单测 |
| 相同总强度的字典序裁决 | `equal_total_strength_lex_tiebreak`、穷举对照 |
| 负有效体积拒绝 / 零合法 | `negative_and_zero_effective_volume`、单测 |
| 不等称量加权（非体积均值） | `unequal_net_mass_weighting` |
| 少于 10 点曲线枚举所有合法组合 | `dp_matches_exhaustive_under_ten_points`（约 35.5 万例） |
| 落选解释的精确性 | `exclusion_reasons_match_brute_force`（约 1.6 万例逐点比对） |
| 300 点、K<=6 | `scale_300_points_k6` |
| 批次必需样失败 → INCOMPLETE + 缺口 | `incomplete_batch_lists_gaps`、单测 |
| 更正曲线/阈值 → 新产品、真实结果改变 | `correction_changes_potency_and_old_report_immutable`、`threshold_correction_new_product` |
| 旧核算绑定旧输入（重放不变） | `replay_report` 与上述校验 |
| 不读取私有候选表 | `potency_uses_only_public_products` |
| 无合法组合 → NO_ENDPOINT（不少选） | `no_endpoint_explicit`、发布失败不落库 |

## 9. 边界与取舍

* 候选索引按体积升序确定，DP 与字典序裁决都以此为准；候选本身仍保存原始曲线点索引。
* `K` 大于候选数、或间隔约束不可满足时明确 `NO_ENDPOINT`；`publish_endpoints` 抛
  `ValidationError`，`compute_sample` 把原因写入失败列表，批次标 `INCOMPLETE`。
* 方法声明 `j` 时强制 `1<=j<=K`；未声明 `j` 的核算请求直接拒绝。
* 曲线与方法历史版本用于审计与重放；产品本身不可变，不会被覆盖。
* 产品按 `(curve, curve_version, method, method_version)` 唯一；重复发布会返回既有产品
  而不重复写入。
