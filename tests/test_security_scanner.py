"""Tests for core/security_scanner.py."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.security_scanner import SecurityScanner, scan_bandit


@pytest.fixture()
def tmp_repo(tmp_path):
    # File with a known bandit issue: hardcoded password
    (tmp_path / "insecure.py").write_text(textwrap.dedent("""\
        import subprocess

        password = "hunter2"   # B105: hardcoded password

        def run_cmd(cmd):
            subprocess.call(cmd, shell=True)   # B602: shell=True
    """))
    (tmp_path / "clean.py").write_text("def add(a, b):\n    return a + b\n")
    return tmp_path


class TestBandit:
    def test_finds_hardcoded_password(self, tmp_repo):
        issues = scan_bandit(str(tmp_repo), list(tmp_repo.glob("*.py")))
        descs = [i["description"] for i in issues]
        # bandit B105 or B106 for hardcoded password
        assert any("B10" in d or "password" in d.lower() or "hardcoded" in d.lower()
                   for d in descs), f"Expected password issue, got: {descs}"

    def test_returns_correct_fields(self, tmp_repo):
        issues = scan_bandit(str(tmp_repo), list(tmp_repo.glob("*.py")))
        for iss in issues:
            assert "file" in iss
            assert "line" in iss
            assert "severity" in iss
            assert iss["severity"] in ("low", "medium", "high")
            assert iss["issue_type"] == "security"

    def test_clean_file_no_issues(self, tmp_repo):
        issues = scan_bandit(str(tmp_repo), [tmp_repo / "clean.py"])
        assert issues == []

    def test_empty_file_list(self, tmp_repo):
        issues = scan_bandit(str(tmp_repo), [])
        assert issues == []


class TestSecurityScanner:
    def test_scan_returns_list(self, tmp_repo):
        scanner = SecurityScanner(str(tmp_repo))
        issues = scanner.scan(list(tmp_repo.glob("*.py")))
        assert isinstance(issues, list)

    def test_no_duplicate_issues(self, tmp_repo):
        scanner = SecurityScanner(str(tmp_repo))
        issues = scanner.scan(list(tmp_repo.glob("*.py")))
        keys = [(i["file"], i["line"], i["description"][:60]) for i in issues]
        assert len(keys) == len(set(keys)), "Duplicate issues found"

    def test_fix_strategy_present(self, tmp_repo):
        scanner = SecurityScanner(str(tmp_repo))
        issues = scanner.scan(list(tmp_repo.glob("*.py")))
        for iss in issues:
            assert "fix_strategy" in iss
            assert len(iss["fix_strategy"]) > 0
