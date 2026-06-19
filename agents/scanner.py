import os
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import git
import radon.complexity as radon_cc
import radon.metrics as radon_mi
from radon.visitors import ComplexityVisitor
from vulture import Vulture
from tree_sitter_languages import get_language, get_parser


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    file: str
    line: int
    issue_type: str        # "complexity" | "dead_code" | "duplicate" | "style"
    severity: str          # "low" | "medium" | "high"
    description: str
    language: str = "python"
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "file": self.file,
            "line": self.line,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "language": self.language,
            "extra": self.extra,
        }


# ── ESLint rule → issue_type mapping ─────────────────────────────────────────

def _eslint_rule_to_type(rule_id: str) -> str:
    dead_code_rules = {"no-unused-vars", "no-unreachable", "no-unused-expressions"}
    complexity_rules = {"complexity", "max-depth", "max-lines-per-function"}
    security_rules   = {"no-eval", "no-implied-eval", "no-new-func"}
    if rule_id in dead_code_rules:
        return "dead_code"
    if rule_id in complexity_rules:
        return "complexity"
    if rule_id in security_rules:
        return "security"
    return "style"


# ── Language map for tree-sitter ──────────────────────────────────────────────

EXTENSION_TO_LANGUAGE = {
    ".py":   "python",
    ".js":   "javascript",
    ".ts":   "typescript",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".cpp":  "cpp",
    ".cxx":  "cpp",
    ".cc":   "cpp",
    ".c":    "c",
    ".php":  "php",
}


# ── Scanner ───────────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.repo = git.Repo(self.repo_path)
        from core.agent_config import load_agent_config
        self.cfg = load_agent_config(str(self.repo_path))

    # ── Git: get changed files since last commit ──────────────────────────────

    def get_changed_files(self) -> list[Path]:
        """Return files changed since last commit. Falls back to all files."""
        try:
            diff = self.repo.index.diff("HEAD") + self.repo.index.diff(None)
            changed = [self.repo_path / item.a_path for item in diff]

            # Also include untracked files
            untracked = [self.repo_path / f for f in self.repo.untracked_files]
            all_files = list(set(changed + untracked))

            # Filter to supported extensions only
            supported = [
                f for f in all_files
                if f.suffix in EXTENSION_TO_LANGUAGE and f.exists()
            ]

            if not supported:
                print("[Scanner] No changed files found, scanning all supported files.")
                return self._get_all_files()

            return supported

        except Exception as e:
            print(f"[Scanner] Git diff failed ({e}), scanning all files.")
            return self._get_all_files()

    def _expand_with_dependents(self, files: list[Path]) -> list[Path]:
        """
        Given a list of changed files, also include their direct dependents
        from the dependency graph (files that import the changed files).
        Keeps the result deduplicated and bounded to supported extensions.
        """
        try:
            from core.graph import DependencyGraph
            dg = DependencyGraph(str(self.repo_path))
            dg.build()

            extra: set[Path] = set()
            for f in files:
                rel = str(f.relative_to(self.repo_path))
                impact = dg.get_impact_radius(rel)
                for dep in impact.get("direct_dependents", []):
                    dep_path = self.repo_path / dep
                    if dep_path.exists() and dep_path.suffix in EXTENSION_TO_LANGUAGE:
                        extra.add(dep_path)

            added = extra - set(files)
            if added:
                print(f"[Scanner] Graph-aware: adding {len(added)} dependent file(s) to scan.")
            return list(set(files) | added)
        except Exception as e:
            print(f"[Scanner] Graph expansion failed ({e}), using original file list.")
            return files

    def _get_all_files(self) -> list[Path]:
        files = []
        for ext in EXTENSION_TO_LANGUAGE:
            files.extend(self.repo_path.rglob(f"*{ext}"))
        return [
            f for f in files
            if not self.cfg.is_path_excluded(str(f.relative_to(self.repo_path)))
            and "venv" not in f.parts
            and "node_modules" not in f.parts
            and not any(part.startswith(".") for part in f.parts)
        ]

    # ── Radon: cyclomatic complexity ──────────────────────────────────────────

    def _analyze_complexity(self, filepath: Path) -> list[Issue]:
        issues = []
        if filepath.suffix != ".py":
            return issues
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            results = ComplexityVisitor.from_code(source)
            for block in results.functions + results.classes:
                complexity = block.complexity
                if complexity >= 10:
                    severity = "high" if complexity >= 20 else "medium"
                    issues.append(Issue(
                        file=str(filepath.relative_to(self.repo_path)),
                        line=block.lineno,
                        issue_type="complexity",
                        severity=severity,
                        description=(
                            f"'{block.name}' has cyclomatic complexity {complexity} "
                            f"({'very high' if complexity >= 20 else 'high'}, threshold=10)"
                        ),
                        language="python",
                        extra={"complexity": complexity, "block": block.name},
                    ))
        except Exception as e:
            print(f"[Scanner] Radon error on {filepath}: {e}")
        return issues

    # ── Vulture: dead code ────────────────────────────────────────────────────

    def _analyze_dead_code(self, filepath: Path) -> list[Issue]:
        issues = []
        if filepath.suffix != ".py":
            return issues
        try:
            v = Vulture()
            v.scavenge([str(filepath)])
            for item in v.get_unused_code():
                issues.append(Issue(
                    file=str(filepath.relative_to(self.repo_path)),
                    line=item.first_lineno,
                    issue_type="dead_code",
                    severity="low",
                    description=f"Unused {item.typ}: '{item.name}'",
                    language="python",
                    extra={"name": item.name, "typ": item.typ},
                ))
        except Exception as e:
            print(f"[Scanner] Vulture error on {filepath}: {e}")
        return issues

    # ── tree-sitter: multi-language AST checks ────────────────────────────────

    def _analyze_ast(self, filepath: Path) -> list[Issue]:
        issues = []
        lang_name = EXTENSION_TO_LANGUAGE.get(filepath.suffix)
        if not lang_name:
            return issues
        try:
            parser = get_parser(lang_name)
            source = filepath.read_bytes()
            tree = parser.parse(source)
            lines = source.decode("utf-8", errors="ignore").splitlines()

            # Check 1: deeply nested functions (nesting > 4)
            issues += self._check_nesting(tree, filepath, lines, lang_name)

            # Check 2: very long functions (> 60 lines)
            issues += self._check_long_functions(tree, filepath, lines, lang_name)

        except Exception as e:
            print(f"[Scanner] tree-sitter error on {filepath}: {e}")
        return issues

    def _check_nesting(self, tree, filepath, lines, lang_name) -> list[Issue]:
        issues = []

        # Control flow node types per language
        nesting_nodes = {
            "python":     {"if_statement", "for_statement", "while_statement", "try_statement", "with_statement"},
            "javascript": {"if_statement", "for_statement", "for_in_statement", "for_of_statement", "while_statement", "do_statement", "try_statement", "switch_statement"},
            "typescript": {"if_statement", "for_statement", "for_in_statement", "for_of_statement", "while_statement", "do_statement", "try_statement", "switch_statement"},
            "java":       {"if_statement", "for_statement", "enhanced_for_statement", "while_statement", "do_statement", "try_statement", "switch_expression"},
            "cpp":        {"if_statement", "for_statement", "range_based_for_statement", "while_statement", "do_statement", "try_statement", "switch_statement"},
            "c":          {"if_statement", "for_statement", "while_statement", "do_statement", "switch_statement"},
            "php":        {"if_statement", "for_statement", "foreach_statement", "while_statement", "do_statement", "try_statement", "switch_statement"},
            "go":         {"if_statement", "for_statement", "type_switch_statement", "select_statement"},
            "rust":       {"if_expression", "for_expression", "while_expression", "match_expression", "loop_expression"},
        }
        target = nesting_nodes.get(lang_name, set())

        def get_depth(node, depth=0):
            if node.type in target:
                depth += 1
            if depth > 4:
                issues.append(Issue(
                    file=str(filepath.relative_to(self.repo_path)),
                    line=node.start_point[0] + 1,
                    issue_type="deep_nesting",
                    severity="medium",
                    description=f"Deeply nested block (depth {depth}) — consider refactoring",
                    language=lang_name,
                    extra={"depth": depth},
                ))
                return
            for child in node.children:
                get_depth(child, depth)

        get_depth(tree.root_node)
        return issues

    def _check_long_functions(self, tree, filepath, lines, lang_name) -> list[Issue]:
        issues = []
        fn_types = {
            "python":     ["function_definition"],
            "javascript": ["function_declaration", "arrow_function", "method_definition", "function_expression"],
            "typescript": ["function_declaration", "arrow_function", "method_definition", "function_expression"],
            "java":       ["method_declaration", "constructor_declaration"],
            "cpp":        ["function_definition"],
            "c":          ["function_definition"],
            "php":        ["function_definition", "method_declaration"],
            "go":         ["function_declaration", "method_declaration"],
            "rust":       ["function_item"],
        }
        target_types = fn_types.get(lang_name, [])

        def walk(node):
            if node.type in target_types:
                start = node.start_point[0]
                end = node.end_point[0]
                length = end - start
                if length > 60:
                    # Try to get function name
                    name = "unknown"
                    for child in node.children:
                        if child.type == "identifier":
                            name = child.text.decode("utf-8", errors="ignore")
                            break
                    issues.append(Issue(
                        file=str(filepath.relative_to(self.repo_path)),
                        line=start + 1,
                        issue_type="style",
                        severity="low",
                        description=(
                            f"Function '{name}' is {length} lines long — "
                            f"consider breaking it up (threshold=60)"
                        ),
                        language=lang_name,
                        extra={"length": length, "name": name},
                    ))
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return issues

    # ── Hardcoded secrets detection ───────────────────────────────────────────

    def _check_hardcoded_secrets(self, filepath: Path) -> list[Issue]:
        import re
        issues = []
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            patterns = [
                (r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{4,}["\']', "Hardcoded password"),
                (r'(?i)(api_key|apikey|api_secret)\s*=\s*["\'][^"\']{8,}["\']', "Hardcoded API key"),
                (r'(?i)(secret|token|auth_token)\s*=\s*["\'][^"\']{8,}["\']', "Hardcoded secret/token"),
                (r'(?i)(aws_access_key_id|aws_secret)\s*=\s*["\'][^"\']{8,}["\']', "Hardcoded AWS credential"),
            ]
            for lineno, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if "os.getenv" in line or "environ" in line or "getenv" in line:
                    continue
                if "your_" in line.lower() or "example" in line.lower() or "placeholder" in line.lower():
                    continue
                for pattern, label in patterns:
                    if re.search(pattern, line):
                        issues.append(Issue(
                            file=str(filepath.relative_to(self.repo_path)),
                            line=lineno,
                            issue_type="security",
                            severity="high",
                            description=f"{label} detected — move to environment variable",
                            language=EXTENSION_TO_LANGUAGE.get(filepath.suffix, "unknown"),
                            extra={"pattern": "hardcoded_secret"},
                        ))
                        break
        except Exception as e:
            print(f"[Scanner] Secret detection error on {filepath}: {e}")
        return issues

    # ── Missing type hints (Python only) ─────────────────────────────────────

    def _check_missing_type_hints(self, filepath: Path) -> list[Issue]:
        import ast as _ast
        if filepath.suffix != ".py":
            return []
        issues = []
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            tree = _ast.parse(source)
            count = 0
            for node in _ast.walk(tree):
                if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_") or node.name in ("setUp", "tearDown"):
                    continue
                args = node.args
                all_args = args.args + args.posonlyargs + args.kwonlyargs
                missing = [a.arg for a in all_args if a.arg != "self" and a.annotation is None]
                no_return = node.returns is None
                if missing or no_return:
                    parts = []
                    if missing:
                        parts.append(f"args {missing}")
                    if no_return:
                        parts.append("return type")
                    issues.append(Issue(
                        file=str(filepath.relative_to(self.repo_path)),
                        line=node.lineno,
                        issue_type="missing_type_hints",
                        severity="low",
                        description=f"Function '{node.name}' missing type hints: {', '.join(parts)}",
                        language="python",
                        extra={"name": node.name, "missing_args": missing},
                    ))
                    count += 1
                    if count >= 10:
                        break
        except Exception as e:
            print(f"[Scanner] Type hint check error on {filepath}: {e}")
        return issues

    # ── SQL injection pattern detection ──────────────────────────────────────

    def _check_sql_injection(self, filepath: Path) -> list[Issue]:
        import re
        if filepath.suffix not in (".py", ".js", ".ts", ".php"):
            return []
        issues = []
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            sql_patterns = [
                r'(?i)(SELECT|INSERT|UPDATE|DELETE|DROP).{0,60}["\'\s]\s*\+\s*\w',
                r'(?i)(SELECT|INSERT|UPDATE|DELETE|DROP).{0,60}%s.{0,40}%\s*[\(\[\{]',
                r'(?i)(SELECT|INSERT|UPDATE|DELETE|DROP).{0,60}f["\'].{0,60}\{',
            ]
            for lineno, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                for pattern in sql_patterns:
                    if re.search(pattern, line):
                        issues.append(Issue(
                            file=str(filepath.relative_to(self.repo_path)),
                            line=lineno,
                            issue_type="security",
                            severity="high",
                            description=(
                                "Possible SQL injection: query built with string interpolation "
                                "— use parameterized queries"
                            ),
                            language=EXTENSION_TO_LANGUAGE.get(filepath.suffix, "unknown"),
                            extra={"pattern": "sql_injection"},
                        ))
                        break
        except Exception as e:
            print(f"[Scanner] SQL injection check error on {filepath}: {e}")
        return issues

    # ── ESLint: JavaScript / TypeScript static analysis ───────────────────────

    def _analyze_js_eslint(self, js_files: list[Path]) -> list[Issue]:
        """
        Run ESLint on JS/TS files via npx. Returns issues parsed from JSON output.
        Skips silently if ESLint/npx is not available.
        """
        if not js_files:
            return []

        # Check npx is available
        npx = subprocess.run(["which", "npx"], capture_output=True, text=True)
        if npx.returncode != 0:
            print("[Scanner] npx not found — skipping ESLint analysis.")
            return []

        file_paths = [str(f) for f in js_files]
        eslint_rules = json.dumps({
            "no-unused-vars": "warn",
            "no-undef": "error",
            "no-console": "warn",
            "no-unreachable": "error",
            "eqeqeq": "warn",
            "no-var": "warn",
            "prefer-const": "warn",
        })
        eslint_envs = "es2020,node,browser"

        try:
            result = subprocess.run(
                ["npx", "eslint", "--format", "json",
                 "--rule", eslint_rules,
                 "--env", eslint_envs] + file_paths,
                capture_output=True, text=True,
                cwd=str(self.repo_path),
                timeout=60,
                env={**os.environ, "ESLINT_USE_FLAT_CONFIG": "false"},
            )
            # ESLint exits non-zero when there are warnings/errors — that's expected
            stderr_clean = "\n".join(
                l for l in result.stderr.splitlines()
                if "ESLintRCWarning" not in l and "DeprecationWarning" not in l
                and "(node:" not in l and "Use `node" not in l
            )
            if stderr_clean.strip():
                print(f"[Scanner] ESLint stderr: {stderr_clean[:200]}")

            raw = result.stdout.strip()
            if not raw:
                return []

            data = json.loads(raw)
        except subprocess.TimeoutExpired:
            print("[Scanner] ESLint timed out.")
            return []
        except Exception as e:
            print(f"[Scanner] ESLint failed: {e}")
            return []

        issues = []
        severity_map = {1: "low", 2: "high"}  # ESLint: 1=warn, 2=error

        for file_result in data:
            fpath_str = file_result.get("filePath", "")
            try:
                rel = str(Path(fpath_str).relative_to(self.repo_path))
            except ValueError:
                rel = fpath_str

            lang = "typescript" if fpath_str.endswith((".ts", ".tsx")) else "javascript"

            for msg in file_result.get("messages", []):
                rule_id = msg.get("ruleId") or "eslint"
                sev = severity_map.get(msg.get("severity", 1), "low")
                issue_type = _eslint_rule_to_type(rule_id)
                issues.append(Issue(
                    file=rel,
                    line=msg.get("line", 0),
                    issue_type=issue_type,
                    severity=sev,
                    description=f"[{rule_id}] {msg.get('message', '')}",
                    language=lang,
                    extra={"rule": rule_id, "column": msg.get("column", 0)},
                ))

        print(f"[Scanner] ESLint: {len(issues)} issue(s) in {len(js_files)} JS/TS file(s).")
        return issues

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self, full_scan: bool = False) -> list[Issue]:
        from core.syntax_checker import SyntaxChecker
        from core.security_scanner import SecurityScanner

        files = self._get_all_files() if full_scan else self.get_changed_files()

        # ── Graph-aware incremental expansion ────────────────────────────────
        # For non-full scans, also re-scan files that *import* changed files,
        # because a change can break its dependents even if they weren't edited.
        if not full_scan and files:
            files = self._expand_with_dependents(files)

        print(f"[Scanner] Scanning {len(files)} file(s)...")

        all_issues: list[Issue] = []

        # ── Pass 1: Syntax + bug checker (all languages) ──────────────────────
        checker = SyntaxChecker(str(self.repo_path))
        syntax_issues = checker.check_files(files)
        syntax_files_with_errors = set()
        for si in syntax_issues:
            all_issues.append(Issue(
                file=si.file,
                line=si.line,
                issue_type=si.issue_type,
                severity=si.severity,
                description=si.description,
                language=si.language,
                extra={"error_class": si.error_class, "col": si.col},
            ))
            if si.issue_type == "syntax_error":
                syntax_files_with_errors.add(si.file)

        # ── Pass 2: Python static analysis (skip files with syntax errors) ───
        for filepath in files:
            rel = str(filepath.relative_to(self.repo_path))
            if rel in syntax_files_with_errors:
                print(f"  → {rel} [skipping static analysis — syntax errors present]")
                continue
            print(f"  → {rel}")
            all_issues += self._analyze_complexity(filepath)
            all_issues += self._analyze_dead_code(filepath)
            all_issues += self._analyze_ast(filepath)

        # ── Pass 3: JS/TS ESLint batch (skip files already checked by tsc/node) ─
        js_files = [
            f for f in files
            if f.suffix in (".js", ".jsx", ".mjs", ".ts", ".tsx")
            and str(f.relative_to(self.repo_path)) not in syntax_files_with_errors
        ]
        if js_files:
            eslint_issues = self._analyze_js_eslint(js_files)
            existing = {(i.file, i.line) for i in all_issues}
            for issue in eslint_issues:
                if (issue.file, issue.line) not in existing:
                    all_issues.append(issue)
                    existing.add((issue.file, issue.line))

        # ── Pass 4: Security scanning (Bandit + Semgrep + npm audit + safety) ─
        print(f"[Scanner] Running security scan...")
        sec_scanner = SecurityScanner(str(self.repo_path))
        sec_issues_raw = sec_scanner.scan(files)
        existing = {(i.file, i.line, i.description[:60]) for i in all_issues}
        for iss in sec_issues_raw:
            key = (iss["file"], iss["line"], iss["description"][:60])
            if key not in existing:
                existing.add(key)
                all_issues.append(Issue(
                    file=iss["file"],
                    line=iss["line"],
                    issue_type="security",
                    severity=iss["severity"],
                    description=iss["description"],
                    language=iss.get("language", "unknown"),
                    extra=iss.get("extra", {}),
                ))

        # ── Pass 5: Duplicate / clone detection ──────────────────────────────
        print(f"[Scanner] Running duplicate code detection...")
        from core.duplicate_detector import DuplicateDetector
        dup_detector = DuplicateDetector(str(self.repo_path))
        dup_issues_raw = dup_detector.scan(files)
        existing_keys = {(i.file, i.line, i.description[:60]) for i in all_issues}
        for iss in dup_issues_raw:
            key = (iss["file"], iss["line"], iss["description"][:60])
            if key not in existing_keys:
                existing_keys.add(key)
                all_issues.append(Issue(
                    file=iss["file"],
                    line=iss["line"],
                    issue_type="duplicate",
                    severity="low",
                    description=iss["description"],
                    language=iss.get("language", "unknown"),
                    extra=iss.get("extra", {}),
                ))

        # ── Pass 6: Hardcoded secrets + SQL injection + missing type hints ───────
        existing_keys = {(i.file, i.line, i.description[:60]) for i in all_issues}
        for filepath in files:
            rel = str(filepath.relative_to(self.repo_path))
            if rel in syntax_files_with_errors:
                continue
            extra_issues = (
                self._check_hardcoded_secrets(filepath)
                + self._check_sql_injection(filepath)
                + self._check_missing_type_hints(filepath)
            )
            for iss in extra_issues:
                key = (iss.file, iss.line, iss.description[:60])
                if key not in existing_keys:
                    existing_keys.add(key)
                    all_issues.append(iss)

        # ── Apply config filters ──────────────────────────────────────────────
        # 1. Severity threshold
        all_issues = [i for i in all_issues if self.cfg.passes_severity_threshold(i.severity)]

        # 2. Disabled checks
        _check_map = {
            "complexity": "complexity", "dead_code": "dead_code",
            "security": "security", "missing_type_hints": "type_hints",
            "duplicate": "duplicate", "deep_nesting": "deep_nesting",
            "style": "long_function",
        }
        all_issues = [
            i for i in all_issues
            if self.cfg.is_check_enabled(_check_map.get(i.issue_type, i.issue_type))
        ]

        # 3. Max issues per file
        from collections import defaultdict
        per_file: dict[str, int] = defaultdict(int)
        filtered: list[Issue] = []
        for iss in all_issues:
            if per_file[iss.file] < self.cfg.max_issues_per_file:
                filtered.append(iss)
                per_file[iss.file] += 1
        all_issues = filtered

        print(f"[Scanner] Found {len(all_issues)} issue(s).")
        return all_issues


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    scanner = Scanner(repo)
    issues = scanner.scan(full_scan=True)

    print("\n── Issue Report ──────────────────────────────────────────")
    for i, issue in enumerate(issues, 1):
        print(f"\n[{i}] {issue.severity.upper()} | {issue.issue_type}")
        print(f"     File : {issue.file}:{issue.line}")
        print(f"     Desc : {issue.description}")

    print(f"\nTotal: {len(issues)} issue(s) found.")

    # Dump to JSON for other agents to consume
    out = [i.to_dict() for i in issues]
    with open("scan_results.json", "w") as f:
        json.dump(out, f, indent=2)
    print("Results saved to scan_results.json")