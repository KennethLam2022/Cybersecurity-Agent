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
- 每项计算都返回下表规定的全部字段；`details` 保留对应指标的判断单位/标签、分子、分母及必要的中间值，但不复制检索文档原文。

| 字段 | 类型 | 必填及取值约定 |
|:--|:--|:--|
| `score` | number 或 null | 必填；成功评分为 0–1 数值；未评分或错误时为 null |
| `status` | enum string | 必填；只允许使用下表列出的状态 |
| `explanation` | 非空 string | 必填；说明分数含义或未评分/错误原因，不包含原始 Prompt 或敏感上下文 |
| `details` | object | 必填；按下方状态与指标结构保留标签/单位、计数及公式中间值；不得复制检索文档原文 |
| `evaluator` | object | 必填；字段为 `method`、`model_id`、`prompt_version`。`method` 只允许 `annotation`、`llm_judge`、`heuristic`、`deterministic`、`not_run`；`model_id` 和 `prompt_version` 类型为 string 或 null |

`evaluator.method` 与状态的约束：

- `scored`：`annotation`（使用已有标注）或 `llm_judge`；方法与输入标签来源必须一致。
- 若结果由输入结构和公式直接确定（例如有效参考证据下空检索的 Recall），`scored` 必须使用 `deterministic`，不得伪称使用 Judge 或数据集标签。
- `unverified_heuristic`：必须为 `heuristic`。
- `unscored_*`：必须为 `not_run`。
- `judge_error`：必须为 `llm_judge`；若回退尝试失败，在 `details` 记录失败类别。
- `llm_judge` 必须提供非空 `model_id` 和 `prompt_version`；其他方法两者必须为 null。`annotation` 必须提供非空 `details.annotation_source`；若来源无法识别，不得使用 `annotation`。
- 每条结果只有一个 `status`；`scored`、`unverified_heuristic`、`judge_error`、`unscored_*` 互斥。

成功评分的 `details` 最少包含下列字段；标注缺失或评估失败时保留已有判断信息，并用 `missing_inputs` 或 `failure_category` 说明原因：

| 指标 | `details` 必需字段 | 标签含义 |
|:--|:--|:--|
| Context Precision | `judgments: [{rank: integer, relevant: boolean}]`、`ap_numerator: number`、`relevant_count: integer` | `rank` 从 1 开始，`relevant` 对应 `v_k` |
| Context Recall | `judgments: [{evidence_id: string, covered: boolean}]`、`covered_count: integer`、`reference_count: integer` | `evidence_id` 对应唯一参考证据项 |
| Context Relevance | `judgments: [{unit_id: string, relevant: boolean}]`、`relevant_unit_count: integer`、`unit_count: integer` | `unit_id` 是来源标识与单元序号组成的稳定定位符 |

`annotation` 表示使用评测集已有标签，必须在 `details.annotation_source` 记录非空来源标识。`judgments` 只保留稳定定位符和标签，不保留对应原文。

按状态约束 `details`：

| 状态 | 必需字段与类型 |
|:--|:--|
| `scored` | 使用对应指标表中的全部字段；计数字段为非负 integer，分数字段为 number。`annotation_source` 为非空 string（`method=annotation` 时）；`method=llm_judge` 时必须有完整 Judge 判断明细 |
| `unscored_missing_annotation` | `missing_inputs: enum[]`，非空；元素为 `relevance_annotations` 或 `judgment_units`；可包含已得到的部分判断明细 |
| `unscored_missing_reference` | `missing_inputs: ["reference_evidence"]`；可包含已得到的部分判断明细 |
| `unscored_empty_retrieval` | `missing_inputs: ["retrieved_contexts"]`；仅用于该指标因空检索确实无法计算的情况 |
| `unscored_no_evaluable_units` | `missing_inputs: ["evaluable_units"]`；检索结果非空，但清洗、拆分、去重后没有可判断的信息单元 |
| `judge_error` | `failure_category: enum string`，限 `call_failed`、`invalid_response`、`invalid_judgment`；`fallback_attempted: boolean`；当其为 true 时，`fallback_failure_category: enum string`，限 `unavailable`、`invalid_result` |
| `unverified_heuristic` | 对应指标表中的全部字段；`judge_failure_category: enum string`，限 `call_failed`、`invalid_response`、`invalid_judgment`；`fallback_method: string`，非空且符合 `<method_name>@<version>` 格式 |

以上字段在适用状态下必填；不适用的字段必须省略，不以空字符串或任意 null 代替。额外诊断字段不得包含 Prompt、原文或敏感内容。

- 只有 `scored` 结果可以进入校准分数平均值；报告同时显示已评分数、总样本数和未评分原因。
- 所有 `unscored_*` 和 `judge_error` 结果的 `score` 必须为 `null`；`unverified_heuristic` 可以带数值分数，但默认不进入校准均值。
- 未提供标签、参考证据或有效上下文，与“全部不相关”是不同状态；不得将其悄悄当作 0 分。
- 输入不完整时先返回对应的 `unscored_*` 状态且不调用 Judge，`evaluator.method=not_run`。所有必需判断标签齐全且公式成功计算时为 `scored`；标签可来自评测集标注或成功的 Judge，`evaluator.method` 必须记录实际来源。
- Judge 失败且没有成功的启发式回退时返回 `judge_error` 和 `score=null`。若显式启用的启发式回退成功，则返回带数值分数的 `unverified_heuristic`，`evaluator.method=heuristic`，`details.fallback_method` 记录符合 `<method_name>@<version>` 格式的回退标识，并保留 Judge 失败类别及计算明细；回退失败则返回 `judge_error` 和 `score=null`，`details` 记录失败类别。`status` 只表示最终结果，不同时标记 `judge_error` 与 `unverified_heuristic`。
- 保存评测时使用的 Judge 模型标识和 Prompt 版本。更换模型或 Prompt 后，分数不应默认视为可直接比较；比较前需用同一校准样例复核。
- 本契约不定义发布通过阈值。

建议统一使用以下状态语义：

| 状态 | 含义 | 是否参与均值 |
|:--|:--|:--:|
| `scored` | 所需输入和标签完整，已按公式计算；标签可来自标注或成功的 Judge | 是 |
| `unscored_missing_annotation` | 所需相关性标签未提供或未完整 | 否 |
| `unscored_missing_reference` | Recall 所需参考证据未提供或为空 | 否 |
| `unscored_empty_retrieval` | 对应指标因检索结果为空而无法计算 | 否 |
| `unscored_no_evaluable_units` | 检索非空，但没有可评估的信息单元 | 否 |
| `judge_error` | Judge 调用、结构化解析或必要校验失败 | 否 |
| `unverified_heuristic` | 使用未校准启发式回退所得结果 | 默认否 |

状态选择按指标专节优先：Precision 在无检索项时为 `unscored_empty_retrieval`；Relevance 在无检索项时为 `unscored_empty_retrieval`，有检索项但无可评估信息单元时为 `unscored_no_evaluable_units`。Recall 若参考证据有效，即使检索为空也可确定没有任何参考项被覆盖，因此得 `score=0, status=scored, evaluator.method=deterministic`，不适用 `unscored_empty_retrieval`。

## Context Precision

**评测问题：** 相关检索结果是否排在不相关结果之前？

**单位与标签：** 对每个实际返回的检索片段，按 Query 和评测集参考证据标注相关性 `v_k ∈ {0,1}`。标签必须与检索片段一一对应并维持原始排名。不能使用生成答案的内容来决定相关性，否则会与 Faithfulness 混淆。

**计算：**

令 `K` 为已完整标注的检索片段数，`P@k = (Σ(i=1..k) v_i) / k`：

`Context Precision@K = (Σ(k=1..K) P@k × v_k) / (Σ(k=1..K) v_k)`

该值为按排名加权的 Average Precision。越相关的片段越靠前，分数越高。若已完整标注且 `Σv_k = 0`，按边界约定直接返回 `0`，不执行上述除法。

**边界：**

- 没有检索片段：`unscored_empty_retrieval`。
- 有检索片段但没有任何相关片段：分数为 `0`，状态为 `scored`；这是“已评估但未命中”，不同于没有标签。
- 任一检索片段缺少相关性标签：`unscored_missing_annotation`，避免只评已标注前缀造成排名偏差。
- 只改变回答文本、保持 Query、检索片段和标签不变时，分数不变。

## Context Recall

运行时输入：`reference_evidence=[{"evidence_id":"e1","text":"原子事实"}]`；ID 与去除首尾空白的事实文本均不得重复。可提供与参考顺序一致的 `coverage_annotations: boolean[]` 和非空 `coverage_annotation_source`，否则由 Judge 判断。E2E JSON 题目直接使用这些字段；旧题目无参考项时保留未评分，不自动编造参考答案。

**评测问题：** 检索上下文覆盖了多少回答问题所需的参考证据？

**单位与标签：** 将 `reference_evidence` 表示为互不重复、可独立判断的原子事实/要求。对每项判断至少一个检索片段是否直接支持它，得到 `c_j ∈ {0,1}`。判断基于检索上下文，不基于模型最终回答。

**计算：**

令 `M` 为参考证据项数：

`Context Recall = (Σ(j=1..M) c_j) / M`

**边界：**

- 没有参考证据或参考清单为空：`unscored_missing_reference`，不把 Recall 记为 0 或 1。
- 参考证据有效但检索结果为空：分数为 `0`，状态为 `scored`。
- 此边界结果可由空检索这一确定事实直接计算，`evaluator.method=deterministic`、`model_id=null`、`prompt_version=null`；`judgments` 中每个参考证据项的 `covered=false`，`covered_count=0`，`reference_count` 等于参考证据项数。
- 同一证据被多个片段重复支持，只计一次。
- 证据是否被回答覆盖属于 Answer Completeness，不属于本项 Context Recall。

## Context Relevance

运行时入口：`_eval_context.eval_context_relevance(query, retrieved_docs, llm=judge)`。Judge 按文档原排名返回原子摘录和标签；实现校验摘录来源、文档完整性及标签类型，去除精确重复项并拒绝冲突标签。明细保留来源标识、文档排名和单元序号，不复制原文。单元拆分与同义去重由 Judge 判断，需人工抽样校准；超出 Judge 上下文预算时返回错误而不基于截断前缀打分。

**评测问题：** 返回上下文中有多少信息与 Query 相关？

**单位与标签：** Judge 先将所有检索片段拆成可独立判断的原子信息单元，排除纯标题、页眉页脚、空白和无语义格式标记；跨片段重复的同一信息只计一次。对每个信息单元仅根据 Query 判断相关性，得到 `r_i ∈ {0,1}`。不判断该信息是否正确、权威或已被回答。

**计算：**

令 `N` 为去重后可评估的信息单元数：

`Context Relevance Ratio = (Σ(i=1..N) r_i) / N`

此为安枢定义的“相关信息占检索信息总量比例”，范围为 0 到 1。它不是检索排序指标，也不依赖生成答案或参考答案。

**边界：**

- 没有检索片段：`unscored_empty_retrieval`。
- 检索片段存在但清洗、拆分、去重后没有可判断单元：`unscored_no_evaluable_units`。
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

## 后续实现复核要点

- Precision 输入的检索顺序、逐片段标签和计算明细可复核。
- Recall 的参考证据是原子、去重并对应 Query；没有参考证据时不产生伪分数。
- Relevance 只看 Query 和检索上下文，不看生成答案；信息单元拆分及去重结果可复核。
- 空输入、未标注、Judge 错误及启发式回退均保留不同状态。
- 结果包含 `score`、`status`、`explanation`、`details`、`evaluator`；报告仅聚合 `scored` 项并显示样本数及未评分原因。
- 未将历史目标值或未经人工抽样校准的 Judge 分数解释为发布门槛。

## 参考资料

- RAGAS, Context Precision: <https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/>
- RAGAS, Context Recall: <https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/>
- RAGAS, Available Metrics（列出 RAG Metrics 与 NVIDIA Metrics 的分组）: <https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/>

上述资料用于确认常见指标关注点与术语边界；具体公式、状态和示例以本契约中的安枢项目口径为准。
