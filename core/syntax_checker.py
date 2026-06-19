"""
Multi-language syntax and bug checker.
Runs language-native tools to catch:
  - Syntax errors (parse failures)
  - Undefined names / bad imports
  - Type errors
  - Basic logic bugs

Languages supported:
  Python    — py_compile (syntax) + pyflakes (undefined names/imports) + mypy (types)
  JS        — node --check (syntax) + eslint --no-eslintrc strict rules (bugs)
  TypeScript — tsc --noEmit (syntax + types)
  Go        — go vet (syntax + bugs)  [if go installed]
  Rust      — cargo check (syntax + types) [if cargo installed]
"""

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SyntaxIssue:
    file: str
    line: int
    col: int
    issue_type: str      # "syntax_error" | "undefined_name" | "type_error" | "bug"
    severity: str        # "high" for syntax, "medium" for bugs
    error_class: str
    description: str
    language: str

    def to_issue_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "language": self.language,
            "extra": {"error_class": self.error_class, "col": self.col},
        }


class SyntaxChecker:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _rel(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.repo_path))
        except ValueError:
            return path

    # ── Python ────────────────────────────────────────────────────────────────

    def check_python(self, filepath: Path) -> list[SyntaxIssue]:
        issues = []
        rel = self._rel(str(filepath))

        # 1. Syntax via ast.parse
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            ast.parse(source, filename=str(filepath))
        except SyntaxError as e:
            issues.append(SyntaxIssue(
                file=rel, line=e.lineno or 0, col=e.offset or 0,
                issue_type="syntax_error", severity="high",
                error_class="SyntaxError",
                description=f"SyntaxError: {e.msg}",
                language="python",
            ))
            return issues  # can't run further checks on broken syntax

        # 2. Pyflakes — undefined names, unused imports, redefined builtins
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pyflakes", str(filepath)],
                capture_output=True, text=True, cwd=str(self.repo_path)
            )
            for line in (result.stdout + result.stderr).splitlines():
                # Format: file.py:line:col: message
                m = re.match(r"(.+?):(\d+):(\d+)?\s*(.*)", line)
                if not m:
                    continue
                msg = m.group(4).strip()
                lineno = int(m.group(2))
                col = int(m.group(3)) if m.group(3) else 0
                issue_type = "undefined_name" if "undefined name" in msg or "imported but unused" in msg else "bug"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=col,
                    issue_type=issue_type, severity="high" if "undefined" in msg else "medium",
                    error_class="PyflakesError",
                    description=msg,
                    language="python",
                ))
        except Exception as e:
            print(f"[SyntaxChecker] pyflakes error on {rel}: {e}")

        # 3. Mypy — type errors
        try:
            result = subprocess.run(
                [sys.executable, "-m", "mypy", str(filepath),
                 "--ignore-missing-imports", "--no-error-summary",
                 "--no-pretty", "--show-column-numbers"],
                capture_output=True, text=True, cwd=str(self.repo_path)
            )
            for line in result.stdout.splitlines():
                m = re.match(r"(.+?):(\d+):(\d+):\s*(error|warning):\s*(.*)", line)
                if not m:
                    continue
                level = m.group(4)
                msg = m.group(5).strip()
                lineno = int(m.group(2))
                col = int(m.group(3))
                if level == "error":
                    issues.append(SyntaxIssue(
                        file=rel, line=lineno, col=col,
                        issue_type="type_error", severity="high",
                        error_class="TypeError",
                        description=f"[mypy] {msg}",
                        language="python",
                    ))
        except Exception as e:
            print(f"[SyntaxChecker] mypy error on {rel}: {e}")

        return issues

    # ── JavaScript ────────────────────────────────────────────────────────────

    def check_javascript(self, filepath: Path) -> list[SyntaxIssue]:
        issues = []
        rel = self._rel(str(filepath))

        # 1. Node syntax check
        try:
            result = subprocess.run(
                ["node", "--check", str(filepath)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                stderr = result.stderr or result.stdout
                # Extract the main error line and line number
                lineno = 0
                msg = "SyntaxError"
                for line in stderr.splitlines():
                    # "file.js:LINE" pattern
                    m = re.search(r"[^:]+:(\d+)$", line.strip())
                    if m:
                        lineno = int(m.group(1))
                    # "SyntaxError: <message>"
                    if line.strip().startswith("SyntaxError:"):
                        msg = line.strip()
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=0,
                    issue_type="syntax_error", severity="high",
                    error_class="SyntaxError",
                    description=msg[:150],
                    language="javascript",
                ))
                return issues  # broken syntax → skip further checks
        except Exception as e:
            print(f"[SyntaxChecker] node --check error on {rel}: {e}")

        # 2. ESLint deep bug rules
        eslint_rules = json.dumps({
            "no-undef": "error",
            "no-unused-vars": "warn",
            "no-unreachable": "error",
            "no-constant-condition": "error",
            "no-dupe-keys": "error",
            "no-duplicate-case": "error",
            "no-empty": "warn",
            "no-ex-assign": "error",
            "no-func-assign": "error",
            "no-import-assign": "error",
            "no-obj-duplicate-keys": "error",
            "use-isnan": "error",
            "valid-typeof": "error",
            "eqeqeq": "warn",
            "no-var": "warn",
            "prefer-const": "warn",
        })
        try:
            result = subprocess.run(
                ["npx", "eslint", "--format", "json",
                 "--rule", eslint_rules,
                 "--env", "es2020,node,browser",
                 str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path),
                timeout=30,
                env={**os.environ, "ESLINT_USE_FLAT_CONFIG": "false"},
            )
            raw = result.stdout.strip()
            if raw:
                data = json.loads(raw)
                sev_map = {1: "medium", 2: "high"}
                type_map = {
                    "no-undef": "undefined_name",
                    "no-unused-vars": "dead_code",
                    "no-unreachable": "bug",
                    "no-constant-condition": "bug",
                    "no-dupe-keys": "bug",
                    "no-duplicate-case": "bug",
                    "use-isnan": "bug",
                    "valid-typeof": "bug",
                }
                for fr in data:
                    for msg in fr.get("messages", []):
                        rule = msg.get("ruleId") or "eslint"
                        issues.append(SyntaxIssue(
                            file=rel,
                            line=msg.get("line", 0),
                            col=msg.get("column", 0),
                            issue_type=type_map.get(rule, "style"),
                            severity=sev_map.get(msg.get("severity", 1), "medium"),
                            error_class=rule,
                            description=f"[{rule}] {msg.get('message','')}",
                            language="javascript",
                        ))
        except Exception as e:
            print(f"[SyntaxChecker] ESLint error on {rel}: {e}")

        return issues

    # ── TypeScript ────────────────────────────────────────────────────────────

    def check_typescript(self, filepath: Path) -> list[SyntaxIssue]:
        issues = []
        rel = self._rel(str(filepath))
        try:
            result = subprocess.run(
                ["tsc", "--noEmit", "--allowJs", "--checkJs",
                 "--strict", "--skipLibCheck",
                 "--target", "es2020",
                 str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=30,
            )
            for line in result.stdout.splitlines():
                # Format: file.ts(line,col): error TSxxxx: message
                m = re.match(r".+?\((\d+),(\d+)\):\s*(error|warning)\s+TS\d+:\s*(.*)", line)
                if not m:
                    continue
                level = m.group(3)
                msg = m.group(4).strip()
                lineno = int(m.group(1))
                col = int(m.group(2))
                issue_type = "syntax_error" if "syntax" in msg.lower() else "type_error"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=col,
                    issue_type=issue_type,
                    severity="high" if level == "error" else "medium",
                    error_class="TypeScriptError",
                    description=f"[tsc] {msg}",
                    language="typescript",
                ))
        except FileNotFoundError:
            pass  # tsc not installed
        except Exception as e:
            print(f"[SyntaxChecker] tsc error on {rel}: {e}")
        return issues

    # ── Go ────────────────────────────────────────────────────────────────────

    def check_go(self, filepath: Path) -> list[SyntaxIssue]:
        issues = []
        rel = self._rel(str(filepath))
        try:
            result = subprocess.run(
                ["go", "vet", str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=30,
            )
            for line in (result.stdout + result.stderr).splitlines():
                m = re.match(r"(.+?):(\d+):(\d+)?:?\s*(.*)", line)
                if not m or not m.group(4):
                    continue
                msg = m.group(4).strip()
                lineno = int(m.group(2))
                issue_type = "syntax_error" if "syntax" in msg.lower() else "bug"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=0,
                    issue_type=issue_type,
                    severity="high" if issue_type == "syntax_error" else "medium",
                    error_class="GoVetError",
                    description=f"[go vet] {msg}",
                    language="go",
                ))
        except FileNotFoundError:
            pass  # go not installed
        except Exception as e:
            print(f"[SyntaxChecker] go vet error on {rel}: {e}")
        return issues

    # ── Rust ──────────────────────────────────────────────────────────────────

    def check_rust(self, filepath: Path) -> list[SyntaxIssue]:
        issues = []
        rel = self._rel(str(filepath))
        try:
            result = subprocess.run(
                ["cargo", "check", "--message-format=json"],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=60,
            )
            for line in result.stdout.splitlines():
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("reason") != "compiler-message":
                    continue
                diag = msg.get("message", {})
                if diag.get("level") not in ("error", "warning"):
                    continue
                spans = diag.get("spans", [])
                if not spans:
                    continue
                span = spans[0]
                span_file = span.get("file_name", "")
                if str(filepath.name) not in span_file:
                    continue
                lineno = span.get("line_start", 0)
                text = diag.get("message", "")
                issue_type = "syntax_error" if "syntax" in text.lower() else "type_error" if "type" in text.lower() else "bug"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=span.get("column_start", 0),
                    issue_type=issue_type,
                    severity="high" if diag["level"] == "error" else "medium",
                    error_class="RustError",
                    description=f"[cargo] {text}",
                    language="rust",
                ))
        except FileNotFoundError:
            pass  # cargo not installed
        except Exception as e:
            print(f"[SyntaxChecker] cargo error on {rel}: {e}")
        return issues

    # ── Java ──────────────────────────────────────────────────────────────────

    def check_java(self, filepath: Path) -> list[SyntaxIssue]:
        """javac for syntax + type errors. Works on a single file."""
        issues = []
        rel = self._rel(str(filepath))
        try:
            result = subprocess.run(
                ["javac", "-Xlint:all", str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=30,
            )
            for line in (result.stdout + result.stderr).splitlines():
                # Format: File.java:LINE: error: message
                m = re.match(r"(.+?):(\d+):\s*(error|warning):\s*(.*)", line)
                if not m:
                    continue
                level = m.group(3)
                msg = m.group(4).strip()
                lineno = int(m.group(2))
                issue_type = "syntax_error" if "illegal" in msg.lower() or "';' expected" in msg or "reached end" in msg else "type_error" if "cannot find symbol" in msg or "incompatible types" in msg else "bug"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=0,
                    issue_type=issue_type,
                    severity="high" if level == "error" else "medium",
                    error_class="JavacError",
                    description=f"[javac] {msg}",
                    language="java",
                ))
        except FileNotFoundError:
            pass  # javac not installed
        except Exception as e:
            print(f"[SyntaxChecker] javac error on {rel}: {e}")
        return issues

    # ── C++ ───────────────────────────────────────────────────────────────────

    def check_cpp(self, filepath: Path) -> list[SyntaxIssue]:
        """g++ -fsyntax-only — syntax + type checking without linking."""
        issues = []
        rel = self._rel(str(filepath))
        try:
            result = subprocess.run(
                ["g++", "-fsyntax-only", "-Wall", "-Wextra",
                 "-std=c++17", str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=30,
            )
            for line in (result.stdout + result.stderr).splitlines():
                # Format: file.cpp:LINE:COL: error/warning: message
                m = re.match(r"(.+?):(\d+):(\d+):\s*(error|warning|note):\s*(.*)", line)
                if not m:
                    continue
                level = m.group(4)
                if level == "note":
                    continue
                msg = m.group(5).strip()
                lineno = int(m.group(2))
                col = int(m.group(3))
                issue_type = "syntax_error" if level == "error" and any(
                    k in msg.lower() for k in ["expected", "error:", "before", "undeclared"]
                ) else "type_error" if "cannot convert" in msg or "no matching" in msg else "bug"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=col,
                    issue_type=issue_type,
                    severity="high" if level == "error" else "medium",
                    error_class="GppError",
                    description=f"[g++] {msg}",
                    language="cpp",
                ))
        except FileNotFoundError:
            pass  # g++ not installed
        except Exception as e:
            print(f"[SyntaxChecker] g++ error on {rel}: {e}")
        return issues

    # ── PHP / Laravel ─────────────────────────────────────────────────────────

    def check_php(self, filepath: Path) -> list[SyntaxIssue]:
        """
        php -l  — syntax check.
        PHPStan — undefined vars, type errors, dead code (Laravel-aware at level 5).
        """
        issues = []
        rel = self._rel(str(filepath))

        # 1. Syntax check via php -l
        try:
            result = subprocess.run(
                ["php", "-l", str(filepath)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                output = result.stdout + result.stderr
                m = re.search(r"(Parse error|Fatal error).*?on line (\d+)", output)
                lineno = int(m.group(2)) if m else 0
                # First non-empty line usually has "Parse error: ... on line N"
                lines = [l for l in output.strip().splitlines() if l.strip() and "Errors parsing" not in l]
                msg = lines[0] if lines else "PHP syntax error"
                issues.append(SyntaxIssue(
                    file=rel, line=lineno, col=0,
                    issue_type="syntax_error", severity="high",
                    error_class="PHPSyntaxError",
                    description=f"[php -l] {msg[:150]}",
                    language="php",
                ))
                return issues  # skip PHPStan on broken syntax
        except Exception as e:
            print(f"[SyntaxChecker] php -l error on {rel}: {e}")

        # 2. PHPStan static analysis
        phpstan = self._find_phpstan()
        if not phpstan:
            return issues
        try:
            result = subprocess.run(
                [phpstan, "analyse", "--error-format=json",
                 "--level=5", "--no-progress", str(filepath)],
                capture_output=True, text=True,
                cwd=str(self.repo_path), timeout=60,
            )
            raw = result.stdout.strip()
            if not raw:
                return issues
            # PHPStan outputs JSON to stdout
            data = json.loads(raw)
            for file_errors in data.get("files", {}).values():
                for err in file_errors.get("messages", []):
                    msg = err.get("message", "")
                    lineno = err.get("line", 0)
                    issue_type = (
                        "undefined_name" if "Undefined variable" in msg or "not found" in msg
                        else "type_error" if "expects" in msg or "type" in msg.lower()
                        else "bug"
                    )
                    issues.append(SyntaxIssue(
                        file=rel, line=lineno, col=0,
                        issue_type=issue_type, severity="high",
                        error_class="PHPStanError",
                        description=f"[phpstan] {msg}",
                        language="php",
                    ))
        except Exception as e:
            print(f"[SyntaxChecker] phpstan error on {rel}: {e}")

        return issues

    def _find_phpstan(self) -> Optional[str]:
        """Locate phpstan binary — global composer, local vendor, or PATH."""
        candidates = [
            Path.home() / ".config/composer/vendor/bin/phpstan",
            Path.home() / ".composer/vendor/bin/phpstan",
            self.repo_path / "vendor/bin/phpstan",
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        result = subprocess.run(["which", "phpstan"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def check_file(self, filepath: Path) -> list[SyntaxIssue]:
        ext = filepath.suffix.lower()
        if ext == ".py":
            return self.check_python(filepath)
        elif ext in (".js", ".jsx", ".mjs"):
            return self.check_javascript(filepath)
        elif ext in (".ts", ".tsx"):
            return self.check_typescript(filepath)
        elif ext == ".go":
            return self.check_go(filepath)
        elif ext == ".rs":
            return self.check_rust(filepath)
        elif ext == ".java":
            return self.check_java(filepath)
        elif ext in (".cpp", ".cxx", ".cc", ".c"):
            return self.check_cpp(filepath)
        elif ext == ".php":
            return self.check_php(filepath)
        return []

    def check_files(self, files: list[Path]) -> list[SyntaxIssue]:
        all_issues = []
        for f in files:
            issues = self.check_file(f)
            if issues:
                print(f"[SyntaxChecker] {f.name}: {len(issues)} issue(s)")
            all_issues.extend(issues)
        return all_issues
