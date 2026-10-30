# 内容任务规划 Agent

题号 2。这个单 Agent 命令行项目根据主题、预算和截止时间规划内容活动。Claude Agent SDK 驱动 GLM 读取工具结果并选择后续动作；本地代码负责成本、工期、步骤依赖和方案保存。

素材、费用、工期和脚本都是合成数据。活动费用是模拟执行预算，不是模型 API 账单；项目只保存方案，不执行或发布活动。

## 环境与安装

需要 Python 3.12 和 `uv`。项目锁定 Claude Agent SDK 0.2.175，SDK 使用内置 Claude Code 运行时。

```bash
uv sync --locked --python 3.12
uv run --locked python -m pytest -q
```

## 离线运行

Mock 不需要密钥，用固定脚本调用相同业务工具，只能验证流程，不能证明模型决策能力：

```bash
uv run --locked python -m src.main run --mode mock --input examples/normal.input.json --db .runtime/normal.sqlite3
uv run --locked python -m src.main run --mode mock --input examples/replan.input.json --db .runtime/replan.sqlite3
uv run --locked python -m src.main run --mode mock --input examples/missing.input.json --db .runtime/missing.sqlite3
```

模拟一次脚本失败，限制本次实际工具调用为 3 次，再显式续跑：

```bash
uv run --locked python -m src.main run --mode mock --input examples/failure.input.json --db .runtime/resume.sqlite3 --fail-once generate_script --max-tool-calls 3
uv run --locked python -m src.main run --mode mock --input examples/failure.input.json --db .runtime/resume.sqlite3 --fail-once generate_script --resume
```

第一条命令返回 `incomplete` 和非零退出码；第二条复用已成功结果，继续处理未完成步骤。相同请求默认返回上次结果；要续跑未完成请求必须显式传 `--resume`。相同请求 ID 配不同输入会报冲突。如果续跑前素材或费用表发生变化，依赖这些数据的旧结果会失效并重新计算，历史记录仍保留。

## 真实 GLM 运行

复制 `.env.example` 为 `.env` 并在本地填写密钥，再将变量载入终端：

```bash
cp .env.example .env
# 在本地编辑 .env，填写 ANTHROPIC_AUTH_TOKEN
set -a
. ./.env
set +a
uv run --locked python scripts/sdk_smoke.py
uv run --locked python -m src.main run --mode glm --input examples/replan.input.json --db .runtime/glm-replan.sqlite3
```

真实模型的失败与续跑可使用以下命令；第一次运行可能因上限或模型停止而未完成，续跑会读取同一个数据库：

```bash
uv run --locked python -m src.main run --mode glm --input examples/failure.input.json --db .runtime/glm-resume.sqlite3 --fail-once generate_script --max-tool-calls 3
uv run --locked python -m src.main run --mode glm --input examples/failure.input.json --db .runtime/glm-resume.sqlite3 --fail-once generate_script --resume
```

默认模型是 `glm-5.3-flash`，请求地址是 `https://open.bigmodel.cn/api/anthropic`。真实模式不自动切换到 Mock；缺密钥、模型错误或达到上限时返回 `incomplete`。

智谱支持 [Claude Code 接入 Coding Plan](https://docs.bigmodel.cn/cn/coding-plan/tool/claude)，其 [FAQ](https://docs.bigmodel.cn/cn/coding-plan/faq) 对自建应用要求使用标准 API。SDK 嵌入业务 Agent 的套餐适用范围需以智谱规则为准，接口调用成功不代表套餐适用已经确认。

## 输入与输出

输入 JSON 需要 `request_id`、`topic`、`budget` 和带时区的 `deadline`，可选带时区的 `start_at`。金额使用十进制，输出为字符串。未指定起始时间时，在首次创建请求时记录当前时间；重复执行沿用该时间。工期按起始时间连续累加，是模拟值，不计算工作日和外部系统的实时耗时。

输出包含方案、模拟费用明细与总额、预计完成时间、实际工具步骤与结果、保存状态、未完成原因和 Trace。方案任务的 `planned` 表示计划待执行。`candidate_gaps` 给出不可行方案各自的预算或工期缺口。退出码 `0` 表示方案已保存且本次执行完成；`1` 表示未完成；`2` 表示输入或请求冲突等错误。

`--max-turns` 默认为 12，限制 SDK Agent 轮次，不等于工具调用次数；`--max-tool-calls` 默认为 20，限制实际业务工具执行次数。超过工具调用上限的尝试会记为 `rejected`，不会执行业务逻辑。每个工具调用的 Trace 包含参数、结果、后续动作和简短依据。超限或 SDK 错误中断时末步标记 `interrupted`；模型自行结束时如实记录 `finish`，如果次数用尽且方案未保存，整体仍返回未完成。

## 评审入口

- [需求与验收](docs/01-requirements.md)、[方案设计](docs/02-solution.md)、[测试与实际结果](docs/03-evaluation.md)
- [AI 使用](docs/04-ai-usage.md)、[复盘](docs/05-retrospective.md)
- `examples/*.input.json` 是输入，`examples/*.glm.output.json` 是真实模型运行结果，`examples/*.mock.output.json` 是离线运行结果；输出内含实际 Trace。

SDK 工具注册参考 [Claude Agent SDK 官方文档](https://code.claude.com/docs/en/agent-sdk/custom-tools)。题目交付结构参考 [agent-campus-submission-example](https://github.com/lwz1357/agent-campus-submission-example)；该示例不作为真实模型决策的证明。
