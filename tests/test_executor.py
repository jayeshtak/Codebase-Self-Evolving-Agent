"""Tests for core/executor.py — test runner + self-healing loop."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.executor import Executor, TestResult


@pytest.fixture()
def tmp_repo(tmp_path):
    return tmp_path


@pytest.fixture()
def executor(tmp_repo):
    return Executor(str(tmp_repo))


@pytest.fixture()
def passing_test(tmp_repo):
    f = tmp_repo / "test_passing.py"
    f.write_text(textwrap.dedent("""\
        def test_add():
            assert 1 + 1 == 2

        def test_upper():
            assert "hello".upper() == "HELLO"
    """))
    return str(f)


@pytest.fixture()
def failing_test(tmp_repo):
    f = tmp_repo / "test_failing.py"
    f.write_text(textwrap.dedent("""\
        def test_will_fail():
            assert 1 + 1 == 3, "math is broken"

        def test_will_pass():
            assert True
    """))
    return str(f)


class TestRunTests:
    def test_passing_tests(self, executor, passing_test):
        result = executor.run_tests(passing_test, framework="pytest")
        assert result.passed is True
        assert result.total == 2
        assert result.passed_count == 2
        assert result.failed_count == 0

    def test_failing_tests(self, executor, failing_test):
        result = executor.run_tests(failing_test, framework="pytest")
        assert result.passed is False
        assert result.failed_count >= 1

    def test_failure_details_populated(self, executor, failing_test):
        result = executor.run_tests(failing_test, framework="pytest")
        details = result.failure_details()
        assert len(details) > 0

    def test_no_tests_counts_as_passed(self, executor, tmp_repo):
        empty = tmp_repo / "test_empty.py"
        empty.write_text("# no tests\n")
        result = executor.run_tests(str(empty), framework="pytest")
        assert result.passed is True
        assert result.total == 0


class TestDetectFramework:
    def test_python_detected(self, executor):
        assert executor.detect_test_framework("main.py") == "pytest"
        assert executor.detect_test_framework("scanner.py") == "pytest"

    def test_js_detected(self, executor):
        assert executor.detect_test_framework("index.js") == "jest"
        assert executor.detect_test_framework("app.ts") == "jest"

    def test_go_detected(self, executor):
        assert executor.detect_test_framework("main.go") == "go"

    def test_rust_detected(self, executor):
        assert executor.detect_test_framework("lib.rs") == "cargo"

    def test_java_detected(self, executor, tmp_repo):
        # Without pom.xml — defaults to maven
        assert executor.detect_test_framework("Foo.java") == "maven"


class TestHealingLoop:
    def test_passes_first_try(self, executor, passing_test):
        result = executor.run_with_healing(passing_test, max_attempts=3, framework="pytest")
        assert result.final_result.passed is True
        assert result.attempts == 1
        assert result.healed is False

    def test_heals_on_failure(self, executor, failing_test, tmp_repo, passing_test):
        heal_calls = []

        def heal_fn(res, attempt):
            heal_calls.append(attempt)
            # Replace failing test with passing one on first heal
            Path(failing_test).write_text(Path(passing_test).read_text())
            return True

        result = executor.run_with_healing(
            failing_test, max_attempts=3, heal_fn=heal_fn, framework="pytest"
        )
        assert result.healed is True
        assert result.final_result.passed is True
        assert len(heal_calls) == 1

    def test_gives_up_after_max_attempts(self, executor, failing_test):
        heal_calls = []

        def bad_heal(res, attempt):
            heal_calls.append(attempt)
            return True  # claims to heal but doesn't actually fix anything

        result = executor.run_with_healing(
            failing_test, max_attempts=2, heal_fn=bad_heal, framework="pytest"
        )
        assert result.final_result.passed is False
        assert result.attempts == 2


class TestTestResult:
    def test_summary_passed(self):
        r = TestResult(passed=True, total=5, passed_count=5, duration=1.2)
        assert "5" in r.summary()
        assert "passed" in r.summary().lower() or "✓" in r.summary()

    def test_summary_failed(self):
        r = TestResult(passed=False, total=3, passed_count=2, failed_count=1, duration=0.5)
        assert "1" in r.summary()

    def test_failure_details_empty(self):
        r = TestResult(passed=True, stderr="")
        assert r.failure_details() == "(no details)" or r.failure_details() == ""
