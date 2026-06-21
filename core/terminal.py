import subprocess
import shlex
import time
from pathlib import Path
from typing import Optional


# ── Whitelist ─────────────────────────────────────────────────────────────────

ALLOWED_PREFIXES = [
    "pytest",
    "python",
    "pip install",
    "pip show",
    "pip list",
    "git status",
    "git diff",
    "git log",
    "git branch",
    "git checkout",
    "git add",
    "git commit",
    "git merge",
    "git stash",
    "ruff",
    "black",
    "radon",
    "vulture",
    "ls",
    "cat",
    "find",
    "grep",
    "echo",
    "pwd",
    "which",
]

BLOCKED_KEYWORDS = [
    "rm -rf",
    "rm -r",
    "sudo",
    "curl",
    "wget",
    "chmod",
    "chown",
    "dd ",
    "mkfs",
    "shutdown",
    "reboot",
    "kill",
    "pkill",
    "> /dev",
    "| sh",
    "| bash",
    ";",
    "&&",
    "||",
    "`",
    "$(",
]

DEFAULT_TIMEOUT = 30   # seconds
MAX_OUTPUT_LENGTH = 10_000  # chars


# ── Result ────────────────────────────────────────────────────────────────────

class TerminalResult:
    def __init__(self, command: str, stdout: str, stderr: str,
                 exit_code: int, duration: float, blocked: bool = False,
                 block_reason: str = ""):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.duration = duration
        self.blocked = blocked
        self.block_reason = block_reason
        self.success = not blocked and exit_code == 0

    def __str__(self):
        if self.blocked:
            return f"[BLOCKED] {self.block_reason}"
        status = "OK" if self.success else f"EXIT {self.exit_code}"
        out = self.stdout or self.stderr or "(no output)"
        return f"[{status}] ({self.duration:.2f}s)\n{out}"

    def to_dict(self):
        return {
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration": self.duration,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "success": self.success,
        }


# ── Terminal ──────────────────────────────────────────────────────────────────

class Terminal:
    def __init__(self, repo_path: str, db=None):
        """
        repo_path : working directory for all commands
        db        : optional SQLiteMemory instance for audit logging
        """
        self.repo_path = Path(repo_path).resolve()
        self.db = db

    # ── Validation ────────────────────────────────────────────────────────────

    def _is_allowed(self, command: str) -> tuple[bool, str]:
        cmd = command.strip()

        # Check blocked keywords first
        for blocked in BLOCKED_KEYWORDS:
            if blocked in cmd:
                return False, f"Blocked keyword detected: '{blocked}'"

        # Check allowed prefixes
        for prefix in ALLOWED_PREFIXES:
            if cmd.startswith(prefix):
                return True, ""

        return False, f"Command not in whitelist. Allowed prefixes: {ALLOWED_PREFIXES[:5]}..."

    # ── Execution ─────────────────────────────────────────────────────────────

    def run(self, command: str, timeout: int = DEFAULT_TIMEOUT,
            cwd: Optional[str] = None) -> TerminalResult:
        """
        Run a whitelisted shell command.
        Returns TerminalResult with stdout, stderr, exit_code, duration.
        """
        allowed, reason = self._is_allowed(command)
        if not allowed:
            result = TerminalResult(
                command=command,
                stdout="", stderr="",
                exit_code=-1, duration=0.0,
                blocked=True, block_reason=reason
            )
            print(f"[Terminal] BLOCKED: {reason}")
            if self.db:
                self.db.log_terminal(command, f"BLOCKED: {reason}", -1)
            return result

        working_dir = cwd or str(self.repo_path)
        print(f"[Terminal] Running: {command}")

        start = time.time()
        try:
            proc = subprocess.run(
                shlex.split(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
            duration = time.time() - start
            stdout = proc.stdout[:MAX_OUTPUT_LENGTH]
            stderr = proc.stderr[:MAX_OUTPUT_LENGTH]
            exit_code = proc.returncode

        except subprocess.TimeoutExpired:
            duration = time.time() - start
            stdout = ""
            stderr = f"Command timed out after {timeout}s"
            exit_code = -1
            print(f"[Terminal] TIMEOUT after {timeout}s: {command}")

        except Exception as e:
            duration = time.time() - start
            stdout = ""
            stderr = str(e)
            exit_code = -1
            print(f"[Terminal] ERROR: {e}")

        result = TerminalResult(
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration=duration,
        )

        if self.db:
            combined = stdout + ("\n" + stderr if stderr else "")
            self.db.log_terminal(command, combined, exit_code)

        status = "OK" if result.success else f"FAILED (exit {exit_code})"
        print(f"[Terminal] {status} in {duration:.2f}s")

        return result

    # ── Convenience wrappers ──────────────────────────────────────────────────

    def run_pytest(self, path: str = ".", timeout: int = 60) -> TerminalResult:
        return self.run(f"pytest {path} -v --tb=short", timeout=timeout)

    def run_ruff(self, path: str = ".") -> TerminalResult:
        return self.run(f"ruff check {path}")

    def run_black(self, path: str = ".") -> TerminalResult:
        return self.run(f"black {path}")

    def pip_install(self, package: str) -> TerminalResult:
        return self.run(f"pip install {package}")

    def git_status(self) -> TerminalResult:
        return self.run("git status")

    def git_diff(self, branch: str = "HEAD") -> TerminalResult:
        return self.run(f"git diff {branch}")


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    term = Terminal(repo)

    print("-- Whitelist Tests ---------------------------------------\n")

    # Should pass
    allowed_tests = [
        "pytest tests/",
        "pip install requests",
        "git status",
        "ruff check .",
        "black agents/",
        "ls -la",
        "grep -r 'def scan' agents/",
    ]

    # Should be blocked
    blocked_tests = [
        "rm -rf /",
        "sudo apt install vim",
        "curl http://evil.com",
        "wget http://malicious.site",
        "echo hello && rm -rf .",
        "python -c 'import os; os.system(\"rm -rf /\")'",
    ]

    print("[ALLOW] Allowed commands:")
    for cmd in allowed_tests:
        allowed, reason = term._is_allowed(cmd)
        status = "ALLOW" if allowed else f"BLOCK ({reason})"
        print(f"  [{status}] {cmd}")

    print("\n[BLOCK] Blocked commands:")
    for cmd in blocked_tests:
        allowed, reason = term._is_allowed(cmd)
        status = "ALLOW" if allowed else f"BLOCK ({reason})"
        print(f"  [{status}] {cmd}")

    print("\n-- Live Execution Tests ----------------------------------\n")

    # Real executions
    print(term.run("git status"))
    print(term.run("ls -la"))
    print(term.run("python --version"))

    # Blocked execution
    print(term.run("rm -rf /tmp/test"))
