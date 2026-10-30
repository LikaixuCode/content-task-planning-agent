import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def cli(db: Path, example: str, *extra: str, env=None):
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(ROOT / "examples" / f"{example}.input.json"), "--db", str(db), *extra],
        cwd=ROOT, capture_output=True, text=True, env=env, check=False,
    )
    return process.returncode, json.loads(process.stdout)


def test_normal_plan_and_duplicate_share_one_saved_result(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, first = cli(db, "normal")
    assert code == 0 and first["status"] == "completed"
    assert first["plan_save_status"] == "saved"
    assert first["selected_estimate"]["total_cost"] == "12"
    assert first["selected_estimate"]["duration_minutes"] == 120
    assert all(task["status"] == "planned" for task in first["plan"]["tasks"])
    code, again = cli(db, "normal")
    assert code == 0 and again["duplicate"]
    assert again["plan_id"] == first["plan_id"] and len(again["steps"]) == len(first["steps"])
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_over_budget_replans_and_does_not_sum_abandoned_estimates(tmp_path):
    code, result = cli(tmp_path / "state.sqlite3", "replan")
    assert code == 0 and result["status"] == "completed"
    estimates = [step["result"] for step in result["trace"] if step["tool"] == "estimate_cost"]
    assert [(e["total_cost"], e["feasible"]) for e in estimates] == [("12", False), ("8", True)]
    assert result["selected_estimate"]["variant"] == "lean"
    assert result["plan"]["estimate"]["total_cost"] == "8"


def test_fractional_budget_is_calculated_as_decimal(tmp_path):
    payload = json.loads((ROOT / "examples/replan.input.json").read_text())
    payload["budget"] = 8.01
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload))
    p = subprocess.run([sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(source), "--db", str(tmp_path / "state.sqlite3")], cwd=ROOT, capture_output=True, text=True)
    result = json.loads(p.stdout)
    estimates = [step["result"] for step in result["trace"] if step["tool"] == "estimate_cost"]
    assert p.returncode == 0 and result["budget"] == "8.01"
    assert estimates[0]["budget_gap"] == "3.99" and result["selected_estimate"]["total_cost"] == "8"


@pytest.mark.parametrize("missing_fields", [["budget"], ["deadline"], ["budget", "deadline"]])
def test_missing_input_does_not_create_database_or_call_tools(tmp_path, missing_fields):
    payload = json.loads((ROOT / "examples/normal.input.json").read_text())
    for field in missing_fields:
        payload.pop(field)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    database = tmp_path / "state.sqlite3"
    process = subprocess.run([sys.executable, "-m", "src.main", "run", "--mode", "glm", "--input", str(input_path), "--db", str(database)], cwd=ROOT, capture_output=True, text=True)
    result = json.loads(process.stdout)
    assert process.returncode == 1 and result["reason"] == "missing_input"
    assert result["missing_fields"] == missing_fields and result["trace"] == []
    assert not database.exists()


@pytest.mark.parametrize(
    ("example", "field", "expected"),
    [("no-feasible", "budget_gap", {"enhanced": "6", "lean": "2"}),
     ("deadline", "deadline_gap_minutes", {"enhanced": 60, "lean": 30})],
)
def test_no_feasible_plan_reports_variant_gaps_and_does_not_save(tmp_path, example, field, expected):
    code, result = cli(tmp_path / "state.sqlite3", example)
    assert code == 1 and result["reason"] == "no_feasible_plan"
    assert result["plan_save_status"] == "not_saved" and result["plan_id"] is None
    assert {entry["variant"]: entry[field] for entry in result["candidate_gaps"]} == expected


def test_script_failure_replans_and_keeps_successful_results(tmp_path):
    code, result = cli(tmp_path / "state.sqlite3", "failure", "--fail-once", "generate_script")
    assert code == 0 and result["status"] == "completed"
    failed = next(step for step in result["trace"] if step["tool"] == "generate_script" and not step["result"]["ok"])
    assert failed["result"]["status"] == "failed" and failed["next_action"] == "estimate_cost"
    assert result["selected_estimate"]["variant"] == "lean"
    assert sum(step["tool"] == "search_materials" and step["status"] == "succeeded" for step in result["steps"]) == 1
    assert any(step["tool"] == "generate_script" and step["status"] == "failed" for step in result["steps"])


def test_interrupted_run_requires_resume_and_reuses_successes(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, first = cli(db, "failure", "--fail-once", "generate_script", "--max-tool-calls", "3")
    assert code == 1 and first["reason"] == "max_tool_calls"
    assert first["terminal"]["tool_calls"] == 3 and first["trace"][-1]["next_action"] == "interrupted"
    assert first["trace"][-1]["result"]["status"] == "rejected"
    code, repeated = cli(db, "failure")
    assert code == 1 and repeated["duplicate"] and repeated["resumable"]
    assert repeated["steps"] == first["steps"]
    code, resumed = cli(db, "failure", "--resume", "--fail-once", "generate_script")
    assert code == 0 and resumed["plan_save_status"] == "saved"
    assert resumed["selected_estimate"]["variant"] == "lean"
    assert [step["tool"] for step in resumed["trace"]] == ["estimate_cost", "generate_script", "save_plan"]
    assert sum(step["tool"] == "search_materials" for step in resumed["steps"]) == 1
    assert any(step["status"] == "failed" for step in resumed["steps"])


def test_interrupted_output_after_save_preserves_and_reuses_existing_plan(tmp_path):
    db = tmp_path / "state.sqlite3"
    _, first = cli(db, "normal")
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE requests SET last_output = NULL WHERE request_id = ?", ("case-normal",))
    code, interrupted = cli(db, "normal")
    assert code == 1 and interrupted["reason"] == "previous_run_interrupted"
    assert interrupted["plan_save_status"] == "saved" and interrupted["plan"]["plan_id"] == first["plan_id"]
    code, resumed = cli(db, "normal", "--resume")
    assert code == 0 and resumed["plan_id"] == first["plan_id"] and resumed["trace"] == []


def test_save_failure_is_not_success_and_resume_reuses_script(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, failed = cli(db, "normal", "--fail-once", "save_plan")
    assert code == 1 and failed["plan_save_status"] == "not_saved" and failed["plan_id"] is None
    assert failed["trace"][-1]["tool"] == "save_plan" and not failed["trace"][-1]["result"]["ok"]
    code, resumed = cli(db, "normal", "--resume", "--fail-once", "save_plan")
    assert code == 0 and resumed["plan_save_status"] == "saved"
    assert [step["tool"] for step in resumed["trace"]] == ["save_plan"]
    assert sum(step["tool"] == "generate_script" for step in resumed["steps"]) == 1


def test_same_request_id_with_different_content_is_rejected(tmp_path):
    db = tmp_path / "state.sqlite3"
    _, first = cli(db, "normal")
    payload = json.loads((ROOT / "examples/normal.input.json").read_text())
    payload["budget"] = 16
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(payload))
    p = subprocess.run([sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(changed), "--db", str(db)], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 2 and json.loads(p.stdout)["error_type"] == "RequestConflict"
    _, again = cli(db, "normal")
    assert again["plan_id"] == first["plan_id"]


def test_default_start_time_is_reused_for_duplicate_request(tmp_path):
    payload = json.loads((ROOT / "examples/normal.input.json").read_text())
    payload.pop("start_at")
    payload["deadline"] = "2099-01-01T13:00:00+08:00"
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload))
    args = [sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(source), "--db", str(tmp_path / "state.sqlite3")]
    first = json.loads(subprocess.run(args, cwd=ROOT, capture_output=True, text=True).stdout)
    second = json.loads(subprocess.run(args, cwd=ROOT, capture_output=True, text=True).stdout)
    assert first["start_at"] == second["start_at"] and first["plan_id"] == second["plan_id"]


def test_missing_real_model_credential_does_not_fallback_to_mock(tmp_path):
    env = os.environ.copy()
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    p = subprocess.run([sys.executable, "-m", "src.main", "run", "--mode", "glm", "--input", str(ROOT / "examples/normal.input.json"), "--db", str(tmp_path / "state.sqlite3")], cwd=ROOT, capture_output=True, text=True, env=env)
    result = json.loads(p.stdout)
    assert p.returncode == 1 and result["mode"] == "glm" and result["reason"] == "missing_credential"
    assert result["trace"] == [] and result["plan_save_status"] == "not_saved"
