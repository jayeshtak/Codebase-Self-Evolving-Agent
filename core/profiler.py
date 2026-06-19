"""
Performance profiler.
- Python: cProfile via subprocess, parses cumulative time per function
- Node.js: node --prof + node --prof-process (isolate-*.log)
Returns Issue-compatible dicts with issue_type="performance".
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def _run(cmd: list[str], cwd: str, timeout: int = 60,
         extra_env: Optional[dict] = None) -> tuple[int, str, str]:
    env = {**os.environ, **(extra_env or {})}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=cwd, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "Timed out"
    except Exception as e:
        return -1, "", str(e)


def _issue(file: str, line: int, description: str,
           language: str = "python", extra: Optional[dict] = None) -> dict:
    return {
        "file": file,
        "line": line,
        "issue_type": "performance",
        "severity": "medium",
        "description": description,
        "language": language,
        "extra": extra or {},
        "fix_strategy": "Profile and optimise the hotspot identified above.",
    }


# ── Python cProfile ───────────────────────────────────────────────────────────

def profile_python(repo_path: str, entry_script: str,
                   top_n: int = 10, threshold_ms: float = 50.0) -> list[dict]:
    """
    Run cProfile on entry_script and return the top_n slowest functions
    that exceed threshold_ms cumulative time.
    """
    import shutil, sys
    python = shutil.which("python3") or sys.executable
    script_path = Path(repo_path) / entry_script
    if not script_path.exists():
        return []

    print(f"[Profiler] Profiling Python: {entry_script}")
    with tempfile.NamedTemporaryFile(suffix=".prof", delete=False) as f:
        prof_path = f.name

    try:
        code, stdout, stderr = _run(
            [python, "-m", "cProfile", "-o", prof_path, str(script_path)],
            cwd=repo_path, timeout=30,
        )

        # Use pstats to read the .prof file
        import pstats, io
        stream = io.StringIO()
        ps = pstats.Stats(prof_path, stream=stream)
        ps.sort_stats("cumulative")
        ps.print_stats(top_n * 3)  # get extra rows, we'll filter
        output = stream.getvalue()
    except Exception as e:
        print(f"[Profiler] cProfile error: {e}")
        return []
    finally:
        try:
            os.unlink(prof_path)
        except Exception:
            pass

    issues = []
    # pstats output: "   ncalls  tottime  percall  cumtime  percall filename:lineno(function)"
    for line in output.splitlines():
        # Match lines like: "  100  0.001  0.000  0.234  0.002 /path/file.py:42(func_name)"
        m = re.match(
            r"\s*[\d/]+\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(.+?):(\d+)\((.+?)\)",
            line
        )
        if not m:
            continue
        cumtime = float(m.group(4))
        filepath = m.group(5)
        lineno = int(m.group(6))
        funcname = m.group(7)

        if cumtime * 1000 < threshold_ms:
            continue
        if "<" in filepath:
            continue  # skip built-ins like <frozen importlib>

        try:
            rel = str(Path(filepath).relative_to(repo_path))
        except ValueError:
            continue  # skip stdlib / venv files not in repo

        if "venv" in rel or "site-packages" in rel:
            continue

        issues.append(_issue(
            file=rel,
            line=lineno,
            description=(
                f"Slow function '{funcname}' — {cumtime*1000:.1f}ms cumulative "
                f"(threshold={threshold_ms}ms)"
            ),
            language="python",
            extra={"function": funcname, "cumtime_ms": round(cumtime * 1000, 1)},
        ))
        if len(issues) >= top_n:
            break

    print(f"[Profiler] Python: {len(issues)} hotspot(s) found.")
    return issues


# ── Node.js --prof ────────────────────────────────────────────────────────────

def profile_node(repo_path: str, entry_script: str,
                 top_n: int = 10, threshold_ms: float = 50.0) -> list[dict]:
    """
    Run node --prof on entry_script for a few seconds, then process the
    isolate log and return the top hotspots.
    """
    import shutil, signal, time
    if not shutil.which("node"):
        return []

    script_path = Path(repo_path) / entry_script
    if not script_path.exists():
        return []

    print(f"[Profiler] Profiling Node.js: {entry_script}")

    env = {**os.environ, "NODE_ENV": "production"}
    try:
        # Start node --prof, let it run briefly, then kill
        proc = subprocess.Popen(
            ["node", "--prof", str(script_path)],
            cwd=repo_path, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(5)  # profile for 5s
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    except Exception as e:
        print(f"[Profiler] node --prof launch error: {e}")
        return []

    # Find the generated isolate-*.log
    log_files = list(Path(repo_path).glob("isolate-*-v8.log"))
    if not log_files:
        log_files = list(Path(repo_path).glob("isolate-*.log"))
    if not log_files:
        return []

    log_file = str(log_files[0])
    code, stdout, stderr = _run(
        ["node", "--prof-process", log_file],
        cwd=repo_path, timeout=30,
    )

    # Clean up
    for f in log_files:
        try:
            f.unlink()
        except Exception:
            pass

    if not stdout:
        return []

    issues = []
    # --prof-process output has a "Bottom up (heavy) profile" section
    # Lines: "  X%   Y%   Z   file:line  function"
    in_profile = False
    for line in stdout.splitlines():
        if "Bottom up" in line or "Top down" in line:
            in_profile = True
        if not in_profile:
            continue
        # Match: "  42%    3%    0  /path/file.js:123  myFunction"
        m = re.match(r"\s+(\d+)%\s+\d+%.*?([^\s]+\.(?:js|ts|mjs)):(\d+)\s+(.+)", line)
        if not m:
            continue
        pct = int(m.group(1))
        filepath = m.group(2)
        lineno = int(m.group(3))
        funcname = m.group(4).strip()

        if pct < 5:
            continue  # below 5% — ignore

        try:
            rel = str(Path(filepath).relative_to(repo_path))
        except ValueError:
            rel = filepath

        if "node_modules" in rel:
            continue

        issues.append(_issue(
            file=rel,
            line=lineno,
            description=(
                f"Hot function '{funcname}' — {pct}% of CPU time "
                f"(node --prof)"
            ),
            language="javascript",
            extra={"function": funcname, "cpu_pct": pct},
        ))
        if len(issues) >= top_n:
            break

    print(f"[Profiler] Node: {len(issues)} hotspot(s) found.")
    return issues


# ── Profiler ──────────────────────────────────────────────────────────────────

class Profiler:
    def __init__(self, repo_path: str):
        self.repo_path = str(Path(repo_path).resolve())

    def profile(self, entry_script: str = "", framework: str = "") -> list[dict]:
        """
        Auto-detect entry point and run profiler.
        Returns list of issue dicts with issue_type='performance'.
        """
        repo = Path(self.repo_path)

        # Auto-detect entry point
        if not entry_script:
            if framework == "node" or (repo / "package.json").exists():
                for name in ["index.js", "server.js", "app.js", "src/index.js"]:
                    if (repo / name).exists():
                        entry_script = name
                        break
                if entry_script:
                    return profile_node(self.repo_path, entry_script)
            # Python
            for name in ["main.py", "app.py", "run.py", "server.py"]:
                if (repo / name).exists():
                    entry_script = name
                    break

        if not entry_script:
            print("[Profiler] No entry point found for profiling.")
            return []

        ext = Path(entry_script).suffix.lower()
        if ext == ".py":
            return profile_python(self.repo_path, entry_script)
        elif ext in (".js", ".mjs", ".ts"):
            return profile_node(self.repo_path, entry_script)

        return []
