# 跨版本资格认定 v3

使用者为数字逻辑课程维护人员。课程测试向量是已发布的行为契约：升级电路后，需要确认旧版确实符合这份契约，并区分候选版的意外回归与经课程负责人批准的有意行为变更，才能决定是否发布候选版。v1 在单个网表上回放向量（见[v1 契约](contract.md)），v2 在单个网表上排程故障覆盖（见[排程契约](campaign.md)）；v3 在同一组向量上对比两个版本的网表，不做任何排程或故障注入。

入口 `logic-replay --qualify <JSON> --approvals <JSON> --output <JSON>`。`--qualify` 与 `--source`、`--campaign` 互斥，必须且只能给一个模式；`--approvals` 只在 `--qualify` 下可用且为必需，搭配其他模式或缺失都退出 2。

## 资格认定输入

顶层恰好三个字段 `reference`、`candidate`、`vectors`：

- `reference` / `candidate`：各为 `{"revision": ..., "netlist": ...}`，不允许其他字段。`revision` 是非空字符串（≤200 字符），两版 revision 在本次输入中必须不同。`netlist` 恰好含 `inputs`/`gates`/`flops`/`outputs`，结构与校验规则和 v1 完全相同，不得再出现 `vectors`。
- 两版的输入网名集合与观测输出集合必须相同；声明顺序不同（排列）不构成差异，门与触发器结构可以不同。接口不一致是输入错误。
- `vectors`：1..1000 条向量，格式与 v1 相同：唯一 `id`、完整 `inputs` 映射、`expect` 子集，期望值为字符串 `"0"`、`"1"`、`"X"` 或 `"*"`。

两版各自从自己的触发器初值开始，按向量数组顺序连续回放，向量之间不复位；三值门逻辑、边沿前观测、边沿同时更新语义与 v1 相同。

## 授权清单输入

`--approvals` 指向一份独立 JSON 文件，顶层恰好 `{"approvals": [...]}`，空数组合法（0..1000 项）。每项恰好 8 个字段：

- `reference_revision` / `candidate_revision`：非空字符串，格式与资格认定输入的 revision 相同。
- `vector_id` / `output`：分别沿用向量 id 与观测输出网名的格式。
- `reference_value` / `candidate_value`：只能是字符串 `"0"`、`"1"` 或 `"X"`，且两者必须不同。
- `ticket` / `reason`：去除首尾空白后非空的字符串。

结构校验只看格式，不检查这些标识是否匹配本次资料。同一 `(reference_revision, candidate_revision, vector_id, output)` 四元组最多一项授权；重复授权或格式非法的授权是输入错误，退出 2 且不生成报告。

## 对账与判定

每个写有非通配 `expect`（即 `"0"`、`"1"` 或 `"X"`）的向量/输出单元是一个对账单元，分别比较参考实际值与期望、候选实际值与期望、两版实际值：

- 参考版任一单元不满足明确期望时，`reference_contract="invalid"`；候选版回放仍完成并列出差异，但候选版不得取得可发布资格。
- 可授权偏差：写有明确期望、候选实际值不满足期望、且两版实际值不同的单元。授权必须精确匹配本次两版 revision、向量 id、输出名及两版实际值，才计为已批准变更。
- 候选版不满足明确期望且未获有效授权的单元列为未批准回归；候选版满足期望的单元无需授权。
- `"*"` 与漏填的期望不参与对账与回归判定，也不能作为授权对象。
- 格式合法但版本或数值不匹配、向量或输出不存在、或目标不是可授权偏差的授权，列入过期/不匹配授权并阻止发布。

`eligible=true` 当且仅当：参考契约有效、没有未批准回归、且没有过期/不匹配授权。资格不通过是业务结论而非输入错误：退出 0，正常生成完整报告。

## 报告

`schema_version` 为 3，字段：

- `reference_revision` / `candidate_revision`：两版 revision。
- `reference_contract`：`"valid"` 或 `"invalid"`。
- `eligible`：最终资格布尔量。
- `reference` / `candidate`：各含 `revision` 与 `trace`。trace 按向量数组顺序，每周期含 `cycle`、`vector_id`、`state_before_edge`、`outputs_before_edge`、`state_after_edge`；状态映射按触发器名排序，输出映射按该版 `outputs` 声明顺序。
- `reconciliation`：全部对账单元，按（周期、参考版 `outputs` 声明顺序）排列。每单元含 `cycle`、`vector_id`、`output`、`expected`、`reference_value`、`candidate_value`、`reference_matches`、`candidate_matches`、`versions_differ` 及 `status`（`match` / `approved_change` / `unapproved_regression`）。
- `differences`：两版实际值不同的全部观测单元（不限于对账单元），排列顺序同上，每项含 `cycle`、`vector_id`、`output`、`reference_value`、`candidate_value`。
- `approved_changes`：已批准变更单元，含 `cycle`、`vector_id`、`output`、`expected`、两版实际值及授权的 `ticket`、`reason`。
- `unapproved_regressions`：未批准回归单元，含 `cycle`、`vector_id`、`output`、`expected` 及两版实际值。
- `stale_approvals`：过期/不匹配授权，按授权数组顺序，每项回显 8 个授权字段并附 `detail`：`revision_mismatch`（版本不匹配）、`unknown_vector`（向量不存在）、`unknown_output`（输出不存在）、`no_explicit_expectation`（目标单元是通配或漏填期望）、`not_a_deviation`（目标不是可授权偏差）、`value_mismatch`（实际值不符）。
- `summary`：两版 revision、周期数、对账单元数、参考契约状态、两版各自失配数、版本差异数、授权总数、已批准变更数、未批准回归数、过期授权数及 `eligible`，与 CLI 标准输出打印的内容一致。

确定性：同一输入内容只改变 JSON 对象键顺序时，报告完全一致；向量数组顺序决定回放与报告顺序，不被排序改写。

## 校验与失败约定

两个输入文件各自沿用 v1 限制（≤8MB、拒绝重复键与非有限常量）；未知字段、重复向量 id、非法网表/向量/授权字段、两版接口不一致、相同 revision、重复授权等都属输入错误。输入错误或目标路径已存在（含符号链接）退出 2，不覆盖旧目标、不留下半份报告；结构合法的分析（包括资格不通过）退出 0 并原子新建完整报告。全程离线，没有 HDL 解析、硬件访问或随机向量生成。
