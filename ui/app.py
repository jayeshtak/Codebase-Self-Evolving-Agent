import sys
import json
import time
from pathlib import Path
from datetime import datetime

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Codebase Self-Evolving Agent",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state defaults ────────────────────────────────────────────────────

def init_state():
    defaults = {
        "agent": None,
        "repo_path": "",
        "scan_results": None,
        "runtime_scan_results": None,
        "plan": None,
        "active_fix": None,
        "fix_history": [],
        "log": [],
        "auto_fix_running": False,
        "auto_fix_progress": [],
        "confidence_threshold": 0.85,
        "max_auto_fix": 5,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def pick_folder() -> str:
    """Open a native OS folder picker and return the selected path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Select Repository Folder")
        root.destroy()
        return folder or ""
    except Exception as e:
        st.warning(f"Native folder browser not supported: {e}. Please type the path in the text box below.")
        return ""

init_state()


# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log.append(f"[{ts}] {msg}")

def severity_badge(severity: str) -> str:
    colors = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    return colors.get(severity, "⚪")

def _render_diff(unified_diff: str, filename: str = ""):
    """Render a unified diff as a visual side-by-side HTML table."""
    import difflib, html as html_mod

    # Parse unified diff back into before/after lines
    before_lines, after_lines = [], []
    for line in unified_diff.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            before_lines.append(line[1:])
        elif line.startswith("+"):
            after_lines.append(line[1:])
        else:
            before_lines.append(line[1:] if line.startswith(" ") else line)
            after_lines.append(line[1:] if line.startswith(" ") else line)

    # Build colour-coded HTML table
    rows = []
    max_len = max(len(before_lines), len(after_lines))
    for i in range(max_len):
        b = before_lines[i] if i < len(before_lines) else ""
        a = after_lines[i]  if i < len(after_lines)  else ""
        b_esc = html_mod.escape(b)
        a_esc = html_mod.escape(a)
        b_bg = "#ffecec" if b != a else "#f8f8f8"
        a_bg = "#eaffea" if b != a else "#f8f8f8"
        rows.append(
            f'<tr>'
            f'<td style="background:{b_bg};padding:2px 6px;font-family:monospace;'
            f'font-size:12px;white-space:pre;border-bottom:1px solid #ddd">{b_esc}</td>'
            f'<td style="background:{a_bg};padding:2px 6px;font-family:monospace;'
            f'font-size:12px;white-space:pre;border-bottom:1px solid #ddd">{a_esc}</td>'
            f'</tr>'
        )

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid #ccc;border-radius:6px">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr>
            <th style="background:#e0e0e0;padding:4px 8px;text-align:left;font-size:12px">
              Before — {html_mod.escape(filename)}</th>
            <th style="background:#e0e0e0;padding:4px 8px;text-align:left;font-size:12px">
              After — {html_mod.escape(filename)}</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>"""

    import streamlit.components.v1 as components
    height = min(600, max(150, len(rows) * 20 + 60))
    components.html(table_html, height=height, scrolling=True)


def type_icon(issue_type: str) -> str:
    icons = {
        "security": "🔒", "syntax_error": "💥", "complexity": "🌀",
        "dead_code": "👻", "style": "✏️", "duplicate": "📋",
        "bug": "🐛", "runtime_error": "⚡",
    }
    return icons.get(issue_type, "⚠️")

def load_agent(repo_path: str):
    try:
        import config
        config.validate()
        from main import CodebaseAgent
        st.session_state.agent = CodebaseAgent(repo_path)
        st.session_state.repo_path = repo_path
        log(f"Agent initialized for: {repo_path}")
        return True
    except EnvironmentError as e:
        st.error(f"API keys missing: {e}")
        return False
    except Exception as e:
        st.error(f"Failed to initialize agent: {e}")
        return False

def load_last_scan(repo_path: str) -> dict:
    """Load last scan results from JSON without running agent."""
    scan_file = Path(repo_path) / "data" / "last_scan.json"
    if scan_file.exists():
        return json.loads(scan_file.read_text(encoding="utf-8"))
    return {}

def load_last_plan(repo_path: str) -> dict:
    plan_file = Path(repo_path) / "data" / "last_plan.json"
    if plan_file.exists():
        return json.loads(plan_file.read_text(encoding="utf-8"))
    return {}


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🧬 Codebase Agent")
    st.divider()

    # Initialise the text input key once
    if "repo_input_box" not in st.session_state:
        st.session_state.repo_input_box = (
            st.session_state.repo_path
            or str(Path(__file__).parent.parent.resolve())
        )

    # Folder picker row
    pick_col, _ = st.columns([1, 3])
    with pick_col:
        if st.button("📁 Browse", width="stretch"):
            picked = pick_folder()
            if picked:
                st.session_state.repo_input_box = picked   # same key as text_input
                st.session_state.repo_path = picked
                st.rerun()

    repo_input = st.text_input(
        "Repository Path",
        placeholder="/path/to/your/repo",
        key="repo_input_box",
    )
    # Keep repo_path in sync with whatever is typed
    st.session_state.repo_path = repo_input

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔌 Connect", width="stretch"):
            if repo_input and Path(repo_input).exists():
                with st.spinner("Connecting..."):
                    if load_agent(repo_input):
                        st.success("Connected!")
                        existing = load_last_scan(repo_input)
                        if existing:
                            st.session_state.scan_results = existing
            else:
                st.error("Invalid path.")

    with col2:
        if st.button("📂 Load", width="stretch", help="Load last scan without connecting"):
            if repo_input and Path(repo_input).exists():
                st.session_state.repo_path = repo_input
                st.session_state.scan_results = load_last_scan(repo_input)
                if st.session_state.scan_results:
                    st.success(f"Loaded {st.session_state.scan_results.get('total_issues', 0)} issues")
                else:
                    st.warning("No previous scan found.")

    st.divider()

    # Agent status
    if st.session_state.agent:
        st.success("✅ Agent connected")
        stats = st.session_state.agent.stats()
        st.metric("Total scans", stats.get("total_scans", 0))
        st.metric("Issues fixed", stats.get("fixed", 0))
        st.metric("Issues rejected", stats.get("rejected", 0))
    else:
        st.warning("⚠️ Agent not connected")
        st.caption("Connect to enable fix generation.")

    st.divider()

    # Quick actions
    st.subheader("Quick Actions")

    if st.button("🔍 Run Scan", width="stretch",
                  disabled=st.session_state.agent is None):
        with st.spinner("Scanning..."):
            try:
                results = st.session_state.agent.scan(full_scan=False)
                st.session_state.scan_results = results
                log(f"Scan complete: {results['total_issues']} issues found.")
                st.success(f"Found {results['total_issues']} issues!")
                st.rerun()
            except Exception as e:
                st.error(f"Scan failed: {e}")

    st.divider()
    st.subheader("Extra Scans")

    if st.button("⚡ Profile Scan", width="stretch",
                  disabled=st.session_state.agent is None,
                  help="Run cProfile/node --prof to find slow functions"):
        with st.spinner("Profiling... (runs your app for ~5s)"):
            try:
                prof_results = st.session_state.agent.profile_scan()
                n = prof_results.get("total_issues", 0)
                # Merge into scan results so they show in Static tab
                if n and st.session_state.scan_results:
                    st.session_state.scan_results["issues"] = (
                        st.session_state.scan_results.get("issues", [])
                        + prof_results.get("issues", [])
                    )
                    st.session_state.scan_results["total_issues"] = len(
                        st.session_state.scan_results["issues"]
                    )
                log(f"Profile scan: {n} hotspot(s).")
                st.success(f"Found {n} performance hotspot(s).") if n else st.success("No hotspots found.")
                st.rerun()
            except Exception as e:
                st.error(f"Profile scan failed: {e}")

    if st.button("📝 Doc Scan & Write", width="stretch",
                  disabled=st.session_state.agent is None,
                  help="Find undocumented functions and auto-generate docstrings"):
        with st.spinner("Scanning for missing docs..."):
            try:
                doc_results = st.session_state.agent.doc_scan()
                n = doc_results.get("total_issues", 0)
                if n and st.session_state.scan_results:
                    st.session_state.scan_results["issues"] = (
                        st.session_state.scan_results.get("issues", [])
                        + doc_results.get("issues", [])
                    )
                    st.session_state.scan_results["total_issues"] = len(
                        st.session_state.scan_results["issues"]
                    )
                log(f"Doc scan: {n} undocumented function(s).")
                st.success(f"Found {n} undocumented function(s).") if n else st.success("All functions documented!")
                st.rerun()
            except Exception as e:
                st.error(f"Doc scan failed: {e}")

    st.divider()
    st.subheader("Runtime Testing")
    runtime_cmd = st.text_input(
        "Run command (optional)",
        placeholder="e.g. python app.py  or  uvicorn main:app",
        help="Leave blank to auto-detect framework and run command.",
        key="runtime_run_command",
    )
    if st.button("🚀 Runtime Scan", width="stretch",
                  disabled=st.session_state.agent is None,
                  help="Run the project and catch runtime errors"):
        with st.spinner("Starting project and testing routes..."):
            try:
                rt_results = st.session_state.agent.runtime_scan(run_command=runtime_cmd or "")
                st.session_state.runtime_scan_results = rt_results
                n = rt_results.get("total_issues", 0)
                log(f"Runtime scan complete: {n} runtime issue(s) found.")
                if n:
                    st.warning(f"Found {n} runtime issue(s)!")
                else:
                    st.success("No runtime errors detected.")
                st.rerun()
            except Exception as e:
                st.error(f"Runtime scan failed: {e}")

    if st.button("🗺️ Generate Plan", width="stretch",
                  disabled=(st.session_state.agent is None or
                             st.session_state.scan_results is None)):
        with st.spinner("Planning..."):
            try:
                plan = st.session_state.agent.plan(st.session_state.scan_results)
                st.session_state.plan = plan
                log(f"Plan generated: {len(plan.actionable())} actionable items.")
                st.success("Plan ready!")
                st.rerun()
            except Exception as e:
                st.error(f"Plan failed: {e}")

    st.divider()

    # Auto-Fix All
    st.subheader("Auto Mode")

    st.session_state.confidence_threshold = st.slider(
        "Auto-apply confidence threshold",
        min_value=0.5, max_value=1.0,
        value=st.session_state.confidence_threshold,
        step=0.05,
        help="Fixes with reviewer confidence ≥ this value are applied automatically. Lower = more automation, higher = more caution.",
    )

    st.session_state.max_auto_fix = st.number_input(
        "Max issues to fix per run",
        min_value=1, max_value=50,
        value=st.session_state.max_auto_fix,
        step=1,
        help="Caps how many issues are sent to the LLM in one auto-fix run. Keep low (≤5) to avoid API rate limits.",
    )

    plan_obj = st.session_state.plan
    n_actionable = len(plan_obj.actionable()) if plan_obj else 0
    rt_issues = (st.session_state.runtime_scan_results or {}).get("issues", [])
    n_runtime = len(rt_issues)

    def _run_auto_fix(issues: list, scan_id: str, progress_key: str):
        """
        Parallel auto-fix loop.
        - Fixes for different files run concurrently (ThreadPoolExecutor).
        - Fixes for the same file are serialised to avoid write conflicts.
        - Fixes with confidence >= threshold are auto-applied; others wait for approval.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from agents.coder import QuotaExhaustedException

        threshold = st.session_state.confidence_threshold
        cap = int(st.session_state.max_auto_fix)
        issues = issues[:cap]  # hard cap — never send more than max_auto_fix to the LLM
        agent = st.session_state.agent

        st.session_state.auto_fix_progress = []
        progress_bar = st.progress(0, text="Starting parallel auto-fix...")
        results_lock = threading.Lock()
        file_locks: dict[str, threading.Lock] = {}
        file_locks_lock = threading.Lock()
        progress_results: list[dict] = []
        quota_exhausted_flag = threading.Event()
        total = len(issues)

        def get_file_lock(filepath: str) -> threading.Lock:
            with file_locks_lock:
                if filepath not in file_locks:
                    file_locks[filepath] = threading.Lock()
                return file_locks[filepath]

        def fix_one(item) -> dict:
            if quota_exhausted_flag.is_set():
                issue = item if isinstance(item, dict) else item.__dict__
                label = f"{issue.get('file')}:{issue.get('line')} [{issue.get('issue_type')}]"
                return {"label": label, "status": "skipped", "explanation": "quota exhausted"}

            issue = item if isinstance(item, dict) else item.__dict__
            label = f"{issue.get('file')}:{issue.get('line')} [{issue.get('issue_type')}]"
            filepath = issue.get("file", "")

            # Serialise writes to the same file
            with get_file_lock(filepath):
                try:
                    fix_result = agent.fix_issue(issue, scan_id)
                    status = fix_result.get("status")
                    confidence = fix_result.get("confidence", 0.0)

                    if status == "ready_for_approval":
                        if confidence >= threshold:
                            success = agent.apply_fix(
                                fix_branch=fix_result.get("fix_branch"),
                                fix_id=fix_result.get("fix_id", 0),
                                issue_id=issue.get("id", 0),
                            )
                            return {"label": label,
                                    "status": "applied" if success else "merge_failed",
                                    "explanation": fix_result.get("explanation", ""),
                                    "confidence": confidence,
                                    "auto_applied": True}
                        else:
                            # Below threshold — leave for manual review
                            return {"label": label, "status": "pending_review",
                                    "explanation": fix_result.get("explanation", ""),
                                    "confidence": confidence,
                                    "fix_result": fix_result, "auto_applied": False}
                    else:
                        err = fix_result.get("error", "")
                        if "QuotaExhausted" in err or ("Both Gemini" in err and "Groq" in err):
                            quota_exhausted_flag.set()
                            return {"label": label, "status": "quota_exhausted", "explanation": err}
                        return {"label": label, "status": status,
                                "explanation": err, "confidence": confidence}

                except QuotaExhaustedException as e:
                    quota_exhausted_flag.set()
                    return {"label": label, "status": "quota_exhausted", "explanation": str(e)}
                except Exception as e:
                    return {"label": label, "status": "error", "explanation": str(e)}

        completed = 0
        # Use up to 4 workers — enough parallelism without hammering the API
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fix_one, item): item for item in issues}
            for future in as_completed(futures):
                entry = future.result()
                completed += 1
                with results_lock:
                    progress_results.append(entry)
                progress_bar.progress(
                    completed / total,
                    text=f"[{completed}/{total}] {entry['label']} → {entry['status']}"
                )
                log(f"Auto-fix {entry['status']}: {entry['label']}")

        st.session_state.auto_fix_progress = progress_results
        if quota_exhausted_flag.is_set():
            progress_bar.progress(1.0, text="⚠️ Stopped — API quotas exhausted.")
            st.warning("Both Gemini and Groq daily quotas exhausted. Remaining issues skipped.")
        else:
            n_applied = sum(1 for e in progress_results if e["status"] == "applied")
            n_review  = sum(1 for e in progress_results if e["status"] == "pending_review")
            progress_bar.progress(1.0, text=f"Done — {n_applied} applied, {n_review} awaiting review.")
        st.rerun()

    if st.button(
        f"⚡ Auto-Fix All ({n_actionable} static issues)",
        width="stretch",
        disabled=(st.session_state.agent is None or plan_obj is None or n_actionable == 0),
        type="primary",
    ):
        scan_id = (st.session_state.scan_results or {}).get("scan_id", "manual")
        cap = int(st.session_state.max_auto_fix)
        items = [i if isinstance(i, dict) else i.__dict__ for i in plan_obj.actionable()][:cap]
        if len(plan_obj.actionable()) > cap:
            st.info(f"Capped at {cap} issues (of {len(plan_obj.actionable())} total). Raise 'Max issues' to fix more.")
        _run_auto_fix(items, scan_id, "static")

    if st.button(
        f"🚀 Auto-Fix Runtime ({n_runtime} issues)",
        width="stretch",
        disabled=(st.session_state.agent is None or n_runtime == 0),
        type="primary",
    ):
        scan_id = (st.session_state.runtime_scan_results or {}).get("scan_id", "runtime")
        cap = int(st.session_state.max_auto_fix)
        capped_rt = rt_issues[:cap]
        if len(rt_issues) > cap:
            st.info(f"Capped at {cap} issues (of {len(rt_issues)} total). Raise 'Max issues' to fix more.")
        _run_auto_fix(capped_rt, scan_id, "runtime")

    # Show auto-fix results
    if st.session_state.auto_fix_progress:
        st.divider()
        for idx, entry in enumerate(st.session_state.auto_fix_progress):
            icon = {"applied": "✅", "tests_failed": "❌", "failed": "❌",
                    "merge_failed": "⚠️", "pending_review": "🔍",
                    "error": "💥", "quota_exhausted": "🚫"}.get(entry["status"], "⚪")
            conf = entry.get("confidence", 0.0)
            st.caption(f"{icon} **{entry['label']}** — {entry['status']} (conf: {conf:.2f})")
            if entry["status"] == "pending_review" and entry.get("fix_result"):
                fr = entry["fix_result"]
                pr_col1, pr_col2 = st.columns(2)
                with pr_col1:
                    if st.button("✅ Apply", key=f"pr_apply_{idx}", width="stretch", type="primary"):
                        success = st.session_state.agent.apply_fix(
                            fix_branch=fr.get("fix_branch"),
                            fix_id=fr.get("fix_id", 0),
                            issue_id=fr.get("issue", {}).get("id", 0),
                        )
                        entry["status"] = "applied" if success else "merge_failed"
                        st.rerun()
                with pr_col2:
                    if st.button("❌ Reject", key=f"pr_reject_{idx}", width="stretch"):
                        st.session_state.agent.reject_fix(
                            fix_id=fr.get("fix_id", 0),
                            fix_branch=fr.get("fix_branch", ""),
                            issue=fr.get("issue", {}),
                        )
                        entry["status"] = "rejected"
                        st.rerun()

    st.divider()

    # Log
    if st.session_state.log:
        st.subheader("Activity Log")
        for entry in reversed(st.session_state.log[-10:]):
            st.caption(entry)


# ── Main area ─────────────────────────────────────────────────────────────────

tab_scan, tab_runtime, tab_plan, tab_fix, tab_history, tab_stats = st.tabs([
    "🔍 Scan",
    "🚀 Runtime",
    "🗺️ Plan",
    "🔧 Fix",
    "📋 History",
    "📊 Stats & Schedule",
])


# ── Tab 1: Scan Results ───────────────────────────────────────────────────────

with tab_scan:
    st.header("Scan Results")

    # ── Natural language issue search ─────────────────────────────────────────
    nl_query = st.text_input(
        "🔍 Search issues in plain English",
        placeholder="e.g. security issues in auth files  |  unused code in scanner  |  complex functions",
        key="nl_search",
    )
    if nl_query and st.session_state.agent:
        with st.spinner("Searching..."):
            try:
                hits = st.session_state.agent.rag.search(nl_query, n_results=8)
                if hits:
                    st.markdown(f"**{len(hits)} relevant code section(s) found:**")
                    for hit in hits:
                        meta = hit.get("metadata", {})
                        score = hit.get("distance", 1.0)
                        relevance = max(0, round((1 - score) * 100))
                        with st.expander(
                            f"📄 `{meta.get('file','?')}` lines {meta.get('start_line','?')}–{meta.get('end_line','?')} "
                            f"— {relevance}% relevant"
                        ):
                            st.code(hit.get("text", "")[:600], language="python")
                else:
                    st.info("No matching code sections found. Try a different query.")
            except Exception as e:
                st.warning(f"Search unavailable: {e}")
    elif nl_query and not st.session_state.agent:
        st.warning("Connect to a repository first to enable search.")

    st.divider()

    if not st.session_state.scan_results:
        st.info("No scan results yet. Run a scan or load a previous one from the sidebar.")
    else:
        results = st.session_state.scan_results
        issues = results.get("issues", [])

        # Summary row
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Issues", results.get("total_issues", 0))
        col2.metric("High", sum(1 for i in issues if i.get("severity") == "high"))
        col3.metric("Medium", sum(1 for i in issues if i.get("severity") == "medium"))
        col4.metric("Low", sum(1 for i in issues if i.get("severity") == "low"))

        st.caption(f"Scan ID: {results.get('scan_id')} | "
                   f"Started: {results.get('started_at')} | "
                   f"Finished: {results.get('finished_at')}")

        st.divider()

        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            filter_severity = st.multiselect(
                "Severity", ["high", "medium", "low"],
                default=["high", "medium", "low"]
            )
        with col2:
            filter_type = st.multiselect(
                "Issue Type",
                list(set(i.get("issue_type", "") for i in issues)),
                default=list(set(i.get("issue_type", "") for i in issues))
            )
        with col3:
            filter_file = st.text_input("Filter by file", placeholder="e.g. scanner")

        # Filter issues
        filtered = [
            i for i in issues
            if i.get("severity") in filter_severity
            and i.get("issue_type") in filter_type
            and (not filter_file or filter_file.lower() in i.get("file", "").lower())
        ]

        st.caption(f"Showing {len(filtered)} of {len(issues)} issues")

        # Issue list
        for i, issue in enumerate(filtered):
            itype = issue.get('issue_type', '')
            with st.expander(
                f"{severity_badge(issue.get('severity', 'low'))} "
                f"{type_icon(itype)} [{itype.upper()}] "
                f"{issue.get('file', '')}:{issue.get('line', '')} — "
                f"{issue.get('description', '')[:60]}"
            ):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"**File:** `{issue.get('file')}`")
                    st.write(f"**Line:** {issue.get('line')}")
                    st.write(f"**Type:** {issue.get('issue_type')}")
                    st.write(f"**Severity:** {issue.get('severity')}")
                    st.write(f"**Description:** {issue.get('description')}")
                    if issue.get("extra"):
                        st.write(f"**Extra:** {issue.get('extra')}")
                with col2:
                    if st.button("Fix this →", key=f"fix_{issue.get('file')}_{issue.get('line')}_{i}",
                                  disabled=st.session_state.agent is None):
                        st.session_state.active_fix = issue
                        log(f"Fix queued: {issue.get('file')}:{issue.get('line')}")
                        st.rerun()


# ── Tab 2: Runtime Scan ───────────────────────────────────────────────────────

with tab_runtime:
    st.header("Runtime Scan Results")

    rt = st.session_state.runtime_scan_results

    # Load from disk if not in session
    if not rt and st.session_state.repo_path:
        rt_file = Path(st.session_state.repo_path) / "data" / "last_runtime_scan.json"
        if rt_file.exists():
            rt = json.loads(rt_file.read_text(encoding="utf-8"))
            st.caption("Showing last saved runtime scan.")

    if not rt:
        st.info("No runtime scan yet. Click **🚀 Runtime Scan** in the sidebar.")
    else:
        issues = rt.get("issues", [])

        # Summary row
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Runtime Issues", rt.get("total_issues", 0))
        col2.metric("Framework", rt.get("framework", "unknown"))
        col3.metric("Routes Tested", len(rt.get("routes_tested", [])))
        col4.metric("Server Started", "✅" if rt.get("server_started") else "❌")

        st.caption(f"Scan ID: {rt.get('scan_id')} | "
                   f"Command: `{rt.get('run_command', 'N/A')}`")

        if rt.get("errors"):
            st.warning("Scan errors: " + " | ".join(rt["errors"]))

        if rt.get("routes_tested"):
            with st.expander("Routes tested"):
                for r in rt["routes_tested"]:
                    st.caption(f"• {r}")

        st.divider()

        if not issues:
            st.success("✅ No runtime errors detected — project ran cleanly.")
        else:
            st.subheader(f"{len(issues)} Runtime Issue(s)")
            for i, issue in enumerate(issues):
                extra = issue.get("extra", {})
                error_class = extra.get("error_class", issue.get("issue_type", "error"))
                with st.expander(
                    f"🔴 [{error_class}] "
                    f"{issue.get('file', 'unknown')}:{issue.get('line', '?')} — "
                    f"{issue.get('description', '')[:70]}"
                ):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.write(f"**File:** `{issue.get('file')}`")
                        st.write(f"**Line:** {issue.get('line')}")
                        st.write(f"**Error:** {issue.get('description')}")
                        if extra.get("traceback"):
                            st.code(extra["traceback"], language="text")
                    with col2:
                        if st.button("Fix this →",
                                     key=f"rt_fix_{i}",
                                     disabled=st.session_state.agent is None):
                            st.session_state.active_fix = issue
                            log(f"Runtime fix queued: {issue.get('file')}:{issue.get('line')}")
                            st.rerun()


# ── Tab 3: Fix Plan ───────────────────────────────────────────────────────────

with tab_plan:
    st.header("Fix Plan")

    plan = st.session_state.plan
    if not plan:
        # Try loading from disk
        if st.session_state.repo_path:
            plan_data = load_last_plan(st.session_state.repo_path)
            if plan_data:
                st.info("Showing last saved plan.")
                items = plan_data.get("items", [])

                st.write(f"**Summary:** {plan_data.get('summary', '')}")
                col1, col2, col3 = st.columns(3)
                col1.metric("Actionable", plan_data.get("actionable", 0))
                col2.metric("Skipped", plan_data.get("skipped", 0))
                col3.metric("Total", plan_data.get("total_issues", 0))

                st.divider()
                for i, item in enumerate([x for x in items if not x.get("skipped")], 1):
                    with st.expander(
                        f"#{i} [{item.get('priority_score')}] "
                        f"{severity_badge(item.get('severity', 'low'))} "
                        f"{item.get('file')}:{item.get('line')} — "
                        f"{item.get('description', '')[:50]}"
                    ):
                        st.write(f"**Strategy:** {item.get('fix_strategy')}")
                        st.write(f"**Risk:** {item.get('risk_level')} "
                                 f"(impact={item.get('impact_radius')})")
                        if st.button("Fix this →",
                                     key=f"plan_fix_{item.get('file')}_{item.get('line')}_{i}",
                                     disabled=st.session_state.agent is None):
                            st.session_state.active_fix = item
                            st.rerun()
            else:
                st.info("No plan yet. Generate a plan from the sidebar.")
        else:
            st.info("Connect to a repository first.")
    else:
        st.write(f"**Summary:** {plan.summary}")
        col1, col2, col3 = st.columns(3)
        col1.metric("Actionable", len(plan.actionable()))
        col2.metric("Skipped", len(plan.skipped))
        col3.metric("Total", plan.total_issues)

        st.divider()
        st.subheader("Actionable Items")
        for i, item in enumerate(plan.actionable(), 1):
            with st.expander(
                f"#{i} [Score: {item.priority_score}] "
                f"{severity_badge(item.severity)} "
                f"{item.file}:{item.line} — {item.description[:50]}"
            ):
                st.write(f"**Strategy:** {item.fix_strategy}")
                st.write(f"**Risk:** {item.risk_level} (impact={item.impact_radius})")
                if st.button("Fix this →",
                             key=f"planobj_fix_{item.file}_{item.line}_{i}",
                             disabled=st.session_state.agent is None):
                    st.session_state.active_fix = item if isinstance(item, dict) else item.__dict__
                    st.rerun()

        if plan.skipped:
            st.divider()
            st.subheader("Skipped (previously rejected)")
            for item in plan.skipped:
                st.caption(f"⏭️ {item.file}:{item.line} — {item.skip_reason}")


# ── Tab 3: Fix Console ────────────────────────────────────────────────────────

with tab_fix:
    st.header("Fix Console")

    active = st.session_state.active_fix
    if not active:
        st.info("Select an issue to fix from the Scan Results or Fix Plan tab.")
    else:
        issue = active if isinstance(active, dict) else active.__dict__

        st.subheader("Selected Issue")
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"**File:** `{issue.get('file')}`")
            st.write(f"**Line:** {issue.get('line')}")
            st.write(f"**Description:** {issue.get('description')}")
        with col2:
            if st.button("❌ Clear", width="stretch"):
                st.session_state.active_fix = None
                st.rerun()

        st.divider()

        # ── Confirmation 1: Approve fix generation ────────────────
        st.subheader("Step 1 — Approve Fix Generation")
        st.warning("⚠️ The agent will generate a fix for this issue. "
                   "No code will be changed yet.")

        if st.button("✅ Generate Fix", width="stretch",
                      disabled=st.session_state.agent is None,
                      type="primary"):
            with st.spinner("Running Coder → Reviewer loop... this may take a minute."):
                try:
                    scan_id = (st.session_state.scan_results or {}).get("scan_id", "manual")
                    fix_result = st.session_state.agent.fix_issue(issue, scan_id)
                    st.session_state.fix_result = fix_result
                    log(f"Fix generated: {fix_result.get('status')} for {issue.get('file')}")
                except Exception as e:
                    st.error(f"Fix generation failed: {e}")

        # ── Show fix result ───────────────────────────────────────
        if hasattr(st.session_state, "fix_result") and st.session_state.fix_result:
            fix = st.session_state.fix_result
            status = fix.get("status", "failed")

            st.divider()
            st.subheader("Step 2 — Review Fix")

            if status == "failed":
                st.error(f"Fix failed: {fix.get('error')}")

            elif status == "tests_failed":
                st.error(f"Tests failed after self-healing: {fix.get('error', '')[:200]}")
                st.write(f"**Branch:** `{fix.get('fix_branch')}`")

            elif status == "ready_for_approval":
                st.success("✅ Fix generated and tests passed!")
                st.write(f"**Branch:** `{fix.get('fix_branch')}`")
                st.write(f"**Explanation:** {fix.get('explanation')}")

                # Show diff — visual side-by-side
                if fix.get("diff"):
                    st.subheader("Diff")
                    _render_diff(fix.get("diff", ""),
                                 fix.get("issue", {}).get("file", ""))

                # Show rename propagation info
                renames = fix.get("renames", [])
                if renames:
                    rename_str = ", ".join(f"`{r['old']}` → `{r['new']}`" for r in renames)
                    updated = fix.get("rename_files_updated", [])
                    st.info(
                        f"🔁 **Rename propagated** — {rename_str}  \n"
                        f"Updated **{fix.get('rename_replacements', 0)}** reference(s) "
                        f"across **{len(updated)}** file(s): "
                        + (", ".join(f"`{f}`" for f in updated) if updated else "none")
                    )

                # Show cross-file diffs
                cross = fix.get("cross_file_diffs", [])
                if cross:
                    with st.expander(f"🔗 Cross-file changes ({len(cross)} file(s))"):
                        for cf in cross:
                            st.caption(cf["file"])
                            _render_diff(cf["diff"], cf["file"])

                # Show test result
                test_res = fix.get("test_result", {})
                if test_res:
                    st.subheader("Test Results")
                    if test_res.get("passed"):
                        st.success(f"✅ {test_res.get('summary', 'Tests passed')}")
                    else:
                        st.error(f"❌ {test_res.get('summary', 'Tests failed')}")

                st.divider()

                # ── Confirmation 2: Approve the fix ──────────────
                st.subheader("Step 3 — Apply Fix to Codebase")
                st.warning(f"⚠️ This will merge the fix branch into {st.session_state.agent.git.default_branch}. Cannot be undone.")

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ Apply Fix", width="stretch", type="primary"):
                        with st.spinner("Merging..."):
                            success = st.session_state.agent.apply_fix(
                                fix_branch=fix.get("fix_branch"),
                                fix_id=fix.get("fix_id", 0),
                                issue_id=issue.get("id", 0),
                            )
                            if success:
                                st.success(f"✅ Fix applied and merged to {st.session_state.agent.git.default_branch}!")
                                st.session_state.fix_result = None
                                st.session_state.active_fix = None
                                log(f"Fix applied: {issue.get('file')}")
                                st.session_state.fix_history.append({
                                    "file": issue.get("file"),
                                    "description": issue.get("description"),
                                    "status": "applied",
                                    "time": datetime.now().strftime("%H:%M:%S"),
                                })
                                st.rerun()
                            else:
                                st.error("Merge failed.")

                with col2:
                    reject_reason = st.text_input("Rejection reason (optional)")
                    if st.button("❌ Reject Fix", width="stretch"):
                        st.session_state.agent.reject_fix(
                            issue=issue,
                            fix_branch=fix.get("fix_branch"),
                            fix_id=fix.get("fix_id", 0),
                            reason=reject_reason,
                        )
                        st.warning("Fix rejected and logged to memory.")
                        st.session_state.fix_result = None
                        st.session_state.active_fix = None
                        log(f"Fix rejected: {issue.get('file')} — {reject_reason}")
                        st.session_state.fix_history.append({
                            "file": issue.get("file"),
                            "description": issue.get("description"),
                            "status": "rejected",
                            "time": datetime.now().strftime("%H:%M:%S"),
                        })
                        st.rerun()


# ── Tab 4: History ────────────────────────────────────────────────────────────

with tab_history:
    st.header("History")

    if st.session_state.agent:
        from memory.sqlite import SQLiteMemory
        mem = SQLiteMemory()
        stats = mem.get_stats()
        rejections = mem.get_rejections()
        all_scans = mem.get_all_scans()

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Scans", stats.get("total_scans", 0))
        col2.metric("Total Fixed", stats.get("fixed", 0))
        col3.metric("Total Rejected", stats.get("rejected", 0))

        # ── Past scans ────────────────────────────────────────────
        if all_scans:
            st.divider()
            st.subheader("Scan History")
            for scan in all_scans:
                status_icon = "✅" if scan.get("status") == "done" else "🔄"
                with st.expander(
                    f"{status_icon} Scan `{scan['id']}` — "
                    f"{scan.get('total_issues', 0)} issues — "
                    f"{scan.get('started_at', '')[:16]}"
                ):
                    st.write(f"**Repo:** `{scan.get('repo_path', '')}`")
                    st.write(f"**Started:** {scan.get('started_at', '')}")
                    st.write(f"**Finished:** {scan.get('finished_at', 'N/A')}")
                    st.write(f"**Issues found:** {scan.get('total_issues', 0)}")
                    if st.button("Load issues from this scan",
                                 key=f"load_scan_{scan['id']}",
                                 disabled=st.session_state.agent is None):
                        scan_issues = mem.get_issues_for_scan(scan["id"])
                        st.session_state.scan_results = {
                            "scan_id": scan["id"],
                            "total_issues": len(scan_issues),
                            "issues": scan_issues,
                            "started_at": scan.get("started_at"),
                            "finished_at": scan.get("finished_at"),
                        }
                        log(f"Loaded {len(scan_issues)} issues from scan {scan['id']}")
                        st.rerun()

        # ── Applied fixes (with rollback) ────────────────────────
        applied_fixes = mem.get_applied_fixes(limit=20)
        if applied_fixes:
            st.divider()
            st.subheader("Applied Fixes")
            st.caption("Click 'Rollback' to restore the original code for any applied fix.")
            for fx in applied_fixes:
                fx_id = fx.get("id")
                with st.expander(
                    f"✅ Fix #{fx_id} — `{fx.get('file', '')}` — "
                    f"{fx.get('fix_description', '')[:50]} — "
                    f"{fx.get('created_at', '')[:16]}"
                ):
                    col_a, col_b = st.columns([3, 1])
                    with col_a:
                        st.write(f"**File:** `{fx.get('file')}`")
                        st.write(f"**Description:** {fx.get('fix_description', '')}")
                        st.write(f"**Test result:** {fx.get('test_result', 'N/A')}")
                        st.write(f"**Applied at:** {fx.get('created_at', '')}")
                    with col_b:
                        if st.button("↩️ Rollback", key=f"rollback_{fx_id}",
                                     disabled=st.session_state.agent is None,
                                     width="stretch"):
                            with st.spinner("Rolling back..."):
                                success = st.session_state.agent.rollback_fix(fx_id)
                                if success:
                                    st.success(f"Fix #{fx_id} rolled back.")
                                    log(f"Rolled back fix #{fx_id}: {fx.get('file')}")
                                    st.rerun()
                                else:
                                    st.error("Rollback failed.")

        # ── Rejection memory ──────────────────────────────────────
        if rejections:
            st.divider()
            st.subheader("Rejection Memory")
            st.caption("These issue types will be skipped in future plans.")
            for r in rejections:
                st.write(f"- `{r.get('file')}` — {r.get('issue_type')}: "
                         f"{r.get('description', '')[:60]} "
                         f"*(reason: {r.get('reason', 'none')})*")

    # ── This session ──────────────────────────────────────────────
    if st.session_state.fix_history:
        st.divider()
        st.subheader("This Session")
        for entry in reversed(st.session_state.fix_history):
            icon = "✅" if entry["status"] == "applied" else "❌"
            st.write(f"{icon} `{entry['file']}` — {entry['description'][:50]} "
                     f"at {entry['time']}")
    else:
        if not st.session_state.agent:
            st.info("Connect to a repository to view history.")
        else:
            st.info("No fixes applied this session yet.")


# ── Tab 5: Stats & Schedule ───────────────────────────────────────────────────

with tab_stats:
    st.header("Stats & Schedule")

    if st.session_state.agent:
        stats = st.session_state.agent.stats()

        # ── Stats ─────────────────────────────────────────────────
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Scans", stats.get("total_scans", 0))
        col2.metric("Fixed", stats.get("fixed", 0))
        col3.metric("Rejected", stats.get("rejected", 0))
        col4.metric("Open Issues", stats.get("open", 0))

        from memory.sqlite import SQLiteMemory as _SQLiteMem
        _mem = _SQLiteMem()

        # ── Code Health Score ─────────────────────────────────────
        st.divider()
        st.subheader("🏥 Code Health Score")
        health_trend = _mem.get_health_trend(limit=30)
        latest_score = _mem.get_latest_health_score()

        if latest_score is not None:
            score_color = "#27ae60" if latest_score >= 70 else "#f39c12" if latest_score >= 40 else "#e74c3c"
            grade = "A" if latest_score >= 90 else "B" if latest_score >= 70 else "C" if latest_score >= 50 else "D" if latest_score >= 30 else "F"
            st.markdown(
                f'<div style="display:flex;align-items:center;gap:24px;margin:8px 0">'
                f'<div style="background:white;border-radius:12px;padding:20px 32px;box-shadow:0 2px 8px rgba(0,0,0,.08);text-align:center">'
                f'<div style="font-size:48px;font-weight:700;color:{score_color}">{latest_score:.0f}</div>'
                f'<div style="font-size:13px;color:#666;margin-top:4px">Health Score / 100</div>'
                f'</div>'
                f'<div style="background:white;border-radius:12px;padding:20px 32px;box-shadow:0 2px 8px rgba(0,0,0,.08);text-align:center">'
                f'<div style="font-size:48px;font-weight:700;color:{score_color}">{grade}</div>'
                f'<div style="font-size:13px;color:#666;margin-top:4px">Grade</div>'
                f'</div>'
                f'<div style="font-size:13px;color:#555;max-width:280px">'
                f'Score = 100 − (high×5 + medium×2 + low×0.5). '
                f'Improves automatically as fixes are applied.'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.info("Run a scan to generate your first health score.")

        if len(health_trend) >= 2:
            import pandas as pd
            hdf = pd.DataFrame(health_trend)
            hdf["recorded_at"] = hdf["recorded_at"].str[:16]
            hdf = hdf.set_index("recorded_at")
            st.caption("Health score trend")
            st.line_chart(hdf[["score"]], height=160)

        # ── Trends dashboard ──────────────────────────────────────
        trends = _mem.get_scan_trends(limit=30)

        if len(trends) >= 2:
            import pandas as pd
            st.divider()
            st.subheader("Trends Over Time")

            df = pd.DataFrame(trends)
            df = df.set_index("started_at")

            col_t1, col_t2 = st.columns(2)
            with col_t1:
                st.caption("Total issues per scan")
                st.line_chart(df[["total_issues"]], width="stretch", height=180)
            with col_t2:
                st.caption("Fixes applied per scan")
                st.bar_chart(df[["fixed"]], width="stretch", height=180)

            st.caption("Severity breakdown per scan")
            st.area_chart(df[["high", "medium", "low"]], width="stretch", height=180)

            if df[["security", "duplicate", "performance", "missing_docs"]].sum().sum() > 0:
                st.caption("Issue type breakdown per scan")
                st.bar_chart(
                    df[["security", "duplicate", "performance", "missing_docs"]],
                    width="stretch", height=180,
                )

            # Top 5 most problematic files across all scans
            st.divider()
            st.subheader("Most Problematic Files (all scans)")
            file_rows = _mem.conn.execute(
                "SELECT file, COUNT(*) c FROM issues GROUP BY file ORDER BY c DESC LIMIT 10"
            ).fetchall()
            if file_rows:
                file_df = pd.DataFrame([dict(r) for r in file_rows])
                file_df = file_df.set_index("file")
                st.bar_chart(file_df, width="stretch", height=220)

        elif st.session_state.scan_results:
            results = st.session_state.scan_results
            issues = results.get("issues", [])
            if issues:
                st.divider()
                st.subheader("Current Scan Breakdown")
                type_counts = {}
                for i in issues:
                    t = i.get("issue_type", "unknown")
                    type_counts[t] = type_counts.get(t, 0) + 1
                for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                    st.progress(count / len(issues), text=f"{t}: {count}")

        # ── Evaluation Metrics ────────────────────────────────────
        st.divider()
        st.subheader("📊 Evaluation Metrics")
        eval_data = _mem.get_evaluation_summary()
        if eval_data.get("total", 0) > 0:
            import pandas as pd
            e1, e2, e3, e4 = st.columns(4)
            e1.metric("Total Fixes Attempted", eval_data["total"])
            e2.metric("Fix Success Rate", f"{eval_data['fix_success_rate']*100:.1f}%")
            e3.metric("False Positive Rate", f"{eval_data['false_positive_rate']*100:.1f}%")
            e4.metric("Avg Confidence", f"{eval_data['avg_confidence']:.2f}")

            col_e1, col_e2 = st.columns(2)
            with col_e1:
                st.caption("Success rate by issue type")
                type_rows = eval_data.get("by_issue_type", [])
                if type_rows:
                    tdf = pd.DataFrame(type_rows)
                    tdf["success_rate"] = (tdf["applied"] / tdf["total"].replace(0, 1)).round(2)
                    tdf = tdf.set_index("issue_type")[["success_rate"]]
                    st.bar_chart(tdf, height=200)

            with col_e2:
                st.caption("Confidence calibration (predicted vs actual)")
                cal = eval_data.get("calibration", [])
                if cal:
                    cdf = pd.DataFrame(cal)
                    cdf["actual_rate"] = (cdf["applied"] / cdf["total"].replace(0, 1)).round(2)
                    cdf = cdf.set_index("bucket")[["actual_rate"]]
                    st.line_chart(cdf, height=200)

            # Recent outcomes table
            recent = _mem.get_recent_outcomes(limit=10)
            if recent:
                st.caption("Recent fix outcomes")
                rdf = pd.DataFrame(recent)[["recorded_at", "file", "issue_type", "predicted_conf", "reviewer_iters", "outcome", "false_positive"]]
                rdf.columns = ["Date", "File", "Type", "Conf", "Iters", "Outcome", "FP"]
                st.dataframe(rdf, use_container_width=True, hide_index=True)
        else:
            st.info("No fix outcomes recorded yet. Run Auto-Fix to start collecting metrics.")

        # ── Schedule ──────────────────────────────────────────────
        st.divider()
        st.subheader("Scheduled Scans")

        sched_status = st.session_state.agent.scheduler.status()
        if sched_status["running"] and sched_status["jobs"]:
            for job in sched_status["jobs"]:
                st.success(f"🕐 **{job['name']}** — next run: {job['next_run']}")
        else:
            st.info("No scheduled scans running.")

        col1, col2 = st.columns(2)
        with col1:
            scan_hour = st.number_input("Daily scan hour (0–23)", min_value=0, max_value=23,
                                         value=2, step=1)
            if st.button("📅 Start Daily Scan", width="stretch"):
                st.session_state.agent.scheduler.start_daily(hour=scan_hour)
                st.success(f"Daily scan scheduled at {scan_hour:02d}:00")
                st.rerun()
        with col2:
            interval_min = st.number_input("Interval scan (minutes)", min_value=5, max_value=1440,
                                            value=60, step=5)
            if st.button("⏱️ Start Interval Scan", width="stretch"):
                st.session_state.agent.scheduler.start_interval(minutes=interval_min)
                st.success(f"Interval scan every {interval_min} min started.")
                st.rerun()

        if sched_status["running"]:
            if st.button("⏹️ Stop Scheduler", width="stretch"):
                st.session_state.agent.scheduler.stop()
                st.warning("Scheduler stopped.")
                st.rerun()

        # ── Branch Manager ────────────────────────────────────────
        st.divider()
        st.subheader("🌿 Branch Manager")
        st.caption("View and clean up agent-fix branches created by auto-fix runs.")
        try:
            git_mgr = st.session_state.agent.git
            all_branches = git_mgr.list_branches()
            fix_branches = sorted([b for b in all_branches if b.startswith("agent-fix-")], reverse=True)
            current = git_mgr.current_branch()
            if fix_branches:
                st.write(f"**{len(fix_branches)} agent-fix branch(es)** — current: `{current}`")
                for br in fix_branches:
                    bcol1, bcol2 = st.columns([4, 1])
                    bcol1.code(br, language=None)
                    with bcol2:
                        if st.button("🗑️ Delete", key=f"delbr_{br}", width="stretch",
                                     disabled=(br == current)):
                            git_mgr.delete_branch(br, force=True)
                            st.success(f"Deleted `{br}`")
                            st.rerun()
                if st.button("🧹 Delete All Fix Branches", width="stretch", type="secondary"):
                    for br in fix_branches:
                        if br != current:
                            git_mgr.delete_branch(br, force=True)
                    st.success(f"Deleted {len([b for b in fix_branches if b != current])} branch(es).")
                    st.rerun()
            else:
                st.info("No agent-fix branches found.")
        except Exception as e:
            st.error(f"Branch manager error: {e}")

        # ── Interactive Dependency Graph ──────────────────────────
        st.divider()
        st.subheader("🕸️ Dependency Graph")
        st.caption("Interactive map of file dependencies — node size = number of dependents, color = issue count.")
        if st.button("🔄 Build / Refresh Graph", width="stretch", type="secondary"):
            with st.spinner("Building dependency graph..."):
                try:
                    from core.graph import DependencyGraph
                    dg = DependencyGraph(st.session_state.repo_path)
                    dg.build()
                    G = dg.graph

                    if len(G.nodes) == 0:
                        st.warning("No dependencies found in this repository.")
                    else:
                        # Limit to top 40 nodes by degree to keep graph readable
                        if len(G.nodes) > 40:
                            top = sorted(G.degree(), key=lambda x: x[1], reverse=True)[:40]
                            top_names = {n for n, _ in top}
                            G = G.subgraph(top_names).copy()

                        # Count issues per file from latest scan
                        issue_counts: dict[str, int] = {}
                        if st.session_state.scan_results:
                            for iss in st.session_state.scan_results.get("issues", []):
                                f = iss.get("file", "")
                                issue_counts[f] = issue_counts.get(f, 0) + 1

                        from pyvis.network import Network
                        import streamlit.components.v1 as _components

                        net = Network(height="520px", width="100%", directed=True,
                                      bgcolor="#0e1117", font_color="white")
                        net.set_options("""{
                          "nodes": {"font": {"size": 11}},
                          "edges": {"arrows": {"to": {"enabled": true, "scaleFactor": 0.5}},
                                    "color": {"color": "#555"}},
                          "physics": {"stabilization": {"iterations": 100}}
                        }""")

                        for node in G.nodes():
                            label = node.split("/")[-1]  # filename only
                            issues_n = issue_counts.get(node, 0)
                            degree = G.degree(node)
                            # Color: red if has issues, green if clean
                            color = "#e74c3c" if issues_n > 0 else "#27ae60"
                            size = max(10, min(40, 10 + degree * 3))
                            net.add_node(node, label=label, title=f"{node}\n{issues_n} issue(s)",
                                         color=color, size=size)

                        for src, dst in G.edges():
                            if src in [n["id"] for n in net.nodes] and dst in [n["id"] for n in net.nodes]:
                                net.add_edge(src, dst)

                        html_graph = net.generate_html()
                        st.session_state["dep_graph_html"] = html_graph
                        st.success(f"Graph built: {len(G.nodes)} nodes, {len(G.edges())} edges.")
                except Exception as e:
                    st.error(f"Dependency graph error: {e}")

        if st.session_state.get("dep_graph_html"):
            import streamlit.components.v1 as _components
            _components.html(st.session_state["dep_graph_html"], height=540, scrolling=False)
            st.caption("🔴 Red = has open issues  |  🟢 Green = clean  |  Node size = number of dependents")

        # ── Benchmark ─────────────────────────────────────────────
        st.divider()
        st.subheader("🧪 Benchmark Suite")
        st.caption("Run the built-in benchmark cases to measure fix accuracy, confidence, and speed.")
        if st.button("▶️ Run Benchmark", width="stretch", type="secondary"):
            with st.spinner("Running benchmark cases (this calls the LLM — may take 2-3 min)..."):
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(Path(__file__).parent.parent))
                    from benchmarks.runner import main as _run_bench
                    bench_data = _run_bench()
                    if bench_data:
                        b1, b2, b3, b4 = st.columns(4)
                        b1.metric("Total Cases", bench_data["total"])
                        b2.metric("Accuracy", f"{bench_data['accuracy']*100:.1f}%")
                        b3.metric("Avg Confidence", f"{bench_data['avg_confidence']:.2f}")
                        b4.metric("Avg Review Iters", f"{bench_data['avg_reviewer_iters']:.1f}")
                        st.dataframe(
                            [{
                                "Case": c["name"],
                                "Type": c["issue_type"],
                                "Result": c["status"],
                                "Conf": f"{c['confidence']:.2f}",
                                "Iters": c["reviewer_iters"],
                                "Time": f"{c['elapsed']:.1f}s",
                            } for c in bench_data["cases"]],
                            use_container_width=True, hide_index=True,
                        )
                        report_files = sorted(Path("benchmarks/results").glob("*.html"), reverse=True)
                        if report_files:
                            html_content = report_files[0].read_text()
                            st.download_button(
                                "⬇️ Download Benchmark Report",
                                data=html_content,
                                file_name=report_files[0].name,
                                mime="text/html",
                                width="stretch",
                            )
                except Exception as e:
                    st.error(f"Benchmark failed: {e}")

        # ── Export report ─────────────────────────────────────────
        st.divider()
        st.subheader("Export Report")
        st.caption("Generate a self-contained HTML report of all findings and fixes.")
        if st.button("📄 Export HTML Report", width="stretch", type="primary"):
            try:
                from core.reporter import generate_html_report, save_report
                html = generate_html_report(
                    repo_path=st.session_state.repo_path,
                    scan_results=st.session_state.scan_results or {},
                    plan=st.session_state.plan,
                    fix_history=st.session_state.fix_history,
                    runtime_results=st.session_state.runtime_scan_results,
                    stats=st.session_state.agent.stats(),
                )
                report_path = save_report(st.session_state.repo_path, html)
                st.success(f"Report saved: `{report_path}`")
                st.download_button(
                    label="⬇️ Download Report",
                    data=html,
                    file_name=f"codebase_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                    mime="text/html",
                    width="stretch",
                )
                log(f"Report exported to {report_path}")
            except Exception as e:
                st.error(f"Report generation failed: {e}")

    else:
        st.info("Connect to a repository to view stats.")
