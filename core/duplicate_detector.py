"""
Duplicate / clone code detection.
- Python: pylint --duplicate-code
- JS/TS/Java/C++/Go/PHP/etc: jscpd (via npx)
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def _resolve_cmd_path(name: str) -> str:
    import shutil
    import sys
    if shutil.which(name):
        return name
    local_bin = Path(sys.executable).parent / name
    if local_bin.exists():
        return str(local_bin)
    if local_bin.with_suffix(".exe").exists():
        return str(local_bin.with_suffix(".exe"))
    return name


def _run(cmd: list[str], cwd: str, timeout: int = 60,
         extra_env: Optional[dict] = None) -> tuple[int, str, str]:
    env = {**os.environ, **(extra_env or {})}
    resolved_cmd = list(cmd)
    resolved_cmd[0] = _resolve_cmd_path(resolved_cmd[0])
    try:
        r = subprocess.run(resolved_cmd, capture_output=True, text=True,
                           cwd=cwd, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {resolved_cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "Timed out"
    except Exception as e:
        return -1, "", str(e)


def _issue(file: str, line: int, description: str,
           language: str = "python", extra: Optional[dict] = None) -> dict:
    return {
        "file": file,
        "line": line,
        "issue_type": "duplicate",
        "severity": "low",
        "description": description,
        "language": language,
        "extra": extra or {},
        "fix_strategy": "Extract the duplicated logic into a shared function or module.",
    }


# ── pylint duplicate-code (Python) ───────────────────────────────────────────

def detect_python_duplicates(repo_path: str, files: list[Path]) -> list[dict]:
    import sys
    pylint_path = _resolve_cmd_path("pylint")
    # Check if pylint_path exists or is executable
    import shutil
    if pylint_path == "pylint" and not shutil.which("pylint"):
        return []
    elif pylint_path != "pylint" and not Path(pylint_path).exists():
        return []
    py_files = [str(f) for f in files if f.suffix == ".py"]
    if len(py_files) < 2:
        return []

    print(f"[Duplicate] Running pylint duplicate-code on {len(py_files)} Python file(s)...")
    code, stdout, stderr = _run(
        ["pylint", "--disable=all", "--enable=duplicate-code",
         "--min-similarity-lines=6", "-f", "text"] + py_files,
        cwd=repo_path, timeout=60,
    )

    issues = []
    # Parse pylint output: "filename.py:42: [R0801(duplicate-code), ] Similar lines..."
    for line in (stdout + stderr).splitlines():
        m = re.match(r"^(.+?):(\d+):\s*\[R0801", line)
        if m:
            try:
                rel = str(Path(m.group(1)).relative_to(repo_path))
            except ValueError:
                rel = m.group(1)
            issues.append(_issue(
                file=rel,
                line=int(m.group(2)),
                description="Duplicate code block detected (pylint R0801) - consider extracting to shared function",
                language="python",
                extra={"rule": "R0801"},
            ))

    if issues:
        print(f"[Duplicate] pylint: {len(issues)} duplicate block(s).")
    return issues


# ── jscpd (multi-language clone detection) ───────────────────────────────────

def detect_jscpd_duplicates(repo_path: str, files: list[Path]) -> list[dict]:
    import shutil
    if not shutil.which("npx"):
        return []

    # Filter to languages jscpd supports well
    supported_exts = {".py", ".js", ".mjs", ".jsx", ".ts", ".tsx",
                      ".java", ".cpp", ".cxx", ".cc", ".c", ".php", ".go", ".rs"}
    target_files = [f for f in files if f.suffix.lower() in supported_exts]
    if len(target_files) < 2:
        return []

    print(f"[Duplicate] Running jscpd on {len(target_files)} file(s)...")

    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "jscpd-report.json"
        file_paths = [str(f) for f in target_files]

        code, stdout, stderr = _run(
            ["npx", "jscpd",
             "--min-tokens", "50",
             "--min-lines", "5",
             "--reporters", "json",
             "--output", tmpdir,
             "--ignore", "node_modules/**,venv/**,.git/**",
             "--no-header",
             ] + file_paths,
            cwd=repo_path, timeout=90,
        )

        report_file = Path(tmpdir) / "jscpd-report.json"
        if not report_file.exists():
            return []

        try:
            data = json.loads(report_file.read_text())
        except Exception as e:
            print(f"[Duplicate] jscpd parse error: {e}")
            return []

    issues = []
    _ext_lang = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascript", ".tsx": "typescript", ".java": "java",
        ".go": "go", ".rs": "rust", ".php": "php",
        ".cpp": "cpp", ".c": "c",
    }

    for clone in data.get("duplicates", []):
        # Each clone has firstFile and secondFile
        f1 = clone.get("firstFile", {})
        f2 = clone.get("secondFile", {})
        file1 = f1.get("name", "")
        file2 = f2.get("name", "")
        line1 = f1.get("start", 0)
        line2 = f2.get("start", 0)
        lines = clone.get("lines", 0)
        tokens = clone.get("tokens", 0)

        try:
            rel1 = str(Path(file1).relative_to(repo_path))
        except ValueError:
            rel1 = file1
        try:
            rel2 = str(Path(file2).relative_to(repo_path))
        except ValueError:
            rel2 = file2

        lang = _ext_lang.get(Path(rel1).suffix.lower(), "unknown")
        desc = (f"Duplicate block ({lines} lines, {tokens} tokens) also found in "
                f"{rel2}:{line2} - extract to shared function")

        issues.append(_issue(
            file=rel1, line=line1,
            description=desc, language=lang,
            extra={"duplicate_in": rel2, "duplicate_line": line2,
                   "lines": lines, "tokens": tokens},
        ))

    if issues:
        print(f"[Duplicate] jscpd: {len(issues)} duplicate block(s).")
    return issues


# ── DuplicateDetector ─────────────────────────────────────────────────────────

class DuplicateDetector:
    def __init__(self, repo_path: str):
        self.repo_path = str(Path(repo_path).resolve())

    def scan(self, files: list[Path]) -> list[dict]:
        all_issues: list[dict] = []

        # pylint for Python
        all_issues += detect_python_duplicates(self.repo_path, files)

        # jscpd for everything (dedup with pylint results)
        jscpd_issues = detect_jscpd_duplicates(self.repo_path, files)
        existing = {(i["file"], i["line"]) for i in all_issues}
        for iss in jscpd_issues:
            if (iss["file"], iss["line"]) not in existing:
                all_issues.append(iss)

        return all_issues
