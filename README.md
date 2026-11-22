# data-qd-185 · 数字电路测试向量回放

编译一位门级连线，在同步触发器模型上回放三值测试向量，记录逐周期状态、期望对账和门输出活动。另提供故障覆盖排程：在周期预算内选择片段执行顺序与复位点，最大化单固定电平故障检出。另提供跨版本资格认定：同一组向量在参考版与候选版上各自回放，核对授权清单，判定候选版是否可发布。另提供分片归档与整批更正对账：无复位长采集按时钟切给多个离线工位，中心仅凭可归档分片摘要完成归并、对账与精确更正。另提供排空采集事务审计：从握手、短标签与请求等级的边沿前采样提取事务，做完整配对对账、迟到界与合规判定。

## 独立安装

Python >=3.11，实际验证使用本地CPython3.12.14。运行与unittest仅用标准库，不依赖外部服务。每仓建立独立虚拟环境；构建工具锁定版本，公共包缓存可复用。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

离线安装需要已安装的兼容Python及缓存中的setuptools==80.9.0、wheel==0.45.1。若缓存缺失，应先准备公共构建工具，不能把以上命令当空缓存可用承诺。修改源码后须重新安装本仓。

## 演示

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/logic-replay --source fixtures/input.json --output run/report.json
```

预期：4周期、2门、1触发器、4次比较、0处不匹配，最终q=X。

故障覆盖排程演示（与 `--source` 二选一的 `--campaign` 入口）：

```sh
.venv/bin/logic-replay --campaign fixtures/campaign.json --output run/campaign.json
```

预期：选中 `prime` 后直接承接 `observe`（不复位），总成本2，检出 `g` 固定"0"故障，见证在 `observe`/`o0` 的 `q`（参考"1"、故障"0"）。若把 `prime` 的输入改为 "0"，原排程参考侧不合格，最优解变为零成本空排程。

跨版本资格认定演示（与 `--source`、`--campaign` 互斥的 `--qualify` 入口，须同时给 `--approvals`）：

```sh
.venv/bin/logic-replay --qualify fixtures/qualify.json --approvals fixtures/approvals.json --output run/qualify.json
```

预期：参考契约有效，候选版在 `v11`/`g` 的偏差（参考"1"、候选"0"）获 COURSE-142 授权记为已批准变更，0 未批准回归、0 过期授权，`eligible=true`。把 `--approvals` 换成空清单则同一偏差成为未批准回归，`eligible=false`，仍退出0并出报告。

采集恢复与回放包导出演示（与前述模式互斥、不得搭配 `--approvals` 的 `--recover` 入口）：

```sh
.venv/bin/logic-replay --recover fixtures/recover.json --output run/recover.json
```

预期：采集一致，`t0` 的漏记输入被 `t3` 的晚到采样唯一补回为"1"，其余格原样记录。窗口 `review-t2` 交接成功，入口状态 `q="1"`，须预热 `t0`、`t1`（仅 `t1` 从初值出发到不了该状态）；窗口 `review-t4` 只预热 `t3`，该预热行 `expect` 为空。两个窗口都带一个可保存为 JSON、直接喂给 `--source` 的 `replay_package`：

```sh
.venv/bin/python -c 'import json;print(json.dumps(json.load(open("run/recover.json"))["windows"][1]["replay_package"]))' > run/package-t4.json
.venv/bin/logic-replay --source run/package-t4.json --output run/package-t4-report.json
```

预期回放包自身全部期望匹配，窗口首周期的边沿前状态等于报告的 `entry_state`；预热期间的未采样输出没有被填成期望。

分片归档与整批更正对账演示（与前述模式互斥、不得搭配 `--approvals` 的 `--pack-shard` 与 `--rollup-shards` 入口）。三个独立工位只拿到各自分段（原始刺激留在工位），分别打包后只把归档交给中心。清单中的相对路径以清单所在目录为准，因此先建 `run/shard/` 并把归档直接发布进去：

```sh
mkdir -p run/shard
.venv/bin/logic-replay --pack-shard fixtures/shard/pack-A.json --output run/shard/archive-A.json
.venv/bin/logic-replay --pack-shard fixtures/shard/pack-B.json --output run/shard/archive-B.json
.venv/bin/logic-replay --pack-shard fixtures/shard/pack-C.json --output run/shard/archive-C.json
cp fixtures/shard/manifest.json run/shard/manifest.json
```

电路为触发器 `q`（初值 "0"，D 接输入 `a`）加门 `g=BUF(q)`、`h=BUF(b)`，只观测 `g`；A/B/C 覆盖周期 0–1、2、3，仅 B 对 `g` 写期望 "0"。中心用清单归并，旧汇总在周期 2 的实际为 "1" 并失配：

```sh
.venv/bin/logic-replay --rollup-shards run/shard/manifest.json --output run/shard/rollup-old.json
```

后来只更正 A 最后一个周期的 `a`（"1"→"0"）：工位重新打包 A，B、C 不重打包、不重传原始数据。中心用带 `replace` 的清单同时产出旧/新两份完整汇总和异常队列事件——周期 2 的失配被精确撤销（`action="remove"`，保留旧记录），B 末态与 C 的结果不变：

```sh
.venv/bin/logic-replay --pack-shard fixtures/shard/pack-A-corrected.json --output run/shard/archive-A-corrected.json
cp fixtures/shard/manifest-corrected.json run/shard/manifest-corrected.json
.venv/bin/logic-replay --rollup-shards run/shard/manifest-corrected.json --output run/shard/rollup-corrected.json
```

把同一刺激的最后一个 `a` 改为 `"X"` 时，同一失配键会得到一条 `update` 事件（实际值 "1"→"X"），而不是先撤销再新增；只更正与触发器无关的输入 `b` 时，所有状态、期望与异常队列不变，但门 `h` 的 `known_transitions` 会因 `"X"` 隔断接缝而下降，反向更正后 `"X"` 从 `values_seen` 消失。

排空采集事务审计演示（与前述模式互斥、不得搭配 `--approvals` 的 `--audit-transfers` 入口）。探针只留握手、短标签与请求等级，本模式从边沿前实际输出提取事务，在“首行前无未完成请求、末行后无待返回应答”的前提下做完整配对对账、迟到界与合规判定：

```sh
.venv/bin/logic-replay --audit-transfers fixtures/audit/handoff-512.json --output run/handoff-audit.json
```

样例连续 512 行：前 256 行每行一个同标签同等级请求、后 256 行每行一个同标签应答，`max_age=511`、两种等级期限均为 256。预期 `status="pairable"`，每个请求有 256 个可行应答、确定配对明细为空，迟到界 `[0, 255]`，`compliance="within_budget"`。观测不足（`"X"` 未被 `"0"` 压制等）报 `unobservable`、无法完整配对报 `unpairable`、超标报 `over_budget`、无法断定是否超标报 `needs_review`，均退出 0 并出完整报告。字段契约见[审计契约](docs/audit.md)。

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

## 数据契约和边界

不是HDL/FPGA工具，没有延迟或模拟电压；X是未知值，期望中的*才表示不比较。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)；排程输入与报告见[排程契约](docs/campaign.md)；资格认定输入、授权与报告见[资格认定契约](docs/qualify.md)；采集恢复与回放包见[恢复契约](docs/recover.md)；分片归档、清单与更正对账见[分片契约](docs/shard.md)；排空采集事务审计见[审计契约](docs/audit.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
