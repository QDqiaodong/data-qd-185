# 排空采集事务审计 v6

实验台撤掉完整流水号后，探针只留下请求/应答两侧的握手信号、短标签与请求服务等级。标签可在上一个同标签请求完成前复用，应答允许乱序。本入口对一份从网表初值连续回放（不插复位）的普通 v1 输入重放，从**边沿前实际输出**提取两侧握手事务，在“首行前无未完成请求、末行后无待返回应答”的排空前提下做身份对账，并在所有完整配对之上给出迟到界与合规结论。入口 `logic-replay --audit-transfers <JSON> --output <JSON>`，与 `--source`、`--campaign`、`--qualify`、`--recover`、`--pack-shard`、`--rollup-shards` 互斥，不接收 `--approvals`。

## 输入

顶层恰好六个字段：`source`、`request`、`response`、`max_age`、`deadlines`、`late_budget`。

- `source`：一份完整的现有 v1 顺序回放输入（`inputs`/`gates`/`flops`/`outputs`/`vectors`，规则见 [v1 契约](contract.md)），1..1000 个向量，从网表初值连续跑、不插复位。所有被引用的信号必须出现在 `source.outputs` 中。
- `request`、`response`：两侧通道配置，各含 `valid`、`ready`、`tag_bits`；请求侧额外含 `class_bit`。
  - `valid`、`ready`、`class_bit` 是单个输出网名；`tag_bits` 是 1..8 个输出网名的数组，同一数组内不得重复引用。两侧标签等宽，按数组顺序**高位在前**拼成标签字符串（仅含 `"0"`/`"1"`，保留前导零）。
- `max_age`：1..999 的整数，应答周期减请求周期的合法闭区间为 `[1, max_age]`。
- `deadlines`：恰好含 `"0"`、`"1"` 两个键，分别是两种请求等级的最大**及时**延迟，均为 1..`max_age` 的整数。延迟严格大于期限才计迟到；延迟等于期限仍及时。
- `late_budget`：0..1000 的整数，本批允许的迟到笔数额度。

## 从事务波形取事件

周期是向量数组的零基下标，事件同时保留该行 `vector_id`。每周期对两侧通道**独立**读取原回放语义下的边沿前实际输出（`outputs_before_edge`），不读取期望值作为采样，也不因一般期望失配阻断审计。判定顺序与规则：

1. `valid`、`ready` 任一为字面 `"0"`：该侧本周期无握手，该侧其余位一律不检查（显式 `"X"` 也被压住）。
2. 没有 `"0"` 而 `valid` 或 `ready` 为 `"X"`：观测不足，原因 `handshake_unknown`。
3. 两者均为 `"1"`：确认一次握手。此后标签任一位为 `"X"` 记 `tag_unknown`；请求侧 `class_bit` 为 `"X"` 记 `class_unknown`。标签/等级只在握手已确认时检查。

任一观测不足都把整批标为 `unobservable`，列出全部问题周期、通道与原因，**不发布**配对、迟到界或合规结论；不会丢掉问题行后照常认证，也不会把 `"X"` 猜成二值。两侧可在同一周期各有一个事件，它们属于两个不同通道。

## 身份对账

一个完整配对必须把每个请求与每个应答各用恰好一次。兼容边要求标签相同且延迟在 `[1, max_age]`；超过该请求及时期限的边仍是合法身份。不按同标签内的时间先后强制先发先回。

- 观测充分但不存在完整配对（数量不齐或归属冲突）时标 `unpairable`，给出两侧事件表与二分图**最大可配笔数** `maximum_matching`，不产生任何确定配对或服务质量判定。
- 没有事务的采集拥有一个空配对（空候选、迟到界 `[0, 0]`）。
- 可以完整配对时，按请求顺序给出每个请求**真正可能**的应答候选（应答周期升序、附 `vector_id`）：一条兼容边只有在至少一种完整配对中能归给该请求才列入（用最大匹配上的交替路/交替圈判定，而不是只看局部兼容）。候选恰好一个的请求才进入 `confirmed_pairs` 供追溯系统消费；多个候选绝不任选一笔写入。

两侧事件表 `request_events`/`response_events` 分别按周期顺序列出已知 `tag`，请求事件另带握手时捕获的 `class`，均附 `cycle` 与 `vector_id`。

## 服务质量

在每种完整配对中，用请求握手时捕获的等级查 `deadlines`，延迟严格大于期限计迟到。逐笔 `tardiness` 标注：

- `always_timely`：该请求的全部候选都及时；
- `always_late`：全部候选都迟到；
- `either`：候选中两者皆有。

全批给出所有完整配对可达到的最少迟到 `minimum` 与最多迟到 `maximum`（在候选二分图上做两次 0/1 费用的最小费用完美指派，O(n³)），并为两个端点各附一份达到该数的完整配对证据 `minimum_witness`/`maximum_witness`，按请求顺序给出两侧周期，凭周期即可回查事件表。不会把彼此争用同一应答的局部可能性相加。

合规结论 `compliance.status` 与是否存在逐笔确定配对相互独立：

- `maximum <= late_budget`：`within_budget`；
- `minimum > late_budget`：`over_budget`（即使没有任何逐笔确定归属，可证明的超标也不隐去）；
- 其余：`needs_review`。

## 报告字段契约

`schema_version` 为 6，顶层按状态不同给出：

- 公共：`parameters`（`max_age`、`deadlines`、`late_budget`、`tag_width`）与 `summary`。
- `status="unobservable"`：`observation_issues` 数组，每项 `{cycle, vector_id, channel, reason}`，`channel` 为 `"request"`/`"response"`，`reason` 为 `handshake_unknown`/`tag_unknown`/`class_unknown`；不含事件表、配对与合规。
- `status="unpairable"`：`request_events`、`response_events` 与 `pairing.maximum_matching`；不含迟到判定。
- `status="pairable"`：`request_events`、`response_events`、`pairing`、`compliance`。
  - `request_events`/`response_events` 项：`{cycle, vector_id, tag[, class]}`。
  - `pairing.candidates` 项（按请求顺序）：`{request_cycle, request_vector_id, candidates[]}`，候选项 `{cycle, vector_id}`。
  - `pairing.confirmed_pairs` 项：`{request_cycle, request_vector_id, response_cycle, response_vector_id, tag}`。
  - `pairing.tardiness` 项：`{request_cycle, request_vector_id, verdict}`。
  - `pairing.late_range`：`{minimum, maximum, minimum_witness, maximum_witness}`，证据项为 `{request_cycle, response_cycle}`。
  - `compliance`：`{status, late_budget}`，`status` 为 `within_budget`/`over_budget`/`needs_review`。
  - `summary`：`{cycles, status, requests, responses, confirmed_pairs, minimum_late, maximum_late, compliance}`；不可观测与不可配对状态只含适用的计数字段。

仅重排 JSON 对象的键不改变候选、确定明细与极值证据；存在多种端点证据时不要求特定的一种。

## 校验与失败约定

整个输入在回放与分析前完整校验：未知/缺失字段、重复键与非有限常量、错误类型、越界数量、标签宽度不一致、`tag_bits` 重复引用、无效信号引用或非法网表（含组合环、多驱动）等都拒绝。非法输入或目标已存在（含符号链接）退出 2，不覆盖旧目标、不留下半份报告；沿用 ≤8 MB 输入限制与同目录临时文件原子硬链接发布。观测不足、无法全配、超标与待复核均是可交付分析，退出 0 并写完整报告。全程离线，仅用标准库。

## 分钟交接样例与复现

`fixtures/audit/handoff-512.json` 由 `tools/generate_audit_handoff.py` 生成：连续 512 行、无触发器，前 256 行每行确认一个同标签同等级请求，后 256 行每行确认一个同标签应答，另一侧均无握手，向量 ID 为短序号；`max_age=511`，两种等级期限均为 256。该样例中每个请求都有 256 个可行应答，迟到界为 `[0, 255]`，自动化测试核对完整候选集与两个端点证据（不只看正常退出）。

复现（单核 Linux、CPython 3.11.2、离线）：

```sh
python3 -I tools/generate_audit_handoff.py fixtures/audit/handoff-512.json
python3 -I -m logic_replay.cli --audit-transfers fixtures/audit/handoff-512.json \
    --output run/handoff-audit.json
```

在 256 MiB 进程地址空间限制下从读入到发布报告约 2 秒（要求 60 秒内）；内存峰值远低于限制。该耗时承诺仅针对此样例形态，不为所有网表给出统一耗时承诺。
