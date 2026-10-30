"""Verify SDK-to-model and one in-process tool call."""

import asyncio
import json
import os

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, tool


@tool("lookup_cost", "查询示例方案的模拟成本。", {"variant": str})
async def lookup_cost(args):
    return {"content": [{"type": "text", "text": json.dumps({"variant": args["variant"], "cost": "12"})}]}


async def main():
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        print(json.dumps({"status": "missing_credential"}))
        return 1
    server = create_sdk_mcp_server(name="smoke", tools=[lookup_cost])
    options = ClaudeAgentOptions(
        tools=[], allowed_tools=["mcp__smoke__lookup_cost"], mcp_servers={"smoke": server},
        strict_mcp_config=True, setting_sources=[], permission_mode="dontAsk", max_turns=4,
        model=os.environ.get("GLM_MODEL", "glm-5.3-flash"),
        env={"ANTHROPIC_AUTH_TOKEN": token, "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_MAX_RETRIES": "0", "API_TIMEOUT_MS": "60000"},
        system_prompt="调用 lookup_cost 查询 enhanced 方案，再根据结果回答。",
    )
    called = []
    returned = []
    terminal = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query("请查询增强版方案的模拟成本。")
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    called.extend({"name": block.name, "id": block.id} for block in message.content if isinstance(block, ToolUseBlock))
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    returned.extend({"id": block.tool_use_id, "is_error": block.is_error, "content": str(block.content)[:300]} for block in message.content if isinstance(block, ToolResultBlock))
                elif isinstance(message, ResultMessage):
                    terminal = {"subtype": message.subtype, "is_error": message.is_error, "reason": message.terminal_reason, "turns": message.num_turns}
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": str(exc).replace(token, "[REDACTED]")[:300]}, ensure_ascii=False))
        return 1
    call_ids = {item["id"] for item in called if item["name"] == "mcp__smoke__lookup_cost"}
    success = bool(terminal and not terminal["is_error"] and any(item["id"] in call_ids and not item["is_error"] and "12" in item["content"] for item in returned))
    print(json.dumps({"status": "ok" if success else "incomplete", "tool_calls": called, "tool_results": returned, "terminal": terminal}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
