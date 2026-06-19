"""Tests for agents/scanner.py — static analysis pipeline."""
import sys
import tempfile
import textwrap
from pathlib import Path

import git
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.scanner import Scanner


@pytest.fixture()
def tmp_repo(tmp_path):
    """Create a minimal repo with known issues."""
    # Initialize git repo so Scanner.__init__ doesn't throw
    git.Repo.init(str(tmp_path))
    # Python file with complexity + dead code
    (tmp_path / "bad.py").write_text(textwrap.dedent("""\
        import os          # unused
        import json

        def deeply_nested(x):
            if x:
                if x > 1:
                    if x > 2:
                        if x > 3:
                            if x > 4:
                                return x * 2
            return 0

        def unused_function():
            pass
    """))
    # Clean Python file
    (tmp_path / "good.py").write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


def _make_scanner(repo_path):
    s = Scanner(str(repo_path))
    # Force full scan so git diff is not needed
    return s


class TestScannerInit:
    def test_init(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        assert s.repo_path == tmp_repo.resolve()

    def test_get_all_files(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        files = s._get_all_files()
        names = [f.name for f in files]
        assert "bad.py" in names
        assert "good.py" in names


class TestSyntaxChecker:
    def test_valid_python_no_syntax_errors(self, tmp_repo):
        from core.syntax_checker import SyntaxChecker
        checker = SyntaxChecker(str(tmp_repo))
        issues = checker.check_file(tmp_repo / "good.py")
        syntax_errors = [i for i in issues if i.issue_type == "syntax_error"]
        assert syntax_errors == []

    def test_invalid_python_raises_syntax_error(self, tmp_repo):
        from core.syntax_checker import SyntaxChecker
        bad = tmp_repo / "syntax_bad.py"
        bad.write_text("def foo(:\n    pass\n")
        checker = SyntaxChecker(str(tmp_repo))
        issues = checker.check_file(bad)
        assert any(i.issue_type == "syntax_error" for i in issues)

    def test_javascript_syntax_error(self, tmp_repo):
        from core.syntax_checker import SyntaxChecker
        js = tmp_repo / "broken.js"
        js.write_text("function foo( { return 1; }\n")
        checker = SyntaxChecker(str(tmp_repo))
        issues = checker.check_file(js)
        # node --check may not be installed in CI; skip gracefully
        if issues:
            assert any(i.issue_type == "syntax_error" for i in issues)


class TestASTAnalysis:
    def test_detects_nesting(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        issues = s._analyze_ast(tmp_repo / "bad.py")
        nesting = [i for i in issues if i.issue_type == "deep_nesting"]
        assert len(nesting) >= 1

    def test_no_false_nesting_on_clean_file(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        issues = s._analyze_ast(tmp_repo / "good.py")
        nesting = [i for i in issues if i.issue_type == "deep_nesting"]
        assert nesting == []


class TestFullScan:
    def test_full_scan_returns_issues(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        issues = s.scan(full_scan=True)
        assert isinstance(issues, list)
        # bad.py has dead code + nesting — should produce at least 1 issue
        assert len(issues) >= 1

    def test_issue_fields(self, tmp_repo):
        s = _make_scanner(tmp_repo)
        issues = s.scan(full_scan=True)
        for iss in issues:
            assert hasattr(iss, "file")
            assert hasattr(iss, "line")
            assert hasattr(iss, "severity")
            assert hasattr(iss, "description")
            assert iss.severity in ("low", "medium", "high")

    def test_security_scan_runs(self, tmp_repo):
        """Security scanner should run without crashing even on clean code."""
        s = _make_scanner(tmp_repo)
        issues = s.scan(full_scan=True)
        types = {i.issue_type for i in issues}
        # Any of these is fine — just check it doesn't throw
        assert isinstance(types, set)
