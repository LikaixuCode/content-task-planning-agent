import copy
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from .models import Catalog, Material, PlanningRequest
from .storage import PlanningStore


def amount(value: Decimal) -> str:
    return format(value.normalize(), "f")


def fingerprint(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PlanningTools:
    def __init__(self, request: PlanningRequest, record: dict, materials: list[Material], catalog: Catalog,
                 store: PlanningStore, mode: str, max_tool_calls: int, fail_once: str | None = None, is_resume: bool = False):
        self.request = request
        self.start_at = datetime.fromisoformat(record["start_at"])
        self.state = record["state"]
        self.materials = materials
        self.catalog = catalog
        self.store = store
        versions = {
            "materials": fingerprint([material.model_dump(mode="json") for material in materials]),
            "costs": fingerprint(catalog.model_dump(mode="json")),
        }
        previous_versions = self.state.get("data_versions")
        self.invalidated_dependencies = []
        if not self.state["plan"] and (previous_versions or self.state["events"]):
            if not previous_versions or previous_versions.get("materials") != versions["materials"]:
                self.invalidated_dependencies.append("materials")
                self.state["materials"] = None
                self.state["estimates"] = {}
                self.state["scripts"] = {}
            elif previous_versions.get("costs") != versions["costs"]:
                self.invalidated_dependencies.append("costs")
                self.state["estimates"] = {}
                self.state["scripts"] = {}
        self.state["data_versions"] = versions
        if previous_versions != versions:
            self.store.save_state(request.request_id, self.state)
        self.mode = mode
        self.is_resume = is_resume
        self.max_tool_calls = max_tool_calls
        self.fail_once = fail_once
        self.tool_calls = 0
        self.limit_hit = False
        self.storage_failed = False
        self.storage_unverified = False
        self.run_id = str(uuid.uuid4())
        self.run_start = len(self.state["events"])

    def call(self, name: str, arguments: dict) -> dict:
        before = copy.deepcopy(self.state)
        if self.storage_failed:
            result = {"ok": False, "status": "rejected", "code": "storage_failure", "message": "存储结果不确定，需确认状态后续跑"}
        elif self.tool_calls >= self.max_tool_calls:
            self.limit_hit = True
            result = {"ok": False, "status": "rejected", "code": "max_tool_calls", "message": "业务工具调用达到上限"}
        else:
            self.tool_calls += 1
            if name == "search_materials":
                result = self.search_materials()
            elif name == "estimate_cost":
                result = self.estimate_cost(arguments.get("material_id", ""), arguments.get("variant", ""))
            elif name == "generate_script":
                result = self.generate_script(arguments.get("estimate_id", ""))
            elif name == "save_plan":
                result = self.save_plan(arguments.get("estimate_id", ""), arguments.get("script_id", ""))
            else:
                result = {"ok": False, "status": "failed", "code": "unknown_tool", "message": "未知工具"}
        if len(self.state["events"]) > self.run_start:
            self.state["events"][-1]["next_action"] = name
        self.state["events"].append({
            "run_id": self.run_id,
            "mode": self.mode,
            "step": len(self.state["events"]) - self.run_start + 1,
            "tool": name,
            "arguments": {key: value for key, value in arguments.items() if key != "reason"},
            "reason": arguments.get("reason", ""),
            "result": result,
            "next_action": "await_model",
        })
        if self.storage_failed:
            return result
        try:
            self.store.save_state(self.request.request_id, self.state)
        except sqlite3.Error:
            self.state = before
            self.storage_failed = True
            self.storage_unverified = True
            try:
                observed = self.store.get(self.request.request_id)
            except sqlite3.Error:
                observed = None
            else:
                self.storage_unverified = False
            self.state = observed["state"] if observed else before
            persisted = self.state["events"][-1] if len(self.state["events"]) > self.run_start else None
            if persisted and persisted["run_id"] == self.run_id and persisted["step"] == len(before["events"]) - self.run_start + 1:
                self.storage_failed = False
                return persisted["result"]
            if len(self.state["events"]) > self.run_start:
                self.state["events"][-1]["next_action"] = name
            code = "storage_state_unknown" if self.storage_unverified else "storage_failure"
            message = "存储状态无法读取，请先确认后续跑" if self.storage_unverified else "工具结果未保存，已读取现有状态；请续跑"
            result = {"ok": False, "status": "failed", "code": code, "message": message}
            self.state["events"].append({
                "run_id": self.run_id,
                "mode": self.mode,
                "step": len(self.state["events"]) - self.run_start + 1,
                "tool": name,
                "arguments": {key: value for key, value in arguments.items() if key != "reason"},
                "reason": arguments.get("reason", ""),
                "result": result,
                "next_action": "await_model",
            })
        return result

    def search_materials(self) -> dict:
        if self.state["materials"] is not None:
            return {"ok": True, "status": "succeeded", "cached": True, "materials": self.state["materials"], "variants": list(self.catalog.variants)}
        matches = [material for material in self.materials if any(word in self.request.topic for word in material.keywords)]
        if not matches:
            matches = [material for material in self.materials if not material.keywords]
        self.state["materials"] = [material.model_dump(mode="json") for material in matches]
        return {"ok": True, "status": "succeeded", "cached": False, "materials": self.state["materials"], "variants": list(self.catalog.variants)}

    def calculate(self, material_id: str, variant: str) -> dict:
        items = [self.catalog.material, self.catalog.variants[variant], self.catalog.package]
        total = sum((item.cost for item in items), Decimal("0"))
        duration = sum(item.duration_minutes for item in items)
        finish_at = self.start_at + timedelta(minutes=duration)
        budget_gap = max(total - self.request.budget, Decimal("0"))
        deadline_gap = max(0, math.ceil((finish_at - self.request.deadline).total_seconds() / 60))
        return {
            "material_id": material_id,
            "variant": variant,
            "line_items": [{"name": item.name, "cost": amount(item.cost), "duration_minutes": item.duration_minutes} for item in items],
            "total_cost": amount(total),
            "budget": amount(self.request.budget),
            "budget_gap": amount(budget_gap),
            "duration_minutes": duration,
            "start_at": self.start_at.isoformat(),
            "finish_at": finish_at.isoformat(),
            "deadline": self.request.deadline.isoformat(),
            "deadline_gap_minutes": deadline_gap,
            "feasible": budget_gap == 0 and deadline_gap == 0,
        }

    def estimate_cost(self, material_id: str, variant: str) -> dict:
        if not self.state["materials"] or material_id not in {value["id"] for value in self.state["materials"]}:
            return {"ok": False, "status": "failed", "code": "material_not_retrieved", "message": "请先检索并选择返回的素材"}
        if variant not in self.catalog.variants:
            return {"ok": False, "status": "failed", "code": "unknown_variant", "message": "方案类型不存在"}
        existing = next((value for value in self.state["estimates"].values() if value["material_id"] == material_id and value["variant"] == variant), None)
        if existing:
            return {"ok": True, "status": "succeeded", "cached": True, **existing}
        estimate = {"estimate_id": f"est-{uuid.uuid4().hex[:12]}", **self.calculate(material_id, variant)}
        self.state["estimates"][estimate["estimate_id"]] = estimate
        return {"ok": True, "status": "succeeded", "cached": False, **estimate}

    def generate_script(self, estimate_id: str) -> dict:
        estimate = self.state["estimates"].get(estimate_id)
        if not estimate:
            return {"ok": False, "status": "failed", "code": "estimate_not_found", "message": "请先估算方案"}
        if not self.calculate(estimate["material_id"], estimate["variant"])["feasible"]:
            return {"ok": False, "status": "failed", "code": "plan_infeasible", "message": "预算或截止时间不足，不能生成该方案"}
        existing = next((value for value in self.state["scripts"].values() if value["estimate_id"] == estimate_id), None)
        if existing:
            return {"ok": True, "status": "succeeded", "cached": True, **existing}
        if self.fail_once == "generate_script" and "generate_script" not in self.state["faults"]:
            self.state["faults"].append("generate_script")
            return {"ok": False, "status": "failed", "code": "simulated_tool_failure", "message": "脚本工具模拟失败；可选择其他方案或停止", "estimate_id": estimate_id}
        material = next(value for value in self.state["materials"] if value["id"] == estimate["material_id"])
        if estimate["variant"] == "enhanced":
            text = f"开场：介绍{self.request.topic}。主体：结合{material['title']}展示三个活动亮点。结尾：邀请用户参与并分享体验。"
        else:
            text = f"围绕{self.request.topic}，使用{material['title']}介绍一个活动亮点，并邀请用户参与。"
        script = {"script_id": f"script-{uuid.uuid4().hex[:12]}", "estimate_id": estimate_id, "material_id": estimate["material_id"], "variant": estimate["variant"], "text": text}
        self.state["scripts"][script["script_id"]] = script
        return {"ok": True, "status": "succeeded", "cached": False, **script}

    def save_plan(self, estimate_id: str, script_id: str) -> dict:
        if self.state["plan"]:
            return {"ok": True, "status": "succeeded", "duplicate": True, "plan": self.state["plan"]}
        estimate = self.state["estimates"].get(estimate_id)
        script = self.state["scripts"].get(script_id)
        if not estimate or not script or script["estimate_id"] != estimate_id:
            return {"ok": False, "status": "failed", "code": "dependency_missing", "message": "保存前需要同一方案的估算和成功脚本"}
        current = self.calculate(estimate["material_id"], estimate["variant"])
        if not current["feasible"]:
            return {"ok": False, "status": "failed", "code": "plan_infeasible", "message": "预算或截止时间不足，不能保存"}
        if self.fail_once == "save_plan" and "save_plan" not in self.state["faults"]:
            self.state["faults"].append("save_plan")
            return {"ok": False, "status": "failed", "code": "simulated_tool_failure", "message": "方案保存模拟失败"}
        plan = {
            "plan_id": f"plan-{uuid.uuid4().hex[:12]}",
            "request_id": self.request.request_id,
            "topic": self.request.topic,
            "estimate_id": estimate_id,
            "script_id": script_id,
            "script": script["text"],
            "estimate": {"estimate_id": estimate_id, **current},
            "tasks": [{"name": item["name"], "status": "planned", "duration_minutes": item["duration_minutes"]} for item in current["line_items"]],
        }
        self.state["plan"] = plan
        return {"ok": True, "status": "succeeded", "duplicate": False, "plan": plan}

    def output(self, terminal: dict) -> dict:
        if self.storage_failed:
            terminal = {**terminal, "status": "incomplete", "reason": "storage_state_unknown" if self.storage_unverified else "storage_failure"}
        elif self.limit_hit:
            terminal = {**terminal, "status": "incomplete", "reason": "max_tool_calls"}
        events = self.state["events"]
        trace = events[self.run_start:]
        if trace:
            trace[-1]["next_action"] = "finish" if terminal.get("status") == "completed" else "interrupted"
            if terminal.get("status") != "completed":
                trace[-1]["termination_reason"] = terminal.get("reason", "unknown")
        plan = self.state["plan"]
        status = "completed" if plan and terminal.get("status") == "completed" else "incomplete"
        reason = None
        gaps = []
        if status == "incomplete":
            if terminal.get("status") != "completed":
                reason = terminal.get("reason", "interrupted")
            elif self.state["materials"]:
                material_id = self.state["materials"][0]["id"]
                choices = [self.calculate(material_id, variant) for variant in self.catalog.variants]
                if not any(choice["feasible"] for choice in choices):
                    reason = "no_feasible_plan"
                    gaps = [{"variant": choice["variant"], "budget_gap": choice["budget_gap"], "deadline_gap_minutes": choice["deadline_gap_minutes"]} for choice in choices]
                elif self.tool_calls >= self.max_tool_calls:
                    reason = "max_tool_calls"
                else:
                    failures = [event["result"].get("code") for event in trace if event["result"].get("status") == "failed"]
                    reason = failures[-1] if failures else "plan_not_saved"
            else:
                reason = "materials_not_retrieved"
        estimates = list(self.state["estimates"].values())
        selected = plan["estimate"] if plan else estimates[-1] if estimates else None
        result = {
            "request_id": self.request.request_id,
            "mode": self.mode,
            "status": status,
            "reason": reason,
            "missing_fields": [],
            "invalidated_dependencies": self.invalidated_dependencies,
            "budget": amount(self.request.budget),
            "deadline": self.request.deadline.isoformat(),
            "start_at": self.start_at.isoformat(),
            "selected_estimate": selected,
            "candidate_gaps": gaps,
            "plan_save_status": "unknown" if self.storage_unverified else "saved" if plan else "not_saved",
            "plan_id": plan["plan_id"] if plan and not self.storage_unverified else None,
            "plan": plan if not self.storage_unverified else None,
            "steps": [{"run_id": event["run_id"], "tool": event["tool"], "status": event["result"]["status"], "result": event["result"]} for event in events],
            "terminal": {**terminal, "tool_calls": self.tool_calls, "max_tool_calls": self.max_tool_calls},
            "trace": trace,
        }
        if not self.storage_unverified:
            self.store.save_output(self.request.request_id, self.state, result)
        return result
