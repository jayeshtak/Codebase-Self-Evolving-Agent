import re
import sys
from pathlib import Path
from dataclasses import dataclass


@dataclass
class RuntimeIssue:
    file: str
    line: int
    issue_type: str        # "runtime_error"
    severity: str          # always "high" — it crashes
    error_class: str       # "TypeError", "AttributeError", etc.
    description: str
    traceback: str
    language: str = "python"

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "language": self.language,
            "extra": {
                "error_class": self.error_class,
                "traceback": self.traceback[:500],
            },
        }


# ── Python traceback parser ───────────────────────────────────────────────────

def parse_python_traceback(stderr: str, repo_path: str) -> list[RuntimeIssue]:
    """
    Parse one or more Python tracebacks from stderr.
    Returns a list of RuntimeIssue, one per distinct error location.
    """
    issues = []
    repo = Path(repo_path).resolve()

    # Split on multiple tracebacks if present
    blocks = re.split(r'(?=Traceback \(most recent call last\))', stderr)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract all "File ..., line N" entries
        file_lines = re.findall(
            r'File "([^"]+)", line (\d+)',
            block
        )
        # Get the error type and message (last line of block)
        error_match = re.search(
            r'^([A-Za-z][A-Za-z0-9_]*(?:Error|Exception|Warning|Interrupt|Exit)[^\n]*)',
            block, re.MULTILINE
        )
        if not error_match:
            # Fallback: last non-empty line
            lines = [l for l in block.splitlines() if l.strip()]
            error_line = lines[-1] if lines else "Unknown error"
            error_class = "RuntimeError"
        else:
            error_line = error_match.group(1).strip()
            error_class = error_line.split(":")[0].strip()

        # Use the innermost frame that belongs to the repo
        target_file, target_line = None, 0
        for fpath, lineno in reversed(file_lines):
            try:
                resolved = Path(fpath).resolve()
                rel = resolved.relative_to(repo)
                target_file = str(rel)
                target_line = int(lineno)
                break
            except (ValueError, OSError):
                continue

        # Fall back to first frame if nothing in repo
        if not target_file and file_lines:
            target_file = file_lines[-1][0]
            target_line = int(file_lines[-1][1])

        if not target_file:
            continue

        issues.append(RuntimeIssue(
            file=target_file,
            line=target_line,
            issue_type="runtime_error",
            severity="high",
            error_class=error_class,
            description=f"{error_class}: {error_line.split(':', 1)[-1].strip()[:120]}",
            traceback=block[:800],
        ))

    return issues


# ── HTTP error parser ─────────────────────────────────────────────────────────

def parse_http_error(
    route: str,
    status_code: int,
    response_body: str,
    server_stderr: str,
    repo_path: str,
) -> list[RuntimeIssue]:
    """
    Parse a failed HTTP response into RuntimeIssues.
    Tries to extract a traceback from the server stderr first,
    falls back to a generic route-level issue.
    """
    issues = parse_python_traceback(server_stderr, repo_path)

    if issues:
        # Annotate with route info
        for issue in issues:
            issue.description = f"[{status_code} {route}] {issue.description}"
        return issues

    # Generic fallback
    return [RuntimeIssue(
        file="",
        line=0,
        issue_type="runtime_error",
        severity="high",
        error_class=f"HTTP{status_code}",
        description=f"Route {route} returned {status_code}: {response_body[:120]}",
        traceback=server_stderr[:500],
    )]


# ── Node/JS error parser ──────────────────────────────────────────────────────

def parse_node_error(stderr: str, repo_path: str) -> list[RuntimeIssue]:
    """
    Parse Node.js/Express errors from stderr.
    Handles both thrown errors and unhandled rejections.
    """
    issues = []
    repo = Path(repo_path).resolve()

    # Node stack trace pattern: "    at functionName (file:line:col)"
    error_header = re.search(r'^([A-Za-z][A-Za-z0-9_]*Error[^\n]*)', stderr, re.MULTILINE)
    error_class = "NodeError"
    error_msg = stderr.splitlines()[0] if stderr else "Unknown Node error"

    if error_header:
        error_msg = error_header.group(1).strip()
        error_class = error_msg.split(":")[0].strip()

    # Find frames in the repo
    frames = re.findall(r'at .+?\((.+?):(\d+):\d+\)', stderr)
    target_file, target_line = None, 0

    for fpath, lineno in frames:
        try:
            resolved = Path(fpath).resolve()
            rel = resolved.relative_to(repo)
            target_file = str(rel)
            target_line = int(lineno)
            break
        except (ValueError, OSError):
            continue

    if not target_file:
        return []

    issues.append(RuntimeIssue(
        file=target_file,
        line=target_line,
        issue_type="runtime_error",
        severity="high",
        error_class=error_class,
        description=f"{error_class}: {error_msg.split(':', 1)[-1].strip()[:120]}",
        traceback=stderr[:800],
        language="javascript",
    ))
    return issues


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = """
Traceback (most recent call last):
  File "/home/user/project/app.py", line 42, in handle_request
    result = process(data)
  File "/home/user/project/utils.py", line 17, in process
    return data["key"] / data["divisor"]
ZeroDivisionError: division by zero
"""
    issues = parse_python_traceback(sample, "/home/user/project")
    for i in issues:
        print(i.to_dict())
