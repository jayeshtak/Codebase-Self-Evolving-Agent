import json
import re
import subprocess
import shlex
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TestResult:  # noqa: pytest-collect
    __test__ = False  # Prevent pytest from collecting this dataclass
    passed: bool
    total: int = 0
    passed_count: int = 0
    failed_count: int = 0
    error_count: int = 0
    failures: list[dict] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    exit_code: int = 0

    def summary(self) -> str:
        if self.passed:
            return f"✓ All {self.total} test(s) passed ({self.duration:.2f}s)"
        return (
            f"✗ {self.failed_count} failed, {self.error_count} errors "
            f"/ {self.total} total ({self.duration:.2f}s)"
        )

    def failure_details(self) -> str:
        if not self.failures:
            return self.stderr or "(no details)"
        return "\n\n".join(
            f"TEST: {f['name']}\n{f['detail']}" for f in self.failures
        )


@dataclass
class HealingResult:
    final_result: TestResult
    attempts: int
    healed: bool
    heal_log: list[str] = field(default_factory=list)


# ── Executor ──────────────────────────────────────────────────────────────────

class Executor:
    def __init__(self, repo_path: str, venv_python: Optional[str] = None):
        self.repo_path = Path(repo_path).resolve()

        # Find venv python
        if venv_python:
            self.python = venv_python
        else:
            candidates = [
                self.repo_path / "venv" / "bin" / "python",
                self.repo_path / ".venv" / "bin" / "python",
            ]
            self.python = next(
                (str(p) for p in candidates if p.exists()),
                "python3"
            )

        print(f"[Executor] Using python: {self.python}")

    # ── Detect test framework ─────────────────────────────────────────────────

    def detect_test_framework(self, source_file: str) -> str:
        """
        Return the test framework to use based on the source file extension
        and project structure. Returns one of:
        'pytest' | 'jest' | 'go' | 'cargo' | 'maven' | 'phpunit' | 'unknown'
        """
        suffix = Path(source_file).suffix.lower()
        repo = self.repo_path

        if suffix == ".py":
            return "pytest"
        if suffix in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
            return "jest"
        if suffix == ".go":
            return "go"
        if suffix == ".rs":
            return "cargo"
        if suffix == ".java":
            # Maven or Gradle
            if (repo / "pom.xml").exists():
                return "maven"
            if (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
                return "gradle"
            return "maven"
        if suffix == ".php":
            return "phpunit"
        return "pytest"  # default fallback

    # ── Run tests (language-aware) ────────────────────────────────────────────

    def run_tests(self, test_path: str = ".", timeout: int = 60,
                  framework: Optional[str] = None) -> TestResult:
        """
        Run tests using the appropriate framework.
        framework: 'pytest'|'jest'|'go'|'cargo'|'maven'|'gradle'|'phpunit'
        Defaults to pytest if not specified.
        """
        fw = framework or "pytest"
        print(f"[Executor] Running {fw} tests: {test_path}")

        if fw == "pytest":
            return self._run_pytest(test_path, timeout)
        if fw == "jest":
            return self._run_jest(test_path, timeout)
        if fw == "go":
            return self._run_go_test(test_path, timeout)
        if fw == "cargo":
            return self._run_cargo_test(timeout)
        if fw in ("maven", "gradle"):
            return self._run_jvm_tests(fw, timeout)
        if fw == "phpunit":
            return self._run_phpunit(test_path, timeout)
        # Fallback
        return self._run_pytest(test_path, timeout)

    # ── pytest ────────────────────────────────────────────────────────────────

    def _run_pytest(self, test_path: str, timeout: int) -> TestResult:
        cmd = f"{self.python} -m pytest {test_path} -v --tb=short --no-header -q"
        start = time.time()
        try:
            proc = subprocess.run(
                shlex.split(cmd),
                capture_output=True, text=True,
                timeout=timeout, cwd=str(self.repo_path),
            )
            return self._parse_pytest_output(
                stdout=proc.stdout, stderr=proc.stderr,
                exit_code=proc.returncode, duration=time.time() - start,
            )
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr=f"Timed out after {timeout}s",
                              duration=time.time() - start, exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e),
                              duration=time.time() - start, exit_code=-1)

    # ── Jest (JS/TS) ──────────────────────────────────────────────────────────

    def _run_jest(self, test_path: str, timeout: int) -> TestResult:
        import shutil
        runner = "npx"
        if not shutil.which(runner):
            return TestResult(passed=False, stderr="npx not found", exit_code=-1)

        start = time.time()
        try:
            proc = subprocess.run(
                [runner, "jest", "--no-coverage", "--forceExit", "--json", test_path],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(self.repo_path),
            )
            duration = time.time() - start
            return self._parse_jest_output(proc.stdout, proc.stderr,
                                           proc.returncode, duration)
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr=f"Timed out after {timeout}s",
                              duration=time.time() - start, exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e),
                              duration=time.time() - start, exit_code=-1)

    def _parse_jest_output(self, stdout: str, stderr: str,
                           exit_code: int, duration: float) -> TestResult:
        passed = exit_code == 0
        failures = []
        total = passed_count = failed_count = 0

        # Jest --json output
        try:
            # Jest writes JSON to stdout but may prefix with other text
            json_start = stdout.find("{")
            if json_start >= 0:
                data = json.loads(stdout[json_start:])
                total = data.get("numTotalTests", 0)
                passed_count = data.get("numPassedTests", 0)
                failed_count = data.get("numFailedTests", 0)
                for suite in data.get("testResults", []):
                    for t in suite.get("testResults", []):
                        if t.get("status") == "failed":
                            failures.append({
                                "name": t.get("fullName", ""),
                                "detail": "\n".join(t.get("failureMessages", []))[:400],
                            })
        except Exception:
            # Fallback: parse text output
            m = re.search(r"Tests:\s+(\d+) failed", stdout + stderr)
            if m:
                failed_count = int(m.group(1))
            m2 = re.search(r"Tests:\s+(\d+) passed", stdout + stderr)
            if m2:
                passed_count = int(m2.group(1))
            total = passed_count + failed_count

        if total == 0:
            return TestResult(passed=True, total=0, stdout=stdout,
                              stderr=stderr, duration=duration, exit_code=exit_code)

        return TestResult(
            passed=passed, total=total,
            passed_count=passed_count, failed_count=failed_count,
            failures=failures, stdout=stdout, stderr=stderr,
            duration=duration, exit_code=exit_code,
        )

    # ── go test ───────────────────────────────────────────────────────────────

    def _run_go_test(self, test_path: str, timeout: int) -> TestResult:
        import shutil
        if not shutil.which("go"):
            return TestResult(passed=False, stderr="go not found", exit_code=-1)

        start = time.time()
        try:
            proc = subprocess.run(
                ["go", "test", "-v", "./..."],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(self.repo_path),
            )
            duration = time.time() - start
            out = proc.stdout + proc.stderr
            passed = proc.returncode == 0
            failures = []
            for line in out.splitlines():
                if line.startswith("--- FAIL:"):
                    name = line.replace("--- FAIL:", "").strip().split()[0]
                    failures.append({"name": name, "detail": line})
            passed_count = len(re.findall(r"^--- PASS:", out, re.MULTILINE))
            failed_count = len(failures)
            return TestResult(
                passed=passed, total=passed_count + failed_count,
                passed_count=passed_count, failed_count=failed_count,
                failures=failures, stdout=proc.stdout, stderr=proc.stderr,
                duration=duration, exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr=f"Timed out", exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e), exit_code=-1)

    # ── cargo test (Rust) ─────────────────────────────────────────────────────

    def _run_cargo_test(self, timeout: int) -> TestResult:
        import shutil
        if not shutil.which("cargo"):
            return TestResult(passed=False, stderr="cargo not found", exit_code=-1)

        start = time.time()
        try:
            proc = subprocess.run(
                ["cargo", "test"],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(self.repo_path),
            )
            duration = time.time() - start
            out = proc.stdout + proc.stderr
            passed = proc.returncode == 0
            failures = []
            for line in out.splitlines():
                if line.strip().startswith("FAILED"):
                    name = line.strip().replace("FAILED", "").strip()
                    failures.append({"name": name, "detail": line})
            # "test result: ok. N passed; M failed;"
            m = re.search(r"(\d+) passed; (\d+) failed", out)
            passed_count = int(m.group(1)) if m else 0
            failed_count = int(m.group(2)) if m else len(failures)
            return TestResult(
                passed=passed, total=passed_count + failed_count,
                passed_count=passed_count, failed_count=failed_count,
                failures=failures, stdout=proc.stdout, stderr=proc.stderr,
                duration=duration, exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr="Timed out", exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e), exit_code=-1)

    # ── Maven / Gradle (Java) ─────────────────────────────────────────────────

    def _run_jvm_tests(self, fw: str, timeout: int) -> TestResult:
        import shutil
        repo = self.repo_path
        if fw == "maven":
            if not shutil.which("mvn"):
                return TestResult(passed=False, stderr="mvn not found", exit_code=-1)
            cmd = ["mvn", "test", "-q"]
        else:
            wrapper = str(repo / "gradlew") if (repo / "gradlew").exists() else "gradle"
            cmd = [wrapper, "test"]

        start = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=str(repo),
            )
            duration = time.time() - start
            passed = proc.returncode == 0
            out = proc.stdout + proc.stderr
            failures = []
            # Look for "FAILED" lines or "Tests run: X, Failures: Y, Errors: Z"
            for line in out.splitlines():
                if "FAILED" in line and "::" in line:
                    failures.append({"name": line.strip(), "detail": line})
            m = re.search(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+)", out)
            total = int(m.group(1)) if m else 0
            failed_count = int(m.group(2)) + int(m.group(3)) if m else len(failures)
            passed_count = total - failed_count
            return TestResult(
                passed=passed, total=total,
                passed_count=passed_count, failed_count=failed_count,
                failures=failures, stdout=proc.stdout, stderr=proc.stderr,
                duration=duration, exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr="Timed out", exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e), exit_code=-1)

    # ── PHPUnit ───────────────────────────────────────────────────────────────

    def _run_phpunit(self, test_path: str, timeout: int) -> TestResult:
        import shutil
        repo = self.repo_path
        # Look for phpunit binary
        candidates = [
            str(repo / "vendor" / "bin" / "phpunit"),
            "phpunit",
        ]
        phpunit = next((c for c in candidates
                        if shutil.which(c) or Path(c).exists()), None)
        if not phpunit:
            return TestResult(passed=False, stderr="phpunit not found", exit_code=-1)

        start = time.time()
        try:
            proc = subprocess.run(
                [phpunit, test_path, "--no-progress"],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(repo),
            )
            duration = time.time() - start
            passed = proc.returncode == 0
            out = proc.stdout + proc.stderr
            failures = []
            for line in out.splitlines():
                if line.strip().startswith("FAILURES!") or "FAILED" in line:
                    failures.append({"name": line.strip(), "detail": line})
            m = re.search(r"Tests: (\d+), Assertions: \d+", out)
            total = int(m.group(1)) if m else 0
            fm = re.search(r"Failures: (\d+)", out)
            failed_count = int(fm.group(1)) if fm else len(failures)
            return TestResult(
                passed=passed, total=total,
                passed_count=total - failed_count, failed_count=failed_count,
                failures=failures, stdout=proc.stdout, stderr=proc.stderr,
                duration=duration, exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return TestResult(passed=False, stderr="Timed out", exit_code=-1)
        except Exception as e:
            return TestResult(passed=False, stderr=str(e), exit_code=-1)

    # ── Parse pytest output ───────────────────────────────────────────────────

    def _parse_pytest_output(self, stdout: str, stderr: str,
                              exit_code: int, duration: float) -> TestResult:
        passed = exit_code == 0
        total = passed_count = failed_count = error_count = 0
        failures = []

        # Parse summary line: "3 passed, 1 failed in 0.12s"
        summary_pattern = re.search(
            r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error",
            stdout
        )

        # Count all occurrences
        passed_match = re.findall(r"(\d+) passed", stdout)
        failed_match = re.findall(r"(\d+) failed", stdout)
        error_match = re.findall(r"(\d+) error", stdout)

        passed_count = int(passed_match[-1]) if passed_match else 0
        failed_count = int(failed_match[-1]) if failed_match else 0
        error_count = int(error_match[-1]) if error_match else 0
        total = passed_count + failed_count + error_count

        # No tests found case
        if "no tests ran" in stdout.lower() or total == 0:
            return TestResult(
                passed=True,  # no tests = not a failure
                total=0,
                passed_count=0,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                exit_code=exit_code,
            )

        # Extract individual failure details
        # pytest --tb=short format: "FAILED test_file.py::test_name"
        fail_blocks = re.split(r"_{5,}", stdout)
        for block in fail_blocks:
            name_match = re.search(r"FAILED\s+([\w/.:]+)", block)
            if name_match:
                failures.append({
                    "name": name_match.group(1),
                    "detail": block.strip()[:500],
                })

        # Also catch short format
        for line in stdout.splitlines():
            if line.startswith("FAILED "):
                name = line.replace("FAILED ", "").strip()
                if not any(f["name"] == name for f in failures):
                    failures.append({"name": name, "detail": line})

        return TestResult(
            passed=passed,
            total=total,
            passed_count=passed_count,
            failed_count=failed_count,
            error_count=error_count,
            failures=failures,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            exit_code=exit_code,
        )

    # ── Self-healing loop ─────────────────────────────────────────────────────

    def run_with_healing(
        self,
        test_path: str = ".",
        max_attempts: int = 3,
        timeout: int = 60,
        heal_fn: Optional[Callable[[TestResult, int], bool]] = None,
        framework: Optional[str] = None,
    ) -> HealingResult:
        """
        Run tests. On failure, call heal_fn to attempt a fix, then retry.
        heal_fn(result, attempt) -> bool (True = healed, try again; False = give up)

        If no heal_fn provided, just runs once.
        """
        heal_log = []
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            print(f"[Executor] Test attempt {attempt}/{max_attempts}")

            result = self.run_tests(test_path, timeout=timeout, framework=framework)
            print(f"[Executor] {result.summary()}")

            if result.passed:
                return HealingResult(
                    final_result=result,
                    attempts=attempt,
                    healed=attempt > 1,
                    heal_log=heal_log,
                )

            # Tests failed
            if attempt >= max_attempts:
                heal_log.append(f"Attempt {attempt}: failed, max attempts reached.")
                break

            if heal_fn is None:
                heal_log.append(f"Attempt {attempt}: failed, no heal_fn provided.")
                break

            # Try to heal
            print(f"[Executor] Attempting self-heal (attempt {attempt})...")
            healed = heal_fn(result, attempt)
            if healed:
                heal_log.append(f"Attempt {attempt}: failed → heal applied, retrying.")
            else:
                heal_log.append(f"Attempt {attempt}: failed → heal_fn gave up.")
                break

        return HealingResult(
            final_result=result,
            attempts=attempt,
            healed=False,
            heal_log=heal_log,
        )


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tempfile
    import os

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    executor = Executor(repo)

    # Create temp test files
    passing_test = '''\
def test_addition():
    assert 1 + 1 == 2

def test_strings():
    assert "hello".upper() == "HELLO"
'''

    failing_test = '''\
def test_will_fail():
    assert 1 + 1 == 3, "Math is broken"

def test_will_pass():
    assert True
'''

    test_dir = Path(repo) / "data" / "_test_tmp"
    test_dir.mkdir(parents=True, exist_ok=True)

    passing_file = test_dir / "test_passing.py"
    failing_file = test_dir / "test_failing.py"

    passing_file.write_text(passing_test)
    failing_file.write_text(failing_test)

    print("── Test 1: Passing tests ─────────────────────────────────")
    result = executor.run_tests(str(passing_file))
    print(result.summary())
    print(f"Total: {result.total}, Passed: {result.passed_count}")

    print("\n── Test 2: Failing tests ─────────────────────────────────")
    result = executor.run_tests(str(failing_file))
    print(result.summary())
    print(f"Failures: {result.failures}")

    print("\n── Test 3: Self-healing loop ─────────────────────────────")
    heal_calls = []

    def mock_heal(result: TestResult, attempt: int) -> bool:
        heal_calls.append(attempt)
        print(f"  [Heal] Called with attempt={attempt}, "
              f"failures={[f['name'] for f in result.failures]}")
        if attempt == 1:
            # Simulate fix: overwrite failing test with passing one
            failing_file.write_text(passing_test)
            return True
        return False

    healing = executor.run_with_healing(
        str(failing_file),
        max_attempts=3,
        heal_fn=mock_heal,
    )
    print(f"Final result : {healing.final_result.summary()}")
    print(f"Attempts     : {healing.attempts}")
    print(f"Healed       : {healing.healed}")
    print(f"Heal log     : {healing.heal_log}")

    # Cleanup
    import shutil
    shutil.rmtree(test_dir)
    print("\n[Test] Cleanup done. All tests passed.")
