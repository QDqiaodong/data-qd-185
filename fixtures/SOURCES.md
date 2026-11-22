本目录为本项目原创合成样例，数据与代码采用MIT许可。不是观测/生产/个人真实数据，不需网络或外部服务。

`shard/` 子目录为 v5 分片归档与整批更正对账的原创样例：`pack-*.json` 是三个独立工位的分段打包输入（原始刺激只在工位侧），`archive-*.json` 是由 `--pack-shard` 实际生成、带校验和的归档，`manifest*.json` 是中心清单，其中 `manifest-corrected.json` 演示只替换 A 段、B/C 不重传的整批更正。
