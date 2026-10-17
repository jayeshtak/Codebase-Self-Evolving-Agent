import pytest
from agents.coder import CoderAgent

@pytest.fixture
def coder():
    return CoderAgent()

def test_fix_undefined_name_import_unused(coder):
    # Simulates database.py:1 [undefined_name] from pyflakes
    content = "from database import get_connection\n\ndef test():\n    return 42\n"
    issue = {
        "file": "database.py",
        "line": 1,
        "issue_type": "undefined_name",
        "description": ": 'database.get_connection' imported but unused"
    }
    fn, exp, code = coder._generate_deterministic_fix(content, issue)
    assert "get_connection" not in code
    assert "def test():" in code

def test_fix_undefined_name_missing_import(coder):
    # Simulates database.py:1 [undefined_name] when sqlite3 is undefined
    content = "def test():\n    return sqlite3.connect(':memory:')\n"
    issue = {
        "file": "database.py",
        "line": 1,
        "issue_type": "undefined_name",
        "description": "undefined name 'sqlite3'"
    }
    fn, exp, code = coder._generate_deterministic_fix(content, issue)
    assert code.startswith("import sqlite3\n")
    assert "def test():" in code

def test_fix_dead_code_unused_variable(coder):
    # Simulates auth.py:3 [dead_code] from vulture/pyflakes
    content = "import os\n\ndef check():\n    SECRET_KEY: str = 'supersecret'\n    return True\n"
    issue = {
        "file": "auth.py",
        "line": 4,
        "issue_type": "dead_code",
        "description": "Unused variable: 'SECRET_KEY'"
    }
    fn, exp, code = coder._generate_deterministic_fix(content, issue)
    assert "SECRET_KEY" not in code
    assert "return True" in code

def test_fix_dead_code_unused_function(coder):
    content = "def live():\n    return 1\n\ndef dead_fn(a: int) -> int:\n    \"\"\"dead doc\"\"\"\n    return a + 1\n"
    issue = {
        "file": "auth.py",
        "line": 4,
        "issue_type": "dead_code",
        "description": "Unused function: 'dead_fn'"
    }
    fn, exp, code = coder._generate_deterministic_fix(content, issue)
    assert "dead doc" not in code
    assert "return 1" in code
    assert "DEPRECATED" in code
