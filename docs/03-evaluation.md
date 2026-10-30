# 测试与实际结果

## 自动化测试

在 Python 3.12.7 下执行 `uv run --locked python -m pytest -q`，结果为 **32 passed**。完整输出见 [测试记录](../examples/test-results.txt)。

测试覆盖正常保存、缺预算或截止时间、预算与时间不可行、方案重估、工具失败、重复请求、显式续跑、方案保存失败、SDK 轮次与工具调用上限。额外检查了十进制费用、步骤依赖、依赖数据变更、保存前重查、写入结果不确定后的状态查询，以及保存后输出中断的恢复。

## 真实 GLM 运行

使用 Claude Agent SDK 0.2.175、其内置 Claude Code 2.1.314 和智谱 Anthropic 兼容接口。以下业务示例的模型标识由 SDK 报告为 `glm-5.3-flash`；素材、费用、耗时、脚本和存储均为本地合成数据。

| 场景 | 实际结果 | 输入、输出与 Trace |
| --- | --- | --- |
| 正常完成 | 增强方案估算为 12，生成脚本并保存 | [输入](../examples/normal.input.json)、[输出](../examples/normal.glm.output.json) |
| 预算重规划 | 预算为 10，增强方案估算为 12 后，模型改选精简方案，重新估算为 8 并保存 | [输入](../examples/replan.input.json)、[输出](../examples/replan.glm.output.json) |
| 工具失败 | 注入一次脚本失败；模型重新估算精简方案，成功生成并保存，同时保留失败和此前成功记录 | [输入](../examples/failure.input.json)、[输出](../examples/failure.glm.output.json) |
| 失败后显式续跑 | 初次脚本失败后停止；续跑复用素材结果，并生成、保存可行方案 | [输入](../examples/failure.input.json)、[首次输出](../examples/resume-start.glm.output.json)、[续跑输出](../examples/resume.glm.output.json) |

真实输出中的 `trace` 包含工具参数、实际结果和后续动作；`terminal.sdk_events` 包含 SDK 工具请求及结果的对应 ID。预算重规划和工具失败的两条记录中，相关结果返回后，模型才发出下一次估算请求。

一次单独的真实调用发生请求超时，未执行工具或保存方案；实际结果记录在 [API 超时输出](../examples/api-error.glm.output.json)。随后显式续跑并设置工具上限为 1：模型调用一次素材工具后自行结束，程序识别到上限已用完且方案未保存，返回 `incomplete/max_tool_calls`；SDK 自身结束状态为 `completed`，Trace 如实标为 `finish`。见[真实上限记录](../examples/limit.glm.output.json)。这些记录不代表接口长期稳定性。

## 离线结果

以下结果由本地脚本驱动，不能证明模型的自主决策能力：

- [正常](../examples/normal.mock.output.json)、[预算重规划](../examples/replan.mock.output.json)、[缺少输入](../examples/missing.mock.output.json)
- [无可行预算](../examples/no-feasible.mock.output.json)、[截止时间不足](../examples/deadline.mock.output.json)、[脚本失败后调整](../examples/failure.mock.output.json)
- [重复请求](../examples/duplicate.mock.output.json)、[中断](../examples/resume-start.mock.output.json)及[续跑](../examples/resume.mock.output.json)
- [保存失败](../examples/save-failure.mock.output.json)及[续跑保存](../examples/save-resume.mock.output.json)、[工具调用上限](../examples/limit.mock.output.json)

缺少输入时程序在模型和工具启动前结束，因此该示例的 Trace 为空，`terminal.status` 为 `not_started`。其他输出包含实际工具执行记录；重复提交返回第一次的方案 ID 和原有步骤。

## 限制与用时

费用是模拟活动方案的费用，不包含真实模型 API 的成本。工期是从固定起始时间连续累加的模拟值。项目没有接入真实素材、发布渠道、工作日历或生产存储，也没有验证多进程并发和长期稳定性。

本机原有代理路由无法连接智谱接口，本次验证使用只对进程生效的临时转发。调用成功不证明 SDK 业务 Agent 适用 Coding Plan 套餐额度，具体规则见 [智谱 FAQ](https://docs.bigmodel.cn/cn/coding-plan/faq)。

本次开发与本地验证为 2026-10-30 22:39 至 23:12（北京时间），约 33 分钟；不包含后续仓库创建、推送和远端复核。
