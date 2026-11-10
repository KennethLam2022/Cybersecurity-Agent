# 安枢上下文评测指标契约

状态：开发基线
适用范围：安枢 E2E RAG 评测中的 Context Precision、Context Recall、Context Relevance
版本：1

本文是这三项指标在安枢项目内的唯一计算口径。名称相同不代表与其他评测库的实现、标签或分值可直接比较。

## 共同输入与结果约定

- `query`：本次检索的用户问题。
- `retrieved_contexts`：检索器实际返回的上下文片段，保持检索顺序；每项至少包含内容和稳定来源标识。
- `reference_evidence`：由评测集提供的、回答该问题所需的最小事实/要求清单。它是可审查的参考证据，不是模型生成的回答。
- 判断单位和标签必须保留在评测明细中，不能只持久化最终分数。
- 每项计算输出 0 到 1 的分数，或明确的未评分状态。只有成功评分的结果可以进入平均值；报告同时显示已评分数和总样本数。
- 未提供标签、参考证据或有效上下文，与“全部不相关”是不同状态；不得将其悄悄当作 0 分。
- Judge 调用失败时，结果为 `judge_error` 且无分数。启发式回退若仍被调用，必须标记为 `unverified_heuristic`，与已校准 Judge 分数分开统计、不可混合比较。
- 保存评测时使用的 Judge 模型标识和 Prompt 版本。更换模型或 Prompt 后，分数不应默认视为可直接比较；比较前需用同一校准样例复核。
- 本契约不定义发布通过阈值。

建议统一使用以下状态语义：

| 状态 | 含义 | 是否参与均值 |
|:--|:--|:--:|
| `scored` | 输入和 Judge 结果均有效，已按公式计算 | 是 |
| `unscored_missing_annotation` | 所需相关性标签未提供或未完整 | 否 |
| `unscored_missing_reference` | Recall 所需参考证据未提供或为空 | 否 |
| `unscored_empty_retrieval` | 检索结果为空，无法评估返回上下文质量 | 否 |
| `judge_error` | Judge 调用、结构化解析或必要校验失败 | 否 |
| `unverified_heuristic` | 使用未校准启发式回退所得结果 | 默认否 |

## Context Precision

**评测问题：** 相关检索结果是否排在不相关结果之前？

**单位与标签：** 对每个实际返回的检索片段，按 Query 和评测集参考证据标注相关性 `v_k ∈ {0,1}`。标签必须与检索片段一一对应并维持原始排名。不能使用生成答案的内容来决定相关性，否则会与 Faithfulness 混淆。

**计算：**

令 `K` 为已完整标注的检索片段数，`P@k = (Σ(i=1..k) v_i) / k`：

`Context Precision@K = (Σ(k=1..K) P@k × v_k) / (Σ(k=1..K) v_k)`

该值为按排名加权的 Average Precision。越相关的片段越靠前，分数越高。

**边界：**

- 没有检索片段：`unscored_empty_retrieval`。
- 有检索片段但没有任何相关片段：分数为 `0`，状态为 `scored`；这是“已评估但未命中”，不同于没有标签。
- 任一检索片段缺少相关性标签：`unscored_missing_annotation`，避免只评已标注前缀造成排名偏差。
- 只改变回答文本、保持 Query、检索片段和标签不变时，分数不变。

## Context Recall

**评测问题：** 检索上下文覆盖了多少回答问题所需的参考证据？

**单位与标签：** 将 `reference_evidence` 表示为互不重复、可独立判断的原子事实/要求。对每项判断至少一个检索片段是否直接支持它，得到 `c_j ∈ {0,1}`。判断基于检索上下文，不基于模型最终回答。

**计算：**

令 `M` 为参考证据项数：

`Context Recall = (Σ(j=1..M) c_j) / M`

**边界：**

- 没有参考证据或参考清单为空：`unscored_missing_reference`，不把 Recall 记为 0 或 1。
- 参考证据有效但检索结果为空：分数为 `0`，状态为 `scored`。
- 同一证据被多个片段重复支持，只计一次。
- 证据是否被回答覆盖属于 Answer Completeness，不属于本项 Context Recall。

## Context Relevance

**评测问题：** 返回上下文中有多少信息与 Query 相关？

**单位与标签：** Judge 先将所有检索片段拆成可独立判断的原子信息单元，排除纯标题、页眉页脚、空白和无语义格式标记；跨片段重复的同一信息只计一次。对每个信息单元仅根据 Query 判断相关性，得到 `r_i ∈ {0,1}`。不判断该信息是否正确、权威或已被回答。

**计算：**

令 `N` 为去重后可评估的信息单元数：

`Context Relevance Ratio = (Σ(i=1..N) r_i) / N`

此为安枢定义的“相关信息占检索信息总量比例”，范围为 0 到 1。它不是检索排序指标，也不依赖生成答案或参考答案。

**边界：**

- 没有检索片段，或没有可评估的信息单元：`unscored_empty_retrieval`。
- 缺少对任一信息单元的判断：`unscored_missing_annotation`。
- 内容与问题有关但事实错误/过时，仍可被判为相关；正确性、来源权威性应由其他检查承担。
- 改变回答文本但保持 Query 和检索上下文不变时，分数不变。

**名称说明：** RAGAS 文档将 Context Relevance 列在 NVIDIA Metrics 名下；不同实现可能使用不同评分尺度或 Judge 逻辑。安枢的 Ratio 只表示本节定义的比例，不宣称与第三方同名指标可互换。

## 与相邻指标的边界

| 指标 | 核心对象 | 是否依赖回答 | 是否依赖参考证据 | 主要发现 |
|:--|:--|:--:|:--:|:--|
| Context Precision | 排序后的检索片段及其相关性标签 | 否 | 是，提供/校准相关性标签 | 相关片段是否靠前 |
| Context Recall | 参考证据项 | 否 | 是 | 检索是否漏掉必要证据 |
| Context Relevance Ratio | 检索上下文中的信息单元 | 否 | 否 | 返回内容中相关信息占比 |
| Faithfulness | 回答中的事实主张 | 是 | 否 | 回答主张是否受上下文支持 |
| Answer Relevancy | 回答与用户问题 | 是 | 否 | 回答是否切题 |

## 手算样例

### Precision：排名影响

三个检索片段的相关性标签为 `[1, 0, 1]`：

- `P@1 = 1/1 = 1`
- `P@2 = 1/2`
- `P@3 = 2/3`
- 相关项数为 2，因此 `AP = (1 + 2/3) / 2 = 5/6 ≈ 0.8333`

若将第一个相关项与第一个不相关项交换，标签为 `[0, 1, 1]`：

- `P@2 = 1/2`，`P@3 = 2/3`
- `AP = (1/2 + 2/3) / 2 = 7/12 ≈ 0.5833`

两个排序具有相同相关项数量，但相关项后移导致 Precision 下降。

### Precision：无相关结果与未标注

- 两个检索片段均已标注为不相关 `[0, 0]`：已评估但无命中，分数 `0`、状态 `scored`。
- 两个片段均无标签：`unscored_missing_annotation`，无分数，不能当成 `[0, 0]`。

### Recall：参考证据覆盖

参考证据为三项 `[e1, e2, e3]`，检索上下文支持 `e1`、`e2`，不支持 `e3`：

`Context Recall = 2/3 ≈ 0.6667`

若同一片段重复支持 `e1`，仍只计一次。若评测样本没有参考证据，状态为 `unscored_missing_reference`。

### Relevance：上下文信息比例

Query 为“网络安全事件报告应包含哪些内容？”。去重后上下文含四项信息，其中三项涉及报告内容、一项介绍无关的办公网络维护：

`Context Relevance Ratio = 3/4 = 0.75`

此分数不因模型回答是否提到这三项而变化。

## 指标评审清单

- [ ] Precision 输入的检索顺序、逐片段标签和计算明细可复核。
- [ ] Recall 的参考证据是原子、去重并对应 Query；没有参考证据时不产生伪分数。
- [ ] Relevance 只看 Query 和检索上下文，不看生成答案；信息单元拆分及去重结果可复核。
- [ ] 空输入、未标注、Judge 错误及启发式回退均保留不同状态。
- [ ] 报告仅聚合 `scored` 项，并同时显示样本数及未评分原因。
- [ ] 未将历史目标值或未经人工抽样校准的 Judge 分数解释为发布门槛。

## 参考资料

- RAGAS, Context Precision: <https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/>
- RAGAS, Context Recall: <https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/>
- RAGAS, Available Metrics（列出 RAG Metrics 与 NVIDIA Metrics 的分组）: <https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/>

上述资料用于确认常见指标关注点与术语边界；具体公式、状态和示例以本契约中的安枢项目口径为准。
