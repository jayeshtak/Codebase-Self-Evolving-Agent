import pytest
import tempfile
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.planner import PlannerAgent, PlanItem
from agents.scanner import Scanner, Issue
from memory.sqlite import SQLiteMemory, IssueRecord
from scheduler.jobs import ScanScheduler, run_scan_job


def test_planner_multi_factor_prioritization_and_clustering():
    planner = PlannerAgent()

    issues = [
        {
            "file": "core/auth.py",
            "line": 15,
            "issue_type": "security",
            "severity": "high",
            "description": "Hardcoded secret API key",
            "language": "python",
            "extra": {"complexity": 16},
        },
        {
            "file": "core/auth.py",
            "line": 18,
            "issue_type": "dead_code",
            "severity": "low",
            "description": "Unused import: os",
            "language": "python",
            "extra": {},
        },
        {
            "file": "utils/helpers.py",
            "line": 50,
            "issue_type": "complexity",
            "severity": "medium",
            "description": "High cyclomatic complexity in helper",
            "language": "python",
            "extra": {"complexity": 11},
        },
    ]

    impact_radii = {
        "core/auth.py": {"impact_score": 4, "risk_level": "high"},
        "utils/helpers.py": {"impact_score": 1, "risk_level": "low"},
    }

    plan = planner.plan(
        scan_id="test-scan-123",
        issues=issues,
        impact_radii=impact_radii,
        rejections=[],
    )

    assert plan.total_issues == 3
    actionable = plan.actionable()
    assert len(actionable) == 3

    # Check that highest score is ranked first
    assert actionable[0].priority_score >= actionable[1].priority_score
    top = actionable[0]
    assert top.priority_explanation != ""
    assert "Severity" in top.priority_explanation
    assert top.regression_likelihood in ("high", "medium", "low")

    # Check clustering: core/auth.py lines 15 and 18 are within 10 lines
    auth_items = [i for i in actionable if i.file == "core/auth.py"]
    assert len(auth_items) == 2
    assert auth_items[0].group_id != ""
    assert auth_items[0].group_id == auth_items[1].group_id
    assert len(auth_items[0].conflicts_with) > 0
    assert "core/auth.py:18" in auth_items[0].conflicts_with[0]


def test_sqlite_developer_learning_and_debt_insights(tmp_path):
    db_file = tmp_path / "test_memory.db"
    mem = SQLiteMemory(str(db_file))

    scan_id = "scan-alpha"
    mem.start_scan(scan_id, str(tmp_path))

    # Insert open issues
    mem.save_issue(IssueRecord(
        file="core/auth.py",
        line=10,
        issue_type="security",
        severity="high",
        description="Insecure cipher",
        language="python",
        extra={},
        scan_id=scan_id,
    ))
    mem.save_issue(IssueRecord(
        file="core/auth.py",
        line=40,
        issue_type="complexity",
        severity="medium",
        description="Complex validation loop",
        language="python",
        extra={},
        scan_id=scan_id,
    ))
    mem.finish_scan(scan_id, total_issues=2)

    # Record developer decisions
    mem.record_developer_decision(
        file="core/auth.py",
        issue_type="security",
        description="Insecure cipher",
        decision="accepted",
        fix_id=1
    )
    mem.record_developer_decision(
        file="core/auth.py",
        issue_type="complexity",
        description="Complex validation loop",
        decision="rejected",
        reason="Refactoring is unnecessary right now",
        fix_id=2
    )

    # 1. Technical debt summary
    debt = mem.get_technical_debt_summary()
    assert debt["total_open_debt"] >= 1
    assert debt["estimated_remediation_hours"] > 0
    assert len(debt["hotspot_files"]) >= 1

    # 2. Learning profile
    learning = mem.get_developer_learning_profile()
    assert learning["total_decisions"] == 2
    assert learning["accepted_count"] == 1
    assert learning["acceptance_rate"] == 0.5
    assert len(learning["top_rejection_reasons"]) >= 1
    assert "Refactoring is unnecessary" in learning["top_rejection_reasons"][0]["reason"]

    # 3. Recurring issues
    recurring = mem.get_recurring_issues(limit=10)
    assert len(recurring) >= 1
    assert recurring[0]["file"] == "core/auth.py"

    mem.close()


def test_scan_scheduler_pause_resume_and_mutex():
    scheduler = ScanScheduler(repo_path=".")
    status = scheduler.status()
    assert status["running"] is False
    assert status["paused"] is False

    scheduler.pause()
    assert scheduler.status()["paused"] is True

    scheduler.resume()
    assert scheduler.status()["paused"] is False
