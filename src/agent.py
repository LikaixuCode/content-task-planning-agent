import json
import os

from .business import PlanningTools


def run_mock(service: PlanningTools) -> dict:
    """脚本驱动离线流程，不代表真实模型决策。"""
    terminal = {"status": "completed", "source": "scripted_mock"}
    if service.state["plan"]:
        return service.output(terminal)
    if service.state["materials"] is None:
        result = service.call("search_materials", {"reason": "检索主题相关素材"})
        if not result["ok"]:
            return service.output({**terminal, "status": "incomplete", "reason": result["code"]})
    if not service.state["materials"]:
        return service.output(terminal)
    material_id = service.state["materials"][0]["id"]
    failed_estimates = {
        event["result"].get("estimate_id") for event in service.state["events"]
        if event["tool"] == "generate_script" and event["result"].get("status") == "failed"
    }
    failed_estimates.difference_update(script["estimate_id"] for script in service.state["scripts"].values())
    for variant in service.catalog.variants:
        estimate = next((value for value in service.state["estimates"].values() if value["material_id"] == material_id and value["variant"] == variant), None)
        if estimate and estimate["estimate_id"] in failed_estimates:
            continue
        if not estimate:
            result = service.call("estimate_cost", {"material_id": material_id, "variant": variant, "reason": "估算候选方案的费用与耗时"})
            if not result["ok"]:
                if service.limit_hit:
                    return service.output({**terminal, "status": "incomplete", "reason": "max_tool_calls"})
                continue
            estimate = service.state["estimates"][result["estimate_id"]]
        if not estimate["feasible"]:
            continue
        script = next((value for value in service.state["scripts"].values() if value["estimate_id"] == estimate["estimate_id"]), None)
        if not script:
            result = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "为可行方案生成活动脚本"})
            if not result["ok"]:
                if service.limit_hit:
                    return service.output({**terminal, "status": "incomplete", "reason": "max_tool_calls"})
                continue
            script = service.state["scripts"][result["script_id"]]
        result = service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": script["script_id"], "reason": "保存满足预算和截止时间的方案"})
        if service.limit_hit:
            return service.output({**terminal, "status": "incomplete", "reason": "max_tool_calls"})
        return service.output(terminal)
    return service.output(terminal)


async def run_glm(service: PlanningTools, max_turns: int = 12) -> dict:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ClaudeSDKError,
        ResultError,
        ResultMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        create_sdk_mcp_server,
        tool,
    )

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        return service.output({"status": "incomplete", "reason": "missing_credential"})

    def response(name: str, args: dict) -> dict:
        result = service.call(name, args)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "is_error": not result["ok"]}

    @tool("search_materials", "检索本次主题的合成素材及可选方案类型。", {"reason": str})
    async def search_materials(args):
        return response("search_materials", args)

    @tool("estimate_cost", "估算选择的素材和方案的费用、工期与可行性。", {"material_id": str, "variant": str, "reason": str})
    async def estimate_cost(args):
        return response("estimate_cost", args)

    @tool("generate_script", "根据可行估算生成合成活动脚本。", {"estimate_id": str, "reason": str})
    async def generate_script(args):
        return response("generate_script", args)

    @tool("save_plan", "保存匹配的可行估算与成功脚本。", {"estimate_id": str, "script_id": str, "reason": str})
    async def save_plan(args):
        return response("save_plan", args)

    names = ("search_materials", "estimate_cost", "generate_script", "save_plan")
    server = create_sdk_mcp_server(name="planning", tools=[search_materials, estimate_cost, generate_script, save_plan])
    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[f"mcp__planning__{name}" for name in names],
        mcp_servers={"planning": server},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        max_turns=max_turns,
        model=os.environ.get("GLM_MODEL", "glm-5.3-flash"),
        env={
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "API_TIMEOUT_MS": "60000",
        },
        system_prompt=(
            "你是内容活动规划助手。只能使用提供的业务工具；仅保存方案，不执行真实活动。"
            "先检索素材，优先考虑增强版 enhanced，先估算再生成脚本。"
            "若估算不满足预算或截止时间，考虑其他可选方案并重新估算；没有可行方案就停止。"
            "若工具失败，可选择其他方案重新估算或停止；保留成功结果，不得称失败步骤成功。"
            "保存需要同一估算的成功脚本。上下文中已有成功结果可以直接复用。"
            "显式续跑是新的执行轮次，历史中的 max_tool_calls 限制已经重置；依据现有结果继续选择工具或说明无法完成的原因。"
            "如果依赖已变更，历史中的旧估算或脚本仅供参考，必须使用当前保存的结果或重新调用工具。"
            "每次工具调用的 reason 用一句话说明公开的决策依据，不输出内部思维过程。"
        ),
    )
    previous = service.state["events"][:service.run_start]
    prompt = {
        "request": service.request.model_dump(mode="json"),
        "effective_start_at": service.start_at.isoformat(),
        "saved_materials": service.state["materials"],
        "saved_estimates": list(service.state["estimates"].values()),
        "saved_scripts": list(service.state["scripts"].values()),
        "previous_tool_results": [{"tool": event["tool"], "result": event["result"]} for event in previous],
        "saved_plan": service.state["plan"],
        "current_run": {"is_resume": service.is_resume, "tool_calls_used": 0, "max_tool_calls": service.max_tool_calls, "invalidated_dependencies": service.invalidated_dependencies},
    }
    terminal = {"status": "incomplete", "reason": "no_result", "model": options.model}
    model_calls = []
    sdk_events = []
    reported_models = set()
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(json.dumps(prompt, ensure_ascii=False))
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    if message.model:
                        reported_models.add(message.model)
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            model_calls.append(block.name)
                            sdk_events.append({"type": "tool_request", "tool_use_id": block.id, "message_id": message.message_id, "tool": block.name, "arguments": block.input})
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            sdk_events.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "is_error": block.is_error})
                elif isinstance(message, ResultMessage):
                    terminal = {
                        "status": "completed" if not message.is_error and message.subtype == "success" else "incomplete",
                        "reason": message.terminal_reason or message.subtype,
                        "turns": message.num_turns,
                        "model": options.model,
                    }
                    if terminal["status"] == "incomplete":
                        terminal["api_error_status"] = message.api_error_status
                        terminal["detail"] = (message.result or "").replace(token, "[REDACTED]")[:500]
    except ResultError as exc:
        terminal = {"status": "incomplete", "reason": exc.terminal_reason or exc.subtype or "sdk_result_error", "model": options.model}
    except ClaudeSDKError as exc:
        terminal = {"status": "incomplete", "reason": type(exc).__name__, "model": options.model}
    except Exception as exc:
        terminal = {"status": "incomplete", "reason": type(exc).__name__, "model": options.model}
    if not model_calls and terminal["status"] == "completed" and not service.state["plan"]:
        terminal = {**terminal, "status": "incomplete", "reason": "no_model_tool_calls"}
    terminal["reported_models"] = sorted(reported_models)
    terminal["sdk_events"] = sdk_events
    return service.output(terminal)
