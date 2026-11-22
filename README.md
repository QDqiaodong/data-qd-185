# data-qd-185 · 数字电路测试向量回放

编译一位门级连线，在同步触发器模型上回放三值测试向量，记录逐周期状态、期望对账和门输出活动。另提供故障覆盖排程：在周期预算内选择片段执行顺序与复位点，最大化单固定电平故障检出。另提供跨版本资格认定：同一组向量在参考版与候选版上各自回放，核对授权清单，判定候选版是否可发布。

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

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

## 数据契约和边界

不是HDL/FPGA工具，没有延迟或模拟电压；X是未知值，期望中的*才表示不比较。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)；排程输入与报告见[排程契约](docs/campaign.md)；资格认定输入、授权与报告见[资格认定契约](docs/qualify.md)；采集恢复与回放包见[恢复契约](docs/recover.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
