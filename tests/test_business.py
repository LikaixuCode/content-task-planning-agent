import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage

from src.agent import run_glm, run_mock
from src.business import PlanningTools
from src.models import Catalog, Material, PlanningRequest
from src.storage import PlanningStore


ROOT = Path(__file__).resolve().parent.parent


def make_service(tmp_path, example="normal", maximum=20):
    request = PlanningRequest.model_validate_json((ROOT / "examples" / f"{example}.input.json").read_text())
    materials = [Material.model_validate(item) for item in json.loads((ROOT / "data/materials.json").read_text())]
    catalog = Catalog.model_validate_json((ROOT / "data/costs.json").read_text())
    store = PlanningStore(tmp_path / "state.sqlite3")
    record, _ = store.register(request)
    return PlanningTools(request, record, materials, catalog, store, "mock", maximum)


def test_estimate_requires_retrieved_material_and_uses_decimal_math(tmp_path):
    service = make_service(tmp_path, "replan")
    assert service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced"})["code"] == "material_not_retrieved"
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    assert estimate["total_cost"] == "12" and estimate["budget_gap"] == "2" and not estimate["feasible"]
    blocked = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    assert blocked["code"] == "plan_infeasible" and not service.state["scripts"]
    service.store.close()


def test_save_rechecks_cost_and_requires_matching_script(tmp_path):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    assert service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": "missing"})["code"] == "dependency_missing"
    script = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    service.catalog.variants["enhanced"].cost = Decimal("20")
    rejected = service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": script["script_id"], "reason": "保存"})
    assert rejected["code"] == "plan_infeasible" and service.state["plan"] is None
    assert service.store.get(service.request.request_id)["state"]["plan"] is None
    service.store.close()


def test_save_rejects_script_from_a_different_estimate(tmp_path):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    enhanced = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    lean = service.call("estimate_cost", {"material_id": "M01", "variant": "lean", "reason": "估算"})
    script = service.call("generate_script", {"estimate_id": lean["estimate_id"], "reason": "生成"})
    result = service.call("save_plan", {"estimate_id": enhanced["estimate_id"], "script_id": script["script_id"], "reason": "保存"})
    assert result["code"] == "dependency_missing" and service.state["plan"] is None
    service.store.close()


def test_tool_limit_rejects_extra_call_without_business_side_effect(tmp_path):
    service = make_service(tmp_path, maximum=1)
    assert service.call("search_materials", {"reason": "查找"})["ok"]
    rejected = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    assert rejected["code"] == "max_tool_calls" and not service.state["estimates"]
    result = service.output({"status": "completed", "reason": "completed"})
    assert result["status"] == "incomplete" and result["terminal"]["tool_calls"] == 1
    assert result["trace"][-1]["next_action"] == "interrupted"
    service.store.close()


def test_finished_model_at_tool_limit_reports_incomplete_without_claiming_interruption(tmp_path):
    service = make_service(tmp_path, maximum=1)
    service.call("search_materials", {"reason": "查找"})
    result = service.output({"status": "completed", "reason": "completed"})
    assert result["status"] == "incomplete" and result["reason"] == "max_tool_calls"
    assert result["trace"][-1]["next_action"] == "finish"
    service.store.close()


def test_storage_failure_does_not_create_a_plan_on_output(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    script = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    original = service.store.save_state

    def fail_once(request_id, state):
        monkeypatch.setattr(service.store, "save_state", original)
        raise sqlite3.OperationalError("simulated storage error")

    monkeypatch.setattr(service.store, "save_state", fail_once)
    saved = service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": script["script_id"], "reason": "保存"})
    assert not saved["ok"] and saved["code"] == "storage_failure"
    result = service.output({"status": "completed", "reason": "completed"})
    assert result["status"] == "incomplete" and result["reason"] == "storage_failure"
    assert result["plan_save_status"] == "not_saved" and service.store.get(service.request.request_id)["state"]["plan"] is None
    service.store.close()


def test_storage_error_after_commit_checks_saved_state_before_retry(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    script = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    original = service.store.save_state

    def commit_then_error(request_id, state):
        original(request_id, state)
        raise sqlite3.OperationalError("simulated uncertain outcome")

    monkeypatch.setattr(service.store, "save_state", commit_then_error)
    saved = service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": script["script_id"], "reason": "保存"})
    assert saved["ok"] and saved["plan"]["plan_id"] == service.store.get(service.request.request_id)["state"]["plan"]["plan_id"]
    service.store.close()


def test_unreadable_storage_after_write_does_not_overwrite_unknown_outcome(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    script = service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    original = service.store.save_state

    def commit_then_error(request_id, state):
        original(request_id, state)
        raise sqlite3.OperationalError("write outcome uncertain")

    def read_error(request_id):
        raise sqlite3.OperationalError("state unavailable")

    monkeypatch.setattr(service.store, "save_state", commit_then_error)
    monkeypatch.setattr(service.store, "get", read_error)
    saved = service.call("save_plan", {"estimate_id": estimate["estimate_id"], "script_id": script["script_id"], "reason": "保存"})
    assert not saved["ok"] and saved["code"] == "storage_state_unknown"
    result = service.output({"status": "completed", "reason": "completed"})
    assert result["status"] == "incomplete" and result["plan_save_status"] == "unknown"
    state = json.loads(service.store.connection.execute("SELECT state FROM requests WHERE request_id = ?", (service.request.request_id,)).fetchone()[0])
    assert state["plan"] is not None
    service.store.close()


def test_resume_reuses_script_that_succeeded_after_an_earlier_failure(tmp_path):
    service = make_service(tmp_path)
    service.fail_once = "generate_script"
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    assert not service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})["ok"]
    assert service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "重试"})["ok"]
    service.output({"status": "incomplete", "reason": "max_turns"})
    record = service.store.get(service.request.request_id)
    resumed = PlanningTools(service.request, record, service.materials, service.catalog, service.store, "mock", 20)
    result = run_mock(resumed)
    assert result["status"] == "completed" and result["selected_estimate"]["variant"] == "enhanced"
    assert [step["tool"] for step in result["trace"]] == ["save_plan"]
    service.store.close()


def test_resume_invalidates_estimates_when_cost_table_changes(tmp_path):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    estimate = service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    service.call("generate_script", {"estimate_id": estimate["estimate_id"], "reason": "生成"})
    service.output({"status": "incomplete", "reason": "max_turns"})
    changed = service.catalog.model_copy(deep=True)
    changed.variants["enhanced"].cost = Decimal("7")
    resumed = PlanningTools(service.request, service.store.get(service.request.request_id), service.materials, changed, service.store, "mock", 20)
    assert resumed.invalidated_dependencies == ["costs"]
    assert resumed.state["materials"] and not resumed.state["estimates"] and not resumed.state["scripts"]
    result = run_mock(resumed)
    assert result["status"] == "completed" and result["selected_estimate"]["total_cost"] == "13"
    assert sum(step["tool"] == "search_materials" for step in result["steps"]) == 1
    assert sum(step["tool"] == "estimate_cost" for step in result["steps"]) == 2
    service.store.close()


def test_new_steps_invalidate_the_previous_completed_run_output(tmp_path):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    service.output({"status": "incomplete", "reason": "max_turns"})
    record = service.store.get(service.request.request_id)
    assert record["last_output"] is not None
    resumed = PlanningTools(service.request, record, service.materials, service.catalog, service.store, "mock", 20)
    resumed.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    record = service.store.get(service.request.request_id)
    assert record["last_output"] is None
    assert record["state"]["events"][-1]["tool"] == "estimate_cost"
    service.store.close()


def test_resume_invalidates_retrieval_when_material_data_changes(tmp_path):
    service = make_service(tmp_path)
    service.call("search_materials", {"reason": "查找"})
    service.output({"status": "incomplete", "reason": "max_turns"})
    changed = [material.model_copy(deep=True) for material in service.materials]
    changed[0].description = "更新后的素材说明"
    resumed = PlanningTools(service.request, service.store.get(service.request.request_id), changed, service.catalog, service.store, "mock", 20)
    assert resumed.invalidated_dependencies == ["materials"] and resumed.state["materials"] is None
    result = run_mock(resumed)
    assert result["status"] == "completed"
    assert sum(step["tool"] == "search_materials" for step in result["steps"]) == 2
    service.store.close()


def test_sdk_limit_returns_incomplete_without_a_fabricated_plan(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    class FakeClient:
        def __init__(self, options):
            assert options.max_turns == 1
            assert options.tools == [] and options.strict_mcp_config and options.setting_sources == []
            assert len(options.allowed_tools) == 4

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="test")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(service, max_turns=1))
    assert result["status"] == "incomplete" and result["terminal"]["reason"] == "error_max_turns"
    assert result["plan_save_status"] == "not_saved"
    service.store.close()


def test_model_answer_without_tool_calls_is_not_treated_as_saved(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="test", result="已保存")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(service))
    assert result["status"] == "incomplete" and result["reason"] == "no_model_tool_calls"
    assert result["plan_save_status"] == "not_saved" and result["plan_id"] is None
    service.store.close()


def test_sdk_events_link_tool_requests_to_returned_results(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield AssistantMessage(content=[ToolUseBlock(id="one", name="mcp__planning__estimate_cost", input={"variant": "enhanced"})], model="test-model", message_id="first")
            yield UserMessage(content=[ToolResultBlock(tool_use_id="one", content="over budget", is_error=False)])
            yield AssistantMessage(content=[ToolUseBlock(id="two", name="mcp__planning__estimate_cost", input={"variant": "lean"})], model="test-model", message_id="second")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="test")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(service))
    events = result["terminal"]["sdk_events"]
    assert [(event["type"], event["tool_use_id"]) for event in events] == [("tool_request", "one"), ("tool_result", "one"), ("tool_request", "two")]
    assert events[0]["message_id"] != events[2]["message_id"]
    service.store.close()


def test_real_resume_context_resets_current_limit_and_preserves_history(tmp_path, monkeypatch):
    service = make_service(tmp_path, maximum=1)
    service.call("search_materials", {"reason": "查找"})
    service.call("estimate_cost", {"material_id": "M01", "variant": "enhanced", "reason": "估算"})
    service.output({"status": "incomplete", "reason": "max_tool_calls"})
    record = service.store.get(service.request.request_id)
    resumed = PlanningTools(service.request, record, service.materials, service.catalog, service.store, "glm", 20, is_resume=True)

    class FakeClient:
        def __init__(self, options):
            assert "限制已经重置" in options.system_prompt

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            value = json.loads(prompt)
            assert value["current_run"] == {"is_resume": True, "tool_calls_used": 0, "max_tool_calls": 20, "invalidated_dependencies": []}
            assert value["saved_materials"] and value["previous_tool_results"][-1]["result"]["code"] == "max_tool_calls"

        async def receive_response(self):
            yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="test")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(resumed))
    assert result["status"] == "incomplete"
    service.store.close()


def test_resume_without_prior_tool_calls_is_identified_as_resume(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.output({"status": "incomplete", "reason": "missing_credential"})
    record = service.store.get(service.request.request_id)
    resumed = PlanningTools(service.request, record, service.materials, service.catalog, service.store, "glm", 20, is_resume=True)

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            value = json.loads(prompt)
            assert value["current_run"]["is_resume"] is True and value["previous_tool_results"] == []

        async def receive_response(self):
            yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="test")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    assert asyncio.run(run_glm(resumed))["status"] == "incomplete"
    service.store.close()
