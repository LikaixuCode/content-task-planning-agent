import argparse
import asyncio
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from .agent import run_glm, run_mock
from .business import PlanningTools
from .models import Catalog, Material, PlanningRequest
from .storage import PlanningStore, RequestConflict


ROOT = Path(__file__).resolve().parent.parent


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="内容任务规划 Agent")
    subcommands = command.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="生成或续跑活动方案")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--mode", choices=["glm", "mock"], default="glm")
    run.add_argument("--db", type=Path, default=ROOT / ".runtime/plans.sqlite3")
    run.add_argument("--resume", action="store_true", help="显式续跑未完成请求")
    run.add_argument("--max-turns", type=int, default=12, help="SDK Agent 轮次上限")
    run.add_argument("--max-tool-calls", type=int, default=20, help="业务工具实际执行次数上限")
    run.add_argument("--fail-once", choices=["generate_script", "save_plan"], help="模拟指定工具的一次失败")
    return command


def main() -> int:
    args = parser().parse_args()
    try:
        if args.max_turns < 1 or args.max_tool_calls < 1:
            raise ValueError("执行上限必须大于零")
        raw = json.loads(args.input.read_text(), parse_float=Decimal)
        if not isinstance(raw, dict):
            raise ValueError("输入必须是 JSON 对象")
        missing = [field for field in ("request_id", "topic", "budget", "deadline") if raw.get(field) is None or raw.get(field) == ""]
        if missing:
            result = {"request_id": raw.get("request_id"), "mode": args.mode, "status": "incomplete", "reason": "missing_input", "missing_fields": missing, "plan_save_status": "not_saved", "plan_id": None, "steps": [], "terminal": {"status": "not_started", "reason": "missing_input", "tool_calls": 0}, "trace": []}
            exit_code = 1
        else:
            request = PlanningRequest.model_validate(raw)
            materials = [Material.model_validate(value) for value in json.loads((ROOT / "data/materials.json").read_text())]
            catalog = Catalog.model_validate_json((ROOT / "data/costs.json").read_text())
            store = PlanningStore(args.db)
            try:
                if args.resume and not store.get(request.request_id):
                    result = {"request_id": request.request_id, "status": "incomplete", "reason": "request_not_found", "plan_save_status": "not_saved", "plan_id": None, "steps": [], "trace": []}
                    exit_code = 1
                else:
                    record, created = store.register(request)
                    if not created and (not args.resume or (record["last_output"] and record["last_output"]["status"] == "completed")):
                        plan = record["state"]["plan"]
                        result = record["last_output"] or {
                            "request_id": request.request_id, "mode": args.mode, "status": "incomplete",
                            "reason": "previous_run_interrupted", "plan_save_status": "saved" if plan else "not_saved",
                            "plan_id": plan["plan_id"] if plan else None, "plan": plan,
                            "steps": record["state"]["events"], "trace": [],
                            "terminal": {"status": "incomplete", "reason": "previous_run_interrupted"},
                        }
                        result = {**result, "duplicate": True, "resumable": result["status"] != "completed"}
                    else:
                        service = PlanningTools(request, record, materials, catalog, store, args.mode, args.max_tool_calls, args.fail_once, is_resume=not created and args.resume)
                        result = run_mock(service) if args.mode == "mock" else asyncio.run(run_glm(service, args.max_turns))
                        result["duplicate"] = False
                        result["resumable"] = result["status"] != "completed"
                    exit_code = 0 if result["status"] == "completed" else 1
            finally:
                store.close()
    except (OSError, ValueError, ValidationError, sqlite3.Error, RequestConflict) as exc:
        result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
