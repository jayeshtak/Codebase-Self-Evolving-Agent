import os
import sys
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
from ui.server import app, state
import config

client = TestClient(app)

REPO_ROOT = str(Path(__file__).parent.parent.resolve())

def test_api_status():
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "connected" in data
    assert "auto_fix_running" in data

def test_api_connect():
    response = client.post("/api/connect", json={"repo_path": REPO_ROOT})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert state.repo_path == REPO_ROOT

def test_api_repo_tree():
    response = client.get(f"/api/repo/tree?repo_path={REPO_ROOT}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["total_files"] > 0
    assert any(f["name"] == "main.py" for f in data["files"])

def test_api_repo_file_valid():
    response = client.post("/api/repo/file", json={"repo_path": REPO_ROOT, "file_path": "config.py"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["filename"] == "config.py"
    assert "def validate" in data["content"]

def test_api_repo_file_path_traversal_blocked():
    response = client.post("/api/repo/file", json={"repo_path": REPO_ROOT, "file_path": "../../../Windows/win.ini"})
    assert response.status_code in (403, 404)

def test_api_agents_status():
    response = client.get("/api/agents/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "agents" in data
    assert "scanner" in data["agents"]
    assert "coder" in data["agents"]
    assert "reviewer" in data["agents"]
    assert "executor" in data["agents"]

def test_api_settings_get_and_post():
    response = client.get("/api/settings")
    assert response.status_code == 200
    data = response.json()
    assert "settings" in data
    assert "complexity_threshold" in data["settings"]

    update_res = client.post("/api/settings", json={
        "complexity_threshold": 12,
        "max_review_iterations": 4
    })
    assert update_res.status_code == 200
    assert config.COMPLEXITY_THRESHOLD == 12
    assert config.MAX_REVIEW_ITERATIONS == 4

    # Reset
    config.COMPLEXITY_THRESHOLD = 10
    config.MAX_REVIEW_ITERATIONS = 3

def test_terminal_security_and_scrub():
    from core.terminal import Terminal
    term = Terminal(REPO_ROOT)
    
    # Path traversal outside workspace must be blocked
    outside_dir = str(Path(REPO_ROOT).parent.parent)
    res = term.run("git status", cwd=outside_dir)
    assert res.blocked is True
    assert "traversal" in res.block_reason.lower()

    # Secret scrubbing
    scrubbed = term._scrub_secrets("Running with key AIzaSy123456789012345678901234567890123")
    assert "AIzaSy" not in scrubbed
    assert "[REDACTED" in scrubbed
