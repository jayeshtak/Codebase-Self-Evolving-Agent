import sys
import os
import re
import json
import uuid
import threading
import tempfile
import subprocess
import urllib.parse
import mimetypes
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure mime types are correctly configured on Windows
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Add parent directory to path to find config, main, memory, etc.
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
import config
from main import CodebaseAgent
from memory.sqlite import SQLiteMemory, IssueRecord, FixRecord
from core.graph import DependencyGraph

app = FastAPI(title="Codebase Self-Evolving Agent API")

# Configure CORS - allows local development and onrender.com instances
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|.*\.onrender\.com)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Automated Background Keep-Alive to prevent Render free instances from sleeping
def start_render_keep_alive():
    """Background worker that periodically pings the public endpoint every 8 minutes to prevent sleep."""
    def worker():
        import time
        import urllib.request
        time.sleep(30)
        while True:
            render_url = (
                os.environ.get("RENDER_EXTERNAL_URL")
                or os.environ.get("KEEP_ALIVE_URL")
                or "https://codebase-self-evolving-agent.onrender.com"
            )
            is_render = bool(
                os.environ.get("RENDER")
                or os.environ.get("RENDER_EXTERNAL_URL")
                or os.environ.get("KEEP_ALIVE_URL")
            )
            if is_render and render_url:
                ping_endpoint = f"{render_url.rstrip('/')}/api/status"
                try:
                    req = urllib.request.Request(
                        ping_endpoint,
                        headers={"User-Agent": "RenderKeepAlivePinger/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        pass
                except Exception:
                    pass
            time.sleep(480)

    t = threading.Thread(target=worker, daemon=True, name="render_keep_alive")
    t.start()

start_render_keep_alive()

# Global State
class AppState:
    def __init__(self):
        self.agent: Optional[CodebaseAgent] = None
        self.repo_path: str = ""
        self.scan_results: Optional[dict] = None
        self.runtime_scan_results: Optional[dict] = None
        self.plan: Optional[Any] = None
        self.active_fix: Optional[dict] = None
        self.fix_result: Optional[dict] = None
        self.fix_history: List[dict] = []
        
        # Concurrency locks
        self.scan_lock = threading.Lock()
        self.fix_lock = threading.Lock()
        self.plan_lock = threading.Lock()
        self.active_run_id: Optional[str] = None

        # Parallel Auto-Fix state
        self.auto_fix_running: bool = False
        self.auto_fix_progress: List[dict] = []
        self.auto_fix_logs: List[str] = []
        self.auto_fix_completed: int = 0
        self.auto_fix_total: int = 0
        self.auto_fix_quota_exhausted: bool = False
        self.auto_fix_applied_count: int = 0
        self.auto_fix_review_count: int = 0
        self.auto_fix_lock = threading.Lock()

        # Orchestrator agent statuses
        self.agent_statuses = {
            "scanner": {"status": "idle", "task": "Awaiting scan trigger", "run_id": None, "last_run": None, "duration": 0, "issues": 0, "errors": 0},
            "dependency": {"status": "idle", "task": "Graph ready", "run_id": None, "last_run": None, "nodes": 0, "edges": 0},
            "planner": {"status": "idle", "task": "Awaiting issues to plan", "run_id": None, "last_run": None, "planned": 0, "skipped": 0},
            "coder": {"status": "idle", "task": "Idle", "run_id": None, "last_run": None, "generated": 0, "retries": 0},
            "reviewer": {"status": "idle", "task": "Idle", "run_id": None, "last_run": None, "reviewed": 0, "approved": 0},
            "test_writer": {"status": "idle", "task": "Idle", "run_id": None, "last_run": None, "written": 0},
            "executor": {"status": "idle", "task": "Idle", "run_id": None, "last_run": None, "passed": 0, "failed": 0},
            "approval": {"status": "idle", "task": "Awaiting human review", "run_id": None, "pending": 0, "applied": 0, "rejected": 0},
        }
        self.system_logs: List[dict] = []

    def log(self, agent: str, message: str, level: str = "info"):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "agent": agent,
            "message": message,
            "level": level
        }
        self.system_logs.append(entry)
        if len(self.system_logs) > 300:
            self.system_logs = self.system_logs[-300:]

state = AppState()

# Validation helpers
def validate_safe_repo_path(repo_path: str) -> Path:
    if not repo_path or not isinstance(repo_path, str) or not repo_path.strip():
        raise HTTPException(status_code=400, detail="Repository path is required.")
    if "\x00" in repo_path:
        raise HTTPException(status_code=400, detail="Invalid character in path.")
    
    repo_str = repo_path.strip()

    # Dynamic Git Clone support (e.g. https://github.com/owner/repo)
    if repo_str.startswith("http://") or repo_str.startswith("https://"):
        parsed = urllib.parse.urlparse(repo_str)
        if not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid repository URL.")
        url_path = parsed.path.rstrip("/")
        if url_path.endswith(".git"):
            url_path = url_path[:-4]
        repo_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(url_path).name) or "cloned_repo"
        sandbox_base = Path("/tmp/cloned_repos") if os.name != "nt" else Path(tempfile.gettempdir()) / "codebase_agent_repos"
        sandbox_base.mkdir(parents=True, exist_ok=True)
        dest_dir = sandbox_base / repo_name

        if not dest_dir.exists():
            cmd = ["git", "clone", "--depth", "1", repo_str, str(dest_dir)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                if proc.returncode != 0:
                    raise HTTPException(status_code=400, detail=f"Failed to clone repository: {proc.stderr[:200]}")
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=408, detail="Cloning repository timed out.")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error executing git clone: {str(e)}")
        else:
            # Sync remote changes so new commits are fetched immediately
            try:
                subprocess.run(["git", "fetch", "--depth", "1", "origin"], cwd=str(dest_dir), capture_output=True, text=True, timeout=30)
                subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=str(dest_dir), capture_output=True, text=True, timeout=30)
            except Exception:
                pass
        # Configure authenticated origin remote URL for git push if GITHUB_TOKEN is available
        token = getattr(config, "GITHUB_TOKEN", None) or os.environ.get("GITHUB_TOKEN", "")
        if token and "github.com" in repo_str:
            clean_url = repo_str.split("@")[-1]
            if clean_url.startswith("https://"):
                clean_url = clean_url[8:]
            elif clean_url.startswith("http://"):
                clean_url = clean_url[7:]
            auth_url = f"https://x-access-token:{token}@{clean_url}"
            try:
                subprocess.run(["git", "remote", "set-url", "origin", auth_url], cwd=str(dest_dir), capture_output=True, timeout=10)
            except Exception:
                pass

        return dest_dir.resolve()

    p = Path(repo_str).resolve()
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"Repository path does not exist: {repo_path}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"Repository path is a file, not a directory: {repo_path}")
    if getattr(config, "DEMO_MODE", False):
        app_root = Path(__file__).parent.parent.resolve()
        sandbox_base = Path("/tmp/cloned_repos") if os.name != "nt" else Path(tempfile.gettempdir()) / "codebase_agent_repos"
        in_app = False
        in_sandbox = False
        try:
            p.relative_to(app_root)
            in_app = True
        except ValueError:
            pass

        try:
            p.relative_to(sandbox_base)
            in_sandbox = True
        except ValueError:
            pass

        if not in_app and not in_sandbox:
            raise HTTPException(
                status_code=403,
                detail="Public Demo Mode restriction: Only the demonstration repository boundary or cloned repositories can be inspected."
            )
    return p

def validate_safe_file_path(repo_root: Path, file_path: str) -> Path:
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        raise HTTPException(status_code=400, detail="File path is required.")
    if "\x00" in file_path:
        raise HTTPException(status_code=400, detail="Invalid character in file path.")
    target = (repo_root / file_path.strip()).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path traversal outside repository boundary is forbidden.")
    return target

# Request schemas
class ConnectRequest(BaseModel):
    repo_path: str

class LoadRequest(BaseModel):
    repo_path: str

class SearchRequest(BaseModel):
    repo_path: str
    query: str
    n: Optional[int] = 8

class ProfileScanRequest(BaseModel):
    repo_path: str
    entry_script: Optional[str] = ""

class DocScanRequest(BaseModel):
    repo_path: str

class RuntimeScanRequest(BaseModel):
    repo_path: str
    run_command: Optional[str] = ""

class PlanRequest(BaseModel):
    repo_path: str
    scan_results: dict

class AutoFixRequest(BaseModel):
    repo_path: str
    scan_id: str
    issues: List[dict]
    confidence: Optional[float] = 0.85
    max_auto_fix: Optional[int] = 100

class FixIssueRequest(BaseModel):
    issue: dict
    scan_id: str

class ApplyFixRequest(BaseModel):
    fix_branch: str
    fix_id: int
    issue_id: int

class RejectFixRequest(BaseModel):
    issue: dict
    fix_branch: str
    fix_id: int
    reason: Optional[str] = ""

class RollbackRequest(BaseModel):
    fix_id: int

class ScheduleDailyRequest(BaseModel):
    repo_path: str
    hour: int

class ScheduleIntervalRequest(BaseModel):
    repo_path: str
    minutes: int

class DeleteBranchRequest(BaseModel):
    branch: str

class FileContentRequest(BaseModel):
    repo_path: str
    file_path: str

class RunTestsRequest(BaseModel):
    repo_path: str
    test_path: Optional[str] = ""

class ImpactRequest(BaseModel):
    repo_path: str
    file_path: str

class SettingsUpdateRequest(BaseModel):
    gemini_model: Optional[str] = None
    groq_model: Optional[str] = None
    max_review_iterations: Optional[int] = None
    max_heal_attempts: Optional[int] = None
    complexity_threshold: Optional[int] = None
    function_length_threshold: Optional[int] = None
    nesting_depth_threshold: Optional[int] = None
    gemini_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    github_token: Optional[str] = None

class DecisionRequest(BaseModel):
    file: str
    issue_type: str
    description: str
    decision: str  # 'accepted' | 'rejected' | 'false_positive' | 'rollback'
    reason: Optional[str] = ""
    scan_id: Optional[str] = None
    fix_id: Optional[int] = None

# Helper functions
def get_agent(repo_path: str) -> CodebaseAgent:
    if not state.agent or state.repo_path != repo_path:
        # Validate config and load agent
        config.validate()
        state.agent = CodebaseAgent(repo_path)
        state.repo_path = repo_path
    return state.agent

def load_last_scan(repo_path: str) -> dict:
    scan_file = Path(repo_path) / "data" / "last_scan.json"
    if scan_file.exists():
        try:
            return json.loads(scan_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def load_last_plan(repo_path: str) -> dict:
    plan_file = Path(repo_path) / "data" / "last_plan.json"
    if plan_file.exists():
        try:
            return json.loads(plan_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

# Endpoints
@app.get("/api/status")
async def get_status():
    connected = state.agent is not None
    stats = {}
    if connected:
        try:
            stats = state.agent.stats()
        except Exception:
            pass
    return {
        "connected": connected,
        "repo_path": state.repo_path,
        "demo_mode": getattr(config, "DEMO_MODE", False),
        "stats": stats,
        "active_fix": state.active_fix,
        "fix_result": state.fix_result,
        "auto_fix_running": state.auto_fix_running
    }

@app.post("/api/connect")
async def connect_repo(req: ConnectRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    try:
        config.validate()
        state.agent = CodebaseAgent(str(repo_p))
        state.repo_path = str(repo_p)
        state.scan_results = load_last_scan(str(repo_p))
        return {
            "status": "success",
            "detail": "Repository connected successfully.",
            "repo_path": str(repo_p),
            "branch": state.agent.git.current_branch(),
            "scan_results": state.scan_results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect: {str(e)}")

@app.post("/api/browse")
async def browse_folder():
    if getattr(config, "DEMO_MODE", False):
        return {"status": "error", "detail": "Local folder picker is disabled in public demo mode.", "path": ""}
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Select Repository Folder")
        root.destroy()
        return {"status": "success", "path": folder or ""}
    except Exception as e:
        return {"status": "error", "detail": str(e), "path": ""}

@app.post("/api/load")
async def load_scan(req: LoadRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    results = load_last_scan(str(repo_p))
    if results:
        state.scan_results = results
        state.repo_path = str(repo_p)
        return {"status": "success", "scan_results": results}
    else:
        return {"status": "warning", "detail": "No previous scan found.", "scan_results": None}

@app.post("/api/scan")
async def run_scan(req: ConnectRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    if not state.scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scan is already in progress on this repository.")
    
    run_id = f"scan-{uuid.uuid4().hex[:8]}"
    state.active_run_id = run_id
    state.agent_statuses["scanner"]["status"] = "running"
    state.agent_statuses["scanner"]["run_id"] = run_id
    state.agent_statuses["scanner"]["task"] = "Executing static analysis, complexity, security checks"
    state.log("scanner", f"Started codebase scan [{run_id}]", "info")
    t0 = datetime.now()
    try:
        agent = get_agent(str(repo_p))
        results = agent.scan(full_scan=True)
        dur = (datetime.now() - t0).total_seconds()
        state.scan_results = results
        total = results.get("total_issues", len(results.get("issues", [])))
        state.agent_statuses["scanner"]["status"] = "idle"
        state.agent_statuses["scanner"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["scanner"]["duration"] = round(dur, 2)
        state.agent_statuses["scanner"]["issues"] = total
        state.log("scanner", f"Scan finished in {dur:.2f}s. Discovered {total} issue(s).", "success")
        return {"status": "success", "run_id": run_id, "scan_results": results}
    except Exception as e:
        state.agent_statuses["scanner"]["status"] = "error"
        state.agent_statuses["scanner"]["errors"] += 1
        state.log("scanner", f"Scan failed: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")
    finally:
        state.scan_lock.release()

@app.post("/api/scan/incremental")
async def run_incremental_scan(req: ConnectRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    if not state.scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scan is already in progress on this repository.")
    
    run_id = f"inc-{uuid.uuid4().hex[:8]}"
    state.active_run_id = run_id
    state.agent_statuses["scanner"]["status"] = "running"
    state.agent_statuses["scanner"]["run_id"] = run_id
    state.agent_statuses["scanner"]["task"] = "Executing intelligent incremental scan with dependency invalidation"
    state.log("scanner", f"Started incremental codebase scan [{run_id}]", "info")
    t0 = datetime.now()
    try:
        agent = get_agent(str(repo_p))
        results = agent.scan_incremental()
        dur = (datetime.now() - t0).total_seconds()
        state.scan_results = results
        total = results.get("total_issues", len(results.get("issues", [])))
        state.agent_statuses["scanner"]["status"] = "idle"
        state.agent_statuses["scanner"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["scanner"]["duration"] = round(dur, 2)
        state.agent_statuses["scanner"]["issues"] = total
        state.log("scanner", f"Incremental scan finished in {dur:.2f}s. {total} issue(s) active.", "success")
        return {"status": "success", "run_id": run_id, "scan_results": results}
    except Exception as e:
        state.agent_statuses["scanner"]["status"] = "error"
        state.agent_statuses["scanner"]["errors"] += 1
        state.log("scanner", f"Incremental scan failed: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=f"Incremental scan failed: {str(e)}")
    finally:
        state.scan_lock.release()

@app.post("/api/search")
async def search_rag(req: SearchRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        hits = agent.rag.search(req.query, n_results=req.n)
        return {"status": "success", "hits": hits or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/api/scan/profile")
async def run_profile_scan(req: ProfileScanRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        prof_results = agent.profile_scan(entry_script=req.entry_script or "")
        n = prof_results.get("total_issues", 0)
        if n and state.scan_results:
            state.scan_results["issues"] = (
                state.scan_results.get("issues", []) + prof_results.get("issues", [])
            )
            state.scan_results["total_issues"] = len(state.scan_results["issues"])
        return {"status": "success", "profile_results": prof_results, "scan_results": state.scan_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile scan failed: {str(e)}")

@app.post("/api/scan/doc")
async def run_doc_scan(req: DocScanRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        doc_results = agent.doc_scan()
        n = doc_results.get("total_issues", 0)
        if n and state.scan_results:
            state.scan_results["issues"] = (
                state.scan_results.get("issues", []) + doc_results.get("issues", [])
            )
            state.scan_results["total_issues"] = len(state.scan_results["issues"])
        return {"status": "success", "doc_results": doc_results, "scan_results": state.scan_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Doc scan failed: {str(e)}")

@app.post("/api/scan/runtime")
async def run_runtime_scan(req: RuntimeScanRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        rt_results = agent.runtime_scan(run_command=req.run_command or "")
        state.runtime_scan_results = rt_results
        return {"status": "success", "runtime_results": rt_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Runtime scan failed: {str(e)}")

@app.post("/api/plan")
async def generate_plan(req: PlanRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    if not state.plan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A plan generation is already in progress.")
    
    run_id = f"plan-{uuid.uuid4().hex[:8]}"
    state.agent_statuses["planner"]["status"] = "running"
    state.agent_statuses["planner"]["run_id"] = run_id
    state.agent_statuses["planner"]["task"] = "Prioritizing issues and generating remediation roadmap"
    state.log("planner", f"Generating prioritized fix plan [{run_id}]...", "info")
    t0 = datetime.now()
    try:
        agent = get_agent(str(repo_p))
        plan_obj = agent.plan(req.scan_results)
        dur = (datetime.now() - t0).total_seconds()
        state.plan = plan_obj
        plan_dict = plan_obj.to_dict()
        actionable_count = len(plan_obj.actionable())
        state.agent_statuses["planner"]["status"] = "idle"
        state.agent_statuses["planner"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["planner"]["planned"] = actionable_count
        state.agent_statuses["planner"]["skipped"] = len(plan_obj.skipped)
        state.log("planner", f"Plan ready: {actionable_count} actionable items in {dur:.2f}s.", "success")
        return {"status": "success", "run_id": run_id, "plan": plan_dict}
    except Exception as e:
        state.agent_statuses["planner"]["status"] = "error"
        state.log("planner", f"Plan failed: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {str(e)}")
    finally:
        state.plan_lock.release()

@app.get("/api/plan/last")
async def get_last_plan(repo_path: str):
    repo_p = validate_safe_repo_path(repo_path)
    plan_data = load_last_plan(str(repo_p))
    return {"status": "success", "plan": plan_data}

@app.get("/api/fix/active")
async def get_active_fix():
    return {
        "status": "success",
        "active_fix": state.active_fix,
        "fix_result": state.fix_result
    }

@app.post("/api/fix/generate")
async def generate_fix(req: FixIssueRequest):
    if not state.agent:
        raise HTTPException(status_code=400, detail="Agent not connected. Connect a repository first.")
    if not state.fix_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A fix synthesis is already in progress.")

    run_id = f"fix-{uuid.uuid4().hex[:8]}"
    file_label = f"{req.issue.get('file', '')}:{req.issue.get('line', 0)}"
    state.agent_statuses["coder"]["status"] = "running"
    state.agent_statuses["coder"]["run_id"] = run_id
    state.agent_statuses["coder"]["task"] = f"Generating fix for {file_label}"
    state.agent_statuses["reviewer"]["status"] = "running"
    state.agent_statuses["reviewer"]["run_id"] = run_id
    state.agent_statuses["reviewer"]["task"] = f"Reviewing fix candidate for {file_label}"
    state.log("coder", f"Synthesizing fix [{run_id}] for {file_label} [{req.issue.get('issue_type')}]", "info")
    t0 = datetime.now()
    try:
        state.active_fix = req.issue
        fix_result = state.agent.fix_issue(req.issue, req.scan_id)
        state.fix_result = fix_result
        dur = (datetime.now() - t0).total_seconds()
        state.agent_statuses["coder"]["status"] = "idle"
        state.agent_statuses["coder"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["coder"]["generated"] += 1
        state.agent_statuses["reviewer"]["status"] = "idle"
        state.agent_statuses["reviewer"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["reviewer"]["reviewed"] += 1
        
        status = fix_result.get("status")
        if status == "ready_for_approval":
            state.agent_statuses["reviewer"]["approved"] += 1
            state.agent_statuses["approval"]["pending"] += 1
            state.log("reviewer", f"Fix candidate validated (conf={fix_result.get('confidence', 0):.2f}) in {dur:.2f}s.", "success")
        elif status == "blocked":
            state.log("coder", f"Fix blocked: {fix_result.get('error')}", "warning")
        else:
            state.log("reviewer", f"Fix verification failed: {fix_result.get('error', 'Unknown')}", "warning")
        return {"status": "success", "run_id": run_id, "fix_result": fix_result}
    except Exception as e:
        state.agent_statuses["coder"]["status"] = "error"
        state.agent_statuses["reviewer"]["status"] = "error"
        state.log("coder", f"Fix pipeline error: {str(e)}", "error")
        raise HTTPException(status_code=500, detail=f"Fix generation failed: {str(e)}")
    finally:
        state.fix_lock.release()

@app.post("/api/fix/apply")
async def apply_fix(req: ApplyFixRequest):
    if getattr(config, "DEMO_MODE", False):
        raise HTTPException(
            status_code=403,
            detail="Public Demo Mode: Merging patches into the main branch is disabled to preserve demonstration repository integrity. Fix generation, AST diffing, test writing, and review studio remain fully functional."
        )
    if not state.agent:
        raise HTTPException(status_code=400, detail="Agent not connected.")
    try:
        success = state.agent.apply_fix(
            fix_branch=req.fix_branch,
            fix_id=req.fix_id,
            issue_id=req.issue_id,
        )
        if success:
            state.fix_result = None
            state.active_fix = None
            state.agent_statuses["approval"]["applied"] += 1
            if state.agent_statuses["approval"]["pending"] > 0:
                state.agent_statuses["approval"]["pending"] -= 1
            state.fix_history.append({
                "file": req.fix_branch,
                "description": "Fix applied successfully.",
                "status": "applied",
                "time": datetime.now().strftime("%H:%M:%S")
            })
            return {"status": "success", "detail": "Fix applied successfully."}
        else:
            raise HTTPException(status_code=400, detail="Merge failed or fix already applied/rejected.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to apply fix: {str(e)}")

@app.post("/api/fix/reject")
async def reject_fix(req: RejectFixRequest):
    if not state.agent:
        raise HTTPException(status_code=400, detail="Agent not connected.")
    try:
        success = state.agent.reject_fix(
            issue=req.issue,
            fix_branch=req.fix_branch,
            fix_id=req.fix_id,
            reason=req.reason or "",
        )
        if not success:
            raise HTTPException(status_code=400, detail="Fix is already applied, rejected, or rolled back.")
        state.fix_result = None
        state.active_fix = None
        state.agent_statuses["approval"]["rejected"] += 1
        if state.agent_statuses["approval"]["pending"] > 0:
            state.agent_statuses["approval"]["pending"] -= 1
        state.fix_history.append({
            "file": req.issue.get("file", "unknown"),
            "description": f"Rejected: {req.reason or ''}",
            "status": "rejected",
            "time": datetime.now().strftime("%H:%M:%S")
        })
        return {"status": "success", "detail": "Fix rejected successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reject fix: {str(e)}")

# Background auto fix worker logic with deterministic sequential Git safety
def auto_fix_worker(issues: List[dict], scan_id: str, threshold: float, cap: int, repo_path: str):
    from agents.coder import QuotaExhaustedException
    
    agent = get_agent(repo_path)
    
    with state.auto_fix_lock:
        state.auto_fix_running = True
        state.auto_fix_progress = []
        state.auto_fix_logs = [f"[{datetime.now().strftime('%H:%M:%S')}] Starting batch auto-fix for {len(issues)} issue(s) (target threshold: {threshold:.2f})."]
        state.auto_fix_completed = 0
        state.auto_fix_total = len(issues)
        state.auto_fix_quota_exhausted = False
        state.auto_fix_applied_count = 0
        state.auto_fix_review_count = 0

    quota_exhausted = False

    for idx, issue in enumerate(issues):
        label = f"{issue.get('file')}:{issue.get('line')} [{issue.get('issue_type')}]"
        
        if quota_exhausted:
            entry = {
                "label": label,
                "status": "skipped",
                "explanation": "Quota exhausted",
                "confidence": 0.0,
                "issue": issue,
                "diff": ""
            }
            with state.auto_fix_lock:
                state.auto_fix_completed += 1
                state.auto_fix_progress.append(entry)
            continue

        try:
            with state.auto_fix_lock:
                state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] ({idx + 1}/{len(issues)}) Synthesizing fix for {label}...")
            state.log("coder", f"Batch fix synthesizing for {label}", "info")
            state.agent_statuses["coder"]["status"] = "running"
            state.agent_statuses["coder"]["task"] = f"Batch fix: {label}"
            
            fix_result = agent.fix_issue(issue, scan_id)
            status = fix_result.get("status")
            confidence = fix_result.get("confidence", 0.0)
            diff_text = fix_result.get("diff", "")
            fix_id = fix_result.get("fix_id", 0)
            fix_branch = fix_result.get("fix_branch", "")

            state.agent_statuses["coder"]["generated"] += 1
            state.agent_statuses["reviewer"]["reviewed"] += 1

            if status == "ready_for_approval":
                if confidence >= threshold:
                    with state.auto_fix_lock:
                        state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Confidence ({confidence:.2f} >= {threshold:.2f}) verified. Merging into main: {label}")
                    state.log("approval", f"Auto-merging verified fix for {label}", "info")
                    
                    success = agent.apply_fix(
                        fix_branch=fix_branch,
                        fix_id=fix_id,
                        issue_id=issue.get("id", 0),
                    )
                    
                    if success:
                        state.agent_statuses["approval"]["applied"] += 1
                        entry = {
                            "label": label,
                            "status": "applied",
                            "explanation": fix_result.get("explanation", ""),
                            "confidence": confidence,
                            "auto_applied": True,
                            "issue": issue,
                            "diff": diff_text,
                            "fix_id": fix_id
                        }
                    else:
                        entry = {
                            "label": label,
                            "status": "merge_failed",
                            "explanation": "AST patch created but merge failed",
                            "confidence": confidence,
                            "auto_applied": False,
                            "issue": issue,
                            "diff": diff_text,
                            "fix_id": fix_id
                        }
                else:
                    with state.auto_fix_lock:
                        state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Confidence ({confidence:.2f} < {threshold:.2f}). Queued for manual review: {label}")
                    entry = {
                        "label": label,
                        "status": "pending_review",
                        "explanation": fix_result.get("explanation", ""),
                        "confidence": confidence,
                        "fix_result": fix_result,
                        "auto_applied": False,
                        "issue": issue,
                        "diff": diff_text,
                        "fix_id": fix_id,
                        "fix_branch": fix_branch
                    }
            else:
                err = fix_result.get("error", "")
                if "QuotaExhausted" in err or ("Both Gemini" in err and "Groq" in err):
                    quota_exhausted = True
                    entry = {"label": label, "status": "quota_exhausted", "explanation": err, "confidence": confidence, "issue": issue, "diff": ""}
                else:
                    entry = {"label": label, "status": status or "failed", "explanation": err, "confidence": confidence, "issue": issue, "diff": ""}

        except QuotaExhaustedException as e:
            quota_exhausted = True
            entry = {"label": label, "status": "quota_exhausted", "explanation": str(e), "confidence": 0.0, "issue": issue, "diff": ""}
        except Exception as e:
            entry = {"label": label, "status": "error", "explanation": str(e), "confidence": 0.0, "issue": issue, "diff": ""}

        with state.auto_fix_lock:
            state.auto_fix_completed += 1
            state.auto_fix_progress.append(entry)
            if entry["status"] == "applied":
                state.auto_fix_applied_count += 1
            elif entry["status"] == "pending_review":
                state.auto_fix_review_count += 1
            state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Completed {state.auto_fix_completed}/{state.auto_fix_total}: {entry['label']} -> {entry['status']}")

    state.agent_statuses["coder"]["status"] = "idle"
    state.agent_statuses["reviewer"]["status"] = "idle"

    with state.auto_fix_lock:
        state.auto_fix_running = False
        if quota_exhausted:
            state.auto_fix_quota_exhausted = True
            state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Stopped — API quotas exhausted.")
        else:
            state.auto_fix_logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] Batch fix complete! Merged: {state.auto_fix_applied_count}, Manual Review: {state.auto_fix_review_count}")

@app.post("/api/fix/auto")
async def start_auto_fix(req: AutoFixRequest, background_tasks: BackgroundTasks):
    global state
    if state.auto_fix_running:
        raise HTTPException(status_code=409, detail="Auto-fix run is already in progress.")
    
    cap = req.max_auto_fix or 100
    threshold = req.confidence if req.confidence is not None else 0.85
    issues = req.issues[:cap]
    
    background_tasks.add_task(
        auto_fix_worker,
        issues=issues,
        scan_id=req.scan_id or "manual",
        threshold=threshold,
        cap=cap,
        repo_path=req.repo_path
    )
    return {"status": "success", "detail": "Batch auto-fix started in background."}

@app.get("/api/fix/auto/status")
async def get_auto_fix_status():
    with state.auto_fix_lock:
        return {
            "running": state.auto_fix_running,
            "completed": state.auto_fix_completed,
            "total": state.auto_fix_total,
            "progress": state.auto_fix_progress,
            "logs": state.auto_fix_logs,
            "quota_exhausted": state.auto_fix_quota_exhausted,
            "applied_count": state.auto_fix_applied_count,
            "review_count": state.auto_fix_review_count
        }

@app.get("/api/history/scans")
async def get_scans_history():
    try:
        mem = SQLiteMemory()
        scans = mem.get_all_scans()
        mem.close()
        return {"status": "success", "scans": scans or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/history/scans/{scan_id}/load")
async def load_history_scan(scan_id: str):
    try:
        mem = SQLiteMemory()
        scan_issues = mem.get_issues_for_scan(scan_id)
        
        # Look up scan metadata
        scans = mem.get_all_scans()
        scan_meta = next((s for s in scans if s["id"] == scan_id), {})
        mem.close()
        
        state.scan_results = {
            "scan_id": scan_id,
            "total_issues": len(scan_issues),
            "issues": scan_issues,
            "started_at": scan_meta.get("started_at", ""),
            "finished_at": scan_meta.get("finished_at", "")
        }
        return {"status": "success", "scan_results": state.scan_results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history/fixes")
async def get_applied_fixes():
    try:
        mem = SQLiteMemory()
        fixes = mem.get_applied_fixes(limit=100)
        mem.close()
        return {"status": "success", "fixes": fixes or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/fix/rollback")
async def rollback_fix(req: RollbackRequest):
    if not state.agent:
        raise HTTPException(status_code=400, detail="Agent not connected.")
    try:
        success = state.agent.rollback_fix(req.fix_id)
        if success:
            state.log("approval", f"Fix #{req.fix_id} was successfully rolled back.", "info")
            return {"status": "success", "detail": f"Fix #{req.fix_id} rolled back successfully."}
        else:
            raise HTTPException(status_code=400, detail=f"Cannot rollback fix #{req.fix_id}: either already rolled back or not in applied state.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history/rejections")
async def get_rejections():
    try:
        mem = SQLiteMemory()
        rejections = mem.get_rejections()
        mem.close()
        return {"status": "success", "rejections": rejections or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats/dashboard")
async def get_dashboard_stats():
    try:
        mem = SQLiteMemory()
        stats = mem.get_stats()
        health_trend = mem.get_health_trend(limit=30)
        latest_health = mem.get_latest_health_score()
        trends = mem.get_scan_trends(limit=30)
        evaluation = mem.get_evaluation_summary()
        recent_outcomes = mem.get_recent_outcomes(limit=20)
        
        # Get problematic files
        problematic_files_raw = mem.conn.execute(
            "SELECT file, COUNT(*) c FROM issues GROUP BY file ORDER BY c DESC LIMIT 10"
        ).fetchall()
        problematic_files = [dict(r) for r in problematic_files_raw]
        mem.close()
        
        return {
            "status": "success",
            "stats": stats,
            "health_trend": health_trend,
            "latest_health": latest_health,
            "trends": trends,
            "evaluation": evaluation,
            "recent_outcomes": recent_outcomes,
            "problematic_files": problematic_files
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/daily")
async def schedule_daily(req: ScheduleDailyRequest):
    agent = get_agent(req.repo_path)
    try:
        agent.scheduler.start_daily(hour=req.hour)
        return {"status": "success", "detail": f"Daily scan scheduled at {req.hour:02d}:00."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/interval")
async def schedule_interval(req: ScheduleIntervalRequest):
    agent = get_agent(req.repo_path)
    try:
        agent.scheduler.start_interval(minutes=req.minutes)
        return {"status": "success", "detail": f"Scan scheduled every {req.minutes} minute(s)."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/stop")
async def schedule_stop(req: ConnectRequest):
    agent = get_agent(req.repo_path)
    try:
        agent.scheduler.stop()
        return {"status": "success", "detail": "Scheduler stopped."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/schedule/status")
async def get_schedule_status(repo_path: str):
    agent = get_agent(repo_path)
    try:
        status = agent.scheduler.status()
        return {"status": "success", "status_info": status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/pause")
async def pause_scheduler(req: ConnectRequest):
    agent = get_agent(req.repo_path)
    try:
        agent.scheduler.pause()
        return {"status": "success", "detail": "Continuous improvement scans paused."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule/resume")
async def resume_scheduler(req: ConnectRequest):
    agent = get_agent(req.repo_path)
    try:
        agent.scheduler.resume()
        return {"status": "success", "detail": "Continuous improvement scans resumed."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/insights/debt")
async def get_insights_debt():
    try:
        mem = SQLiteMemory()
        debt_summary = mem.get_technical_debt_summary()
        profile = mem.get_developer_learning_profile()
        recurring = mem.get_recurring_issues(limit=15)
        mem.close()
        return {
            "status": "success",
            "technical_debt": debt_summary,
            "learning_profile": profile,
            "recurring_issues": recurring,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/feedback/decision")
async def record_feedback_decision(req: DecisionRequest):
    try:
        mem = SQLiteMemory()
        mem.record_developer_decision(
            file=req.file,
            issue_type=req.issue_type,
            description=req.description,
            decision=req.decision,
            reason=req.reason or "",
            scan_id=req.scan_id,
            fix_id=req.fix_id,
        )
        mem.close()
        return {"status": "success", "detail": f"Developer decision '{req.decision}' recorded."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/branches")
async def get_branches(repo_path: str):
    agent = get_agent(repo_path)
    try:
        all_branches = agent.git.list_branches()
        fix_branches = sorted([b for b in all_branches if b.startswith("agent-fix-")], reverse=True)
        current = agent.git.current_branch()
        return {"status": "success", "branches": fix_branches, "current": current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/branches/delete")
async def delete_branch(req: DeleteBranchRequest):
    if not state.agent:
        raise HTTPException(status_code=400, detail="Agent not connected.")
    try:
        state.agent.git.delete_branch(req.branch, force=True)
        return {"status": "success", "detail": f"Branch '{req.branch}' deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/branches/delete-all")
async def delete_all_branches(req: ConnectRequest):
    agent = get_agent(req.repo_path)
    try:
        all_branches = agent.git.list_branches()
        fix_branches = [b for b in all_branches if b.startswith("agent-fix-")]
        current = agent.git.current_branch()
        deleted_count = 0
        for br in fix_branches:
            if br != current:
                agent.git.delete_branch(br, force=True)
                deleted_count += 1
        return {"status": "success", "detail": f"Deleted {deleted_count} branch(es)."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/graph/build")
async def build_graph(req: ConnectRequest):
    agent = get_agent(req.repo_path)
    try:
        dg = DependencyGraph(req.repo_path)
        dg.build()
        G = dg.graph
        
        # Limit to top 40 nodes to be readable
        if len(G.nodes) > 40:
            top = sorted(G.degree(), key=lambda x: x[1], reverse=True)[:40]
            top_names = {n for n, _ in top}
            G = G.subgraph(top_names).copy()

        # Count issues per file from latest scan results
        issue_counts = {}
        if state.scan_results:
            for iss in state.scan_results.get("issues", []):
                f = iss.get("file", "")
                issue_counts[f] = issue_counts.get(f, 0) + 1

        nodes = []
        for node in G.nodes():
            label = node.split("/")[-1] or node.split("\\")[-1]
            issues_n = issue_counts.get(node, 0)
            degree = G.degree(node)
            color = "#e74c3c" if issues_n > 0 else "#27ae60"
            size = max(10, min(40, 10 + degree * 3))
            nodes.append({
                "id": node,
                "label": label,
                "title": f"{node}\n{issues_n} issue(s)",
                "color": color,
                "size": size
            })

        edges = []
        for src, dst in G.edges():
            if src in G.nodes() and dst in G.nodes():
                edges.append({"from": src, "to": dst})

        return {"status": "success", "nodes": nodes, "edges": edges}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/benchmark/run")
async def run_benchmark():
    try:
        from benchmarks.runner import main as run_bench
        bench_data = run_bench()
        return {"status": "success", "benchmark": bench_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/report/export")
async def export_report(req: ConnectRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        from core.reporter import generate_html_report, save_report
        html = generate_html_report(
            repo_path=str(repo_p),
            scan_results=state.scan_results or {},
            plan=state.plan,
            fix_history=state.fix_history,
            runtime_results=state.runtime_scan_results,
            stats=agent.stats(),
        )
        report_path = save_report(str(repo_p), html)
        return {
            "status": "success",
            "report_path": report_path,
            "html": html,
            "filename": f"codebase_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── New Endpoints for Command Center Experience ────────────────────────────────

@app.get("/api/repo/tree")
async def get_repo_tree(repo_path: str):
    root = validate_safe_repo_path(repo_path)
    
    # Map issues per file
    issues_by_file = {}
    if state.scan_results:
        for iss in state.scan_results.get("issues", []):
            f = iss.get("file", "").replace("\\", "/")
            issues_by_file[f] = issues_by_file.get(f, 0) + 1

    items = []
    ignore_dirs = {".git", "venv", ".venv", "env", ".env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build"}
    for r, dirs, filenames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
        for fn in filenames:
            p = Path(r) / fn
            if p.is_file():
                rel = str(p.relative_to(root)).replace("\\", "/")
                suffix = p.suffix.lower()
                try:
                    stat = p.stat()
                    items.append({
                        "path": rel,
                        "name": p.name,
                        "size": stat.st_size,
                        "extension": suffix,
                        "issues_count": issues_by_file.get(rel, 0),
                        "directory": str(p.parent.relative_to(root)).replace("\\", "/") if p.parent != root else "",
                    })
                except Exception:
                    continue

    return {
        "status": "success",
        "root": str(root),
        "total_files": len(items),
        "files": sorted(items, key=lambda x: x["path"])
    }

@app.post("/api/repo/file")
async def get_repo_file(req: FileContentRequest):
    root = validate_safe_repo_path(req.repo_path)
    file_path = validate_safe_file_path(root, req.file_path)
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {e}")

    lines = content.splitlines()
    rel_str = str(file_path.relative_to(root)).replace("\\", "/")
    
    file_issues = []
    if state.scan_results:
        for iss in state.scan_results.get("issues", []):
            if iss.get("file", "").replace("\\", "/") == rel_str:
                file_issues.append(iss)

    return {
        "status": "success",
        "file_path": rel_str,
        "filename": file_path.name,
        "total_lines": len(lines),
        "extension": file_path.suffix.lower(),
        "content": content,
        "issues": file_issues
    }

@app.get("/api/agents/status")
async def get_agents_status():
    if state.scan_results:
        state.agent_statuses["scanner"]["issues"] = state.scan_results.get("total_issues", len(state.scan_results.get("issues", [])))
    if state.plan:
        plan_dict = state.plan.to_dict() if hasattr(state.plan, "to_dict") else state.plan
        state.agent_statuses["planner"]["planned"] = len(plan_dict.get("items", []))
    
    return {
        "status": "success",
        "agents": state.agent_statuses,
        "logs": state.system_logs,
        "auto_fix_running": state.auto_fix_running
    }

@app.post("/api/tests/run")
async def run_project_tests(req: RunTestsRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        state.log("executor", "Running test suite...", "info")
        target = req.test_path.strip() if req.test_path else "."
        fw = agent.executor.detect_test_framework(target)
        result = agent.executor.run_tests(target, framework=fw, timeout=120)
        state.agent_statuses["executor"]["status"] = "idle"
        state.agent_statuses["executor"]["last_run"] = datetime.now().strftime("%H:%M:%S")
        state.agent_statuses["executor"]["passed"] = result.passed_count
        state.agent_statuses["executor"]["failed"] = result.failed_count
        state.log("executor", f"Test execution finished: {result.summary()}", "success" if result.passed else "error")
        return {
            "status": "success",
            "passed": result.passed,
            "total": result.total,
            "passed_count": result.passed_count,
            "failed_count": result.failed_count,
            "error_count": result.error_count,
            "duration": result.duration,
            "summary": result.summary(),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "failures": result.failures
        }
    except Exception as e:
        state.log("executor", f"Test run error: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/graph/impact")
async def get_graph_impact(req: ImpactRequest):
    repo_p = validate_safe_repo_path(req.repo_path)
    agent = get_agent(str(repo_p))
    try:
        dg = DependencyGraph(str(repo_p))
        dg.build()
        rel = req.file_path.replace("\\", "/")
        impact = dg.get_impact_radius(rel)
        return {"status": "success", "file": rel, "impact": impact}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/settings")
async def get_settings():
    return {
        "status": "success",
        "settings": {
            "gemini_model": config.GEMINI_PRO,
            "groq_model": config.GROQ_MODEL,
            "gemini_flash_model": config.GEMINI_FLASH,
            "max_review_iterations": config.MAX_REVIEW_ITERATIONS,
            "max_heal_attempts": config.MAX_HEAL_ATTEMPTS,
            "complexity_threshold": config.COMPLEXITY_THRESHOLD,
            "function_length_threshold": config.FUNCTION_LENGTH_THRESHOLD,
            "nesting_depth_threshold": config.NESTING_DEPTH_THRESHOLD,
            "gemini_configured": bool(config.GEMINI_API_KEY and config.GEMINI_API_KEY.strip()),
            "groq_configured": bool(config.GROQ_API_KEY and config.GROQ_API_KEY.strip()),
            "db_path": str(config.DB_PATH),
            "data_dir": str(config.DATA_DIR),
        }
    }

@app.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    if req.gemini_model:
        config.GEMINI_PRO = req.gemini_model
    if req.groq_model:
        config.GROQ_MODEL = req.groq_model
    if req.max_review_iterations is not None and req.max_review_iterations > 0:
        config.MAX_REVIEW_ITERATIONS = req.max_review_iterations
    if req.max_heal_attempts is not None and req.max_heal_attempts > 0:
        config.MAX_HEAL_ATTEMPTS = req.max_heal_attempts
    if req.complexity_threshold is not None and req.complexity_threshold > 0:
        config.COMPLEXITY_THRESHOLD = req.complexity_threshold
    if req.function_length_threshold is not None and req.function_length_threshold > 0:
        config.FUNCTION_LENGTH_THRESHOLD = req.function_length_threshold
    if req.nesting_depth_threshold is not None and req.nesting_depth_threshold > 0:
        config.NESTING_DEPTH_THRESHOLD = req.nesting_depth_threshold
    if req.gemini_api_key:
        config.GEMINI_API_KEY = req.gemini_api_key.strip()
    if req.groq_api_key:
        config.GROQ_API_KEY = req.groq_api_key.strip()
    if req.github_token:
        config.GITHUB_TOKEN = req.github_token.strip()
        os.environ["GITHUB_TOKEN"] = req.github_token.strip()
    return {"status": "success", "detail": "Settings updated successfully."}

# Serve static dashboard files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
async def get_index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    else:
        raise HTTPException(status_code=404, detail="Frontend dashboard index.html not found.")

if __name__ == "__main__":
    import uvicorn
    # Launch uvicorn server on port 8000
    uvicorn.run(app, host="127.0.0.1", port=8000)
