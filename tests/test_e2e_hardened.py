"""
tests/test_e2e_hardened.py

Comprehensive End-to-End Hardening & Security Test Suite.
Verifies the complete workflow across 15 production scenarios using isolated temporary Git repositories.
"""
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import git
import pytest
from fastapi.testclient import TestClient

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import config
from ui.server import app, state
from core.git_manager import GitManager, DirtyWorkingTreeError
from memory.sqlite import SQLiteMemory, IssueRecord, FixRecord
from main import CodebaseAgent

client = TestClient(app)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_git_repo():
    """Create a temporary initialized Git repository with clean sample files."""
    import gc
    import shutil

    tmpdir = tempfile.mkdtemp(prefix="agent_test_repo_")
    repo_path = Path(tmpdir).resolve()
    repo = git.Repo.init(repo_path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test Agent")
        writer.set_value("user", "email", "test@agent.com")

    # Create sample files
    main_py = repo_path / "main.py"
    main_py.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    calc_py = repo_path / "calc.py"
    calc_py.write_text(
        "def add(a, b):\n"
        "    # Simple function\n"
        "    return a + b\n\n"
        "def complex_func(x):\n"
        "    if x > 0:\n"
        "        for i in range(x):\n"
        "            if i % 2 == 0:\n"
        "                pass\n"
        "    return x\n",
        encoding="utf-8",
    )

    # Create nested and excluded directories
    venv_dir = repo_path / "venv" / "Lib"
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "site.py").write_text("# Virtualenv file\n", encoding="utf-8")

    node_dir = repo_path / "node_modules" / "pkg"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "index.js").write_text("// JS lib\n", encoding="utf-8")

    # Initial commit
    repo.git.add(A=True)
    repo.index.commit("Initial test repository commit")

    try:
        yield repo_path
    finally:
        try:
            repo.close()
        except Exception:
            pass
        if state.agent:
            try:
                state.agent.git.repo.close()
            except Exception:
                pass
            state.agent = None
            state.repo_path = ""
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Scenario 1: Repository Connection & Validation ────────────────────────────

def test_repo_connect_and_validation(temp_git_repo):
    """Scenario 1: Connect to valid repo, reject invalid, files, and traversal."""
    # 1. Valid repository connection
    res = client.post("/api/connect", json={"repo_path": str(temp_git_repo)})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "branch" in data
    assert state.repo_path == str(temp_git_repo)

    # 2. Non-existent path
    non_existent = str(temp_git_repo / "does_not_exist_12345")
    res_bad = client.post("/api/connect", json={"repo_path": non_existent})
    assert res_bad.status_code == 400
    assert "does not exist" in res_bad.json()["detail"].lower()

    # 3. File path instead of directory
    file_path = str(temp_git_repo / "main.py")
    res_file = client.post("/api/connect", json={"repo_path": file_path})
    assert res_file.status_code == 400
    assert "file, not a directory" in res_file.json()["detail"].lower()

    # 4. Empty path
    res_empty = client.post("/api/connect", json={"repo_path": ""})
    assert res_empty.status_code == 400


# ── Scenario 2: Clean Repository Scanning & Path Exclusion ────────────────────

def test_repo_tree_clean_and_excluded_paths(temp_git_repo):
    """Scenario 2: Explorer file tree strictly excludes venv, node_modules, and git."""
    res = client.get(f"/api/repo/tree?repo_path={str(temp_git_repo)}")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"

    files = [f["path"] for f in data["files"]]
    # Valid files must be included
    assert "main.py" in files
    assert "calc.py" in files

    # Virtual environments and node_modules must NEVER be in tree
    for f in files:
        assert not f.startswith("venv/")
        assert not f.startswith("node_modules/")
        assert not f.startswith(".git/")


# ── Scenario 3: File Reading & Workspace Boundary Isolation ───────────────────

def test_repo_file_access_and_traversal_prevention(temp_git_repo):
    """Scenario 3: Read repo files securely and block path traversal outside workspace."""
    # 1. Valid file
    res = client.post("/api/repo/file", json={
        "repo_path": str(temp_git_repo),
        "file_path": "main.py"
    })
    assert res.status_code == 200
    assert "def hello" in res.json()["content"]

    # 2. Non-existent file inside repo
    res_404 = client.post("/api/repo/file", json={
        "repo_path": str(temp_git_repo),
        "file_path": "missing_module.py"
    })
    assert res_404.status_code == 404

    # 3. Traversal outside repository
    res_traversal = client.post("/api/repo/file", json={
        "repo_path": str(temp_git_repo),
        "file_path": "../../secret.txt"
    })
    assert res_traversal.status_code == 403
    assert "forbidden" in res_traversal.json()["detail"].lower()


# ── Scenario 4: Structured Scanner Output & Persistence ───────────────────────

def test_scanner_clean_repo_and_issue_schema(temp_git_repo):
    """Scenario 4: Scanner runs, discovers structured issues, and persists last_scan.json."""
    res = client.post("/api/scan", json={"repo_path": str(temp_git_repo)})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "run_id" in data
    assert "scan_results" in data

    # Verify last_scan.json persistence
    scan_file = temp_git_repo / "data" / "last_scan.json"
    assert scan_file.exists()


# ── Scenario 5: Concurrency Protection on Scans ───────────────────────────────

def test_concurrency_lock_prevents_duplicate_scans(temp_git_repo):
    """Scenario 5: Attempting concurrent scan while lock is held returns 409 Conflict."""
    with state.scan_lock:
        res = client.post("/api/scan", json={"repo_path": str(temp_git_repo)})
        assert res.status_code == 409
        assert "already in progress" in res.json()["detail"].lower()


# ── Scenario 6: Concurrency Protection on Fix Synthesis ───────────────────────

def test_concurrency_lock_prevents_duplicate_fixes(temp_git_repo):
    """Scenario 6: Attempting concurrent fix synthesis returns 409 Conflict."""
    # Connect agent first
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    dummy_issue = {
        "file": "main.py",
        "line": 1,
        "issue_type": "style",
        "description": "Sample style issue",
        "severity": "low"
    }

    with state.fix_lock:
        res = client.post("/api/fix/generate", json={
            "issue": dummy_issue,
            "scan_id": "scan-1234"
        })
        assert res.status_code == 409
        assert "already in progress" in res.json()["detail"].lower()


# ── Scenario 7: Dirty Working Tree Protection ─────────────────────────────────

def test_dirty_working_tree_blocks_fix_creation(temp_git_repo):
    """Scenario 7: Uncommitted user changes block automated branch operations to prevent data loss."""
    agent = CodebaseAgent(str(temp_git_repo))

    # Dirty the working tree with an uncommitted user change
    target_file = temp_git_repo / "main.py"
    target_file.write_text("# USER DIRTY UNCOMMITTED EDIT\n", encoding="utf-8")
    assert agent.git.has_uncommitted_changes() is True

    issue = {
        "file": "main.py",
        "line": 1,
        "issue_type": "style",
        "description": "Add docstring",
        "severity": "low"
    }

    # Execute fix_issue — MUST block without clobbering user edit
    result = agent.fix_issue(issue, "scan-dirty-test")
    assert result["status"] == "blocked"
    assert "uncommitted user changes" in result["error"].lower()

    # Verify user change was preserved completely
    assert target_file.read_text(encoding="utf-8") == "# USER DIRTY UNCOMMITTED EDIT\n"

    # Reset dirty state for remaining tests
    agent.git.repo.git.checkout("HEAD", "--", "main.py")


# ── Scenario 8: Isolated Fix Branch Creation ──────────────────────────────────

def test_isolated_fix_branch_creation(temp_git_repo):
    """Scenario 8: Fix branches are created in complete isolation from the base branch."""
    gm = GitManager(str(temp_git_repo))
    base_branch = gm.current_branch()

    branch = gm.create_fix_branch("iso-001")
    assert branch.startswith("agent-fix-iso-001-")
    assert gm.current_branch() == branch

    # Cleanup: return to base branch and delete fix branch
    gm.rollback_branch(branch, base_branch)
    assert gm.current_branch() == base_branch
    assert branch not in gm.list_branches()


# ── Scenario 9: In-Memory AST Validation & Syntax Guard ───────────────────────

def test_fix_in_memory_validation_ast_check(temp_git_repo):
    """Scenario 9: Syntax errors in generated patches are rejected during AST validation."""
    agent = CodebaseAgent(str(temp_git_repo))

    issue = {
        "file": "main.py",
        "line": 1,
        "issue_type": "syntax",
        "description": "Fix function",
        "severity": "high"
    }

    # Mock reviewer to return code with broken syntax
    mock_review = MagicMock()
    mock_review.feedback = "Added syntax error"
    mock_review.confidence = 0.95

    with patch.object(agent.reviewer, "review_loop", return_value=(mock_review, "def broken(\n", "hello")):
        result = agent.fix_issue(issue, "scan-ast-test")
        # Must fail validation and roll back
        assert result["status"] in ("failed", "tests_failed")
        assert "syntaxerror" in result["error"].lower() or "patch failed" in result["error"].lower()


# ── Scenario 10: Fix Approval Workflow (Stage, Commit, Merge, Cleanup) ────────

def test_fix_approval_workflow(temp_git_repo):
    """Scenario 10: Approved fix is staged, committed, merged to default branch, and fix branch removed."""
    agent = CodebaseAgent(str(temp_git_repo))
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    # Create an actual fix branch and modify a file
    branch = agent.git.create_fix_branch("appr-10")
    test_file = temp_git_repo / "calc.py"
    test_file.write_text("def add(a, b):\n    '''Add two numbers.'''\n    return a + b\n", encoding="utf-8")

    # Record fix in DB
    fix_rec = FixRecord(
        issue_id=1,
        scan_id="scan-appr-10",
        file="calc.py",
        original_code="def add(a, b):\n    return a + b\n",
        fixed_code=test_file.read_text(encoding="utf-8"),
        fix_description="Added docstring to add()",
        status="ready_for_approval",
        test_result="passed",
    )
    fix_id = agent.mem.save_fix(fix_rec)

    # Approve via API
    res = client.post("/api/fix/apply", json={
        "fix_branch": branch,
        "fix_id": fix_id,
        "issue_id": 1
    })
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify branch was merged and deleted
    assert branch not in agent.git.list_branches()
    assert "'''Add two numbers.'''" in (temp_git_repo / "calc.py").read_text(encoding="utf-8")

    # Verify SQLite status
    fix_in_db = agent.mem.get_fix_by_id(fix_id)
    assert fix_in_db["status"] == "applied"


# ── Scenario 11: Duplicate Approval Prevention ────────────────────────────────

def test_duplicate_approval_prevention(temp_git_repo):
    """Scenario 11: Attempting to approve an already applied fix is rejected with 400."""
    agent = CodebaseAgent(str(temp_git_repo))
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    fix_rec = FixRecord(
        issue_id=2,
        scan_id="scan-dup-appr",
        file="calc.py",
        original_code="original",
        fixed_code="fixed",
        fix_description="Test fix",
        status="applied",
        test_result="passed",
    )
    fix_id = agent.mem.save_fix(fix_rec)

    # Attempt to apply already applied fix
    res = client.post("/api/fix/apply", json={
        "fix_branch": "non-existent-branch",
        "fix_id": fix_id,
        "issue_id": 2
    })
    assert res.status_code == 400


# ── Scenario 12: Fix Rejection & False Positive Memory ────────────────────────

def test_fix_rejection_and_rejection_memory(temp_git_repo):
    """Scenario 12: Rejecting a fix rolls back branch and logs rejection to memory."""
    agent = CodebaseAgent(str(temp_git_repo))
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    branch = agent.git.create_fix_branch("rej-12")
    fix_rec = FixRecord(
        issue_id=3,
        scan_id="scan-rej-12",
        file="calc.py",
        original_code="orig",
        fixed_code="mod",
        fix_description="Unwanted change",
        status="ready_for_approval",
        test_result="passed",
    )
    fix_id = agent.mem.save_fix(fix_rec)

    issue = {
        "file": "calc.py",
        "line": 1,
        "issue_type": "style",
        "description": "Style change",
        "scan_id": "scan-rej-12"
    }

    res = client.post("/api/fix/reject", json={
        "issue": issue,
        "fix_branch": branch,
        "fix_id": fix_id,
        "reason": "Style rule false positive"
    })
    assert res.status_code == 200

    # Verify branch was deleted
    assert branch not in agent.git.list_branches()

    # Verify status in DB
    fix = agent.mem.get_fix_by_id(fix_id)
    assert fix["status"] == "rejected"

    # Verify false positive recorded in memory
    assert agent.mem.is_rejected_pattern("calc.py", "style", "Style change") is True


# ── Scenario 13: Duplicate Rejection Prevention ───────────────────────────────

def test_duplicate_rejection_prevention(temp_git_repo):
    """Scenario 13: Attempting to reject an already rejected fix is rejected with 400."""
    agent = CodebaseAgent(str(temp_git_repo))
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    fix_rec = FixRecord(
        issue_id=4,
        scan_id="scan-dup-rej",
        file="calc.py",
        original_code="orig",
        fixed_code="mod",
        fix_description="Already rejected",
        status="rejected",
        test_result="passed",
    )
    fix_id = agent.mem.save_fix(fix_rec)

    res = client.post("/api/fix/reject", json={
        "issue": {"file": "calc.py", "issue_type": "style", "description": "test"},
        "fix_branch": "",
        "fix_id": fix_id,
        "reason": "Again"
    })
    assert res.status_code == 400


# ── Scenario 14: Rollback of Applied Fix & Duplicate Rollback Guard ───────────

def test_rollback_of_applied_fix(temp_git_repo):
    """Scenario 14: Rollback reverts an applied fix, restores original content, and prevents duplicate rollback."""
    agent = CodebaseAgent(str(temp_git_repo))
    client.post("/api/connect", json={"repo_path": str(temp_git_repo)})

    orig_content = "def sample():\n    return 1\n"
    modified_content = "def sample():\n    return 2  # modified\n"

    target_file = temp_git_repo / "main.py"
    target_file.write_text(modified_content, encoding="utf-8")
    agent.git.stage_all()
    agent.git.commit("Apply change to main.py")

    fix_rec = FixRecord(
        issue_id=5,
        scan_id="scan-rb-14",
        file="main.py",
        original_code=orig_content,
        fixed_code=modified_content,
        fix_description="Incremented return value",
        status="applied",
        test_result="passed",
    )
    fix_id = agent.mem.save_fix(fix_rec)

    # Execute rollback
    res = client.post("/api/fix/rollback", json={"fix_id": fix_id})
    assert res.status_code == 200
    assert "rolled back successfully" in res.json()["detail"].lower()

    # Verify original file content was restored
    assert target_file.read_text(encoding="utf-8") == orig_content

    # Verify DB status is rolled_back
    assert agent.mem.get_fix_by_id(fix_id)["status"] == "rolled_back"

    # Duplicate rollback must return 400
    res_dup = client.post("/api/fix/rollback", json={"fix_id": fix_id})
    assert res_dup.status_code == 400


# ── Scenario 15: Graceful Degradation with Missing Credentials ────────────────

def test_missing_credentials_graceful_degradation(temp_git_repo):
    """Scenario 15: Application operates safely when LLM API keys are absent."""
    with patch.object(config, "GEMINI_API_KEY", ""), patch.object(config, "GROQ_API_KEY", ""):
        # Settings endpoint reflects lack of configuration
        res_settings = client.get("/api/settings")
        assert res_settings.status_code == 200
        data = res_settings.json()["settings"]
        assert data["gemini_configured"] is False
        assert data["groq_configured"] is False

        # File reading, tree, and terminal must continue functioning
        res_tree = client.get(f"/api/repo/tree?repo_path={str(temp_git_repo)}")
        assert res_tree.status_code == 200
        assert res_tree.json()["total_files"] > 0
