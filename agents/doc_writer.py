"""
DocWriterAgent — detects undocumented functions and generates
docstrings (Python), JSDoc (JS/TS), Javadoc (Java), etc.
"""
import ast
import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DocResult:
    success: bool
    file: str
    function_name: str
    doc_added: str          # the generated docstring/JSDoc block
    patched_code: str       # full file with doc inserted
    error: str = ""


# ── Language helpers ──────────────────────────────────────────────────────────

_EXT_LANG = {
    ".py":   "python",
    ".js":   "javascript", ".mjs": "javascript", ".jsx": "javascript",
    ".ts":   "typescript", ".tsx": "typescript",
    ".java": "java",
    ".cpp":  "cpp", ".cxx": "cpp", ".cc": "cpp",
    ".php":  "php",
    ".go":   "go",
    ".rs":   "rust",
}

_DOC_STYLE = {
    "python":     "Google-style docstring (triple-quoted \"\"\")",
    "javascript": "JSDoc block comment (/** ... */)",
    "typescript": "JSDoc block comment (/** ... */)",
    "java":       "Javadoc block comment (/** ... */)",
    "cpp":        "Doxygen block comment (/** ... */)",
    "php":        "PHPDoc block comment (/** ... */)",
    "go":         "Go godoc comment (// FuncName ...)",
    "rust":       "Rust doc comment (/// ...)",
}


# ── Undocumented function detection ──────────────────────────────────────────

def _find_undocumented_python(filepath: Path, repo_path: str) -> list[dict]:
    """Use Python AST to find functions/methods with no docstring."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
    except Exception:
        return []

    issues = []
    rel = str(filepath.relative_to(repo_path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") and not node.name.startswith("__"):
            continue  # skip private helpers
        # Check for docstring
        has_doc = (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        )
        if not has_doc:
            issues.append({
                "file": rel,
                "line": node.lineno,
                "issue_type": "missing_docs",
                "severity": "low",
                "description": f"Function '{node.name}' has no docstring",
                "language": "python",
                "extra": {"function_name": node.name},
                "fix_strategy": f"Add a Google-style docstring to '{node.name}'.",
            })
    return issues


def _find_undocumented_ts(filepath: Path, repo_path: str, lang: str) -> list[dict]:
    """Use tree-sitter to find JS/TS/Java/Go/Rust functions with no doc comment."""
    try:
        from tree_sitter_languages import get_parser
        parser = get_parser(lang)
        source_bytes = filepath.read_bytes()
        tree = parser.parse(source_bytes)
    except Exception:
        return []

    rel = str(filepath.relative_to(repo_path))
    lines = source_bytes.decode("utf-8", errors="ignore").splitlines()

    _fn_types = {
        "javascript": {"function_declaration", "method_definition", "arrow_function"},
        "typescript": {"function_declaration", "method_definition", "arrow_function"},
        "java":       {"method_declaration", "constructor_declaration"},
        "cpp":        {"function_definition"},
        "php":        {"function_definition", "method_declaration"},
        "go":         {"function_declaration", "method_declaration"},
        "rust":       {"function_item"},
    }
    fn_types = _fn_types.get(lang, set())

    issues = []

    def walk(node, parent=None):
        if node.type in fn_types:
            lineno = node.start_point[0]  # 0-based
            # Get name
            name = None
            for child in node.children:
                if child.type in ("identifier", "name", "property_identifier"):
                    name = child.text.decode("utf-8", errors="ignore")
                    break
            if not name or name.startswith("_"):
                for child in node.children:
                    walk(child, node)
                return

            # Check if the line immediately before has a doc comment
            doc_styles = {
                "javascript": ("/**", "//"),
                "typescript": ("/**", "//"),
                "java":       ("/**",),
                "cpp":        ("/**", "///"),
                "php":        ("/**",),
                "go":         ("//",),
                "rust":       ("///",),
            }
            prefixes = doc_styles.get(lang, ("/**", "//"))
            prev_line = lines[lineno - 1].strip() if lineno > 0 else ""
            has_doc = any(prev_line.startswith(p) for p in prefixes) or prev_line.endswith("*/")

            if not has_doc:
                issues.append({
                    "file": rel,
                    "line": lineno + 1,
                    "issue_type": "missing_docs",
                    "severity": "low",
                    "description": f"Function '{name}' has no doc comment",
                    "language": lang,
                    "extra": {"function_name": name},
                    "fix_strategy": f"Add a {_DOC_STYLE.get(lang, 'doc comment')} to '{name}'.",
                })

        for child in node.children:
            walk(child, node)

    walk(tree.root_node)
    return issues


# ── DocWriterAgent ────────────────────────────────────────────────────────────

class DocWriterAgent:
    def __init__(self):
        self.gemini = genai.Client(api_key=config.GEMINI_API_KEY)
        self.groq   = Groq(api_key=config.GROQ_API_KEY)

    def _call_llm(self, prompt: str) -> str:
        try:
            resp = self.gemini.models.generate_content(
                model=config.GEMINI_FLASH,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=400),
            )
            return resp.text.strip()
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
        resp = self.groq.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=400,
        )
        return resp.choices[0].message.content.strip()

    # ── Scan for missing docs ─────────────────────────────────────────────────

    def scan_missing_docs(self, repo_path: str) -> list[dict]:
        """
        Walk repo and return Issue-compatible dicts for every undocumented
        public function/method.
        """
        skip = {"venv", "node_modules", "__pycache__", ".git", "build", "dist", "target"}
        issues: list[dict] = []

        for path in Path(repo_path).rglob("*"):
            if not path.is_file():
                continue
            if any(p in skip for p in path.parts):
                continue
            lang = _EXT_LANG.get(path.suffix.lower())
            if not lang:
                continue

            if lang == "python":
                issues += _find_undocumented_python(path, repo_path)
            else:
                issues += _find_undocumented_ts(path, repo_path, lang)

        print(f"[DocWriter] {len(issues)} undocumented function(s) found.")
        return issues

    # ── Generate doc for a single function ───────────────────────────────────

    def _build_prompt(self, issue: dict, file_content: str) -> str:
        lang = issue.get("language", "python")
        func_name = issue.get("extra", {}).get("function_name", "")
        style = _DOC_STYLE.get(lang, "doc comment")
        fence = {"python": "python", "javascript": "javascript",
                 "typescript": "typescript", "java": "java",
                 "cpp": "cpp", "php": "php", "go": "go", "rust": "rust"}.get(lang, lang)

        # Grab ±20 lines around the function for context
        line = issue.get("line", 1) - 1
        src_lines = file_content.splitlines()
        start = max(0, line - 2)
        end   = min(len(src_lines), line + 30)
        snippet = "\n".join(src_lines[start:end])

        return f"""You are an expert {lang} developer. Write a concise {style} for the function below.

Function name: {func_name}
File: {issue.get("file")}

Code context:
```{fence}
{snippet}
```

Rules:
1. Output ONLY the doc comment text itself — no code, no fences, no explanation
2. Keep it under 6 lines
3. Describe what the function does, parameters, and return value (if obvious)
4. Match the {style} format exactly
5. Do NOT repeat the function signature inside the doc

Output just the raw doc comment, nothing else."""

    def generate_doc(self, repo_path: str, issue: dict) -> DocResult:
        """
        Generate a doc comment for a missing-docs issue and insert it into the file.
        Returns DocResult with the patched file content.
        """
        filepath = Path(repo_path) / issue.get("file", "")
        if not filepath.exists():
            return DocResult(success=False, file=issue.get("file", ""),
                             function_name="", doc_added="", patched_code="",
                             error="File not found")

        lang = issue.get("language", "python")
        func_name = issue.get("extra", {}).get("function_name", "")
        insert_line = issue.get("line", 1) - 1  # 0-based

        file_content = filepath.read_text(encoding="utf-8", errors="ignore")
        prompt = self._build_prompt(issue, file_content)

        try:
            doc_text = self._call_llm(prompt).strip()
        except Exception as e:
            return DocResult(success=False, file=issue.get("file", ""),
                             function_name=func_name, doc_added="", patched_code="",
                             error=str(e))

        # Insert the doc comment just before the function definition line
        src_lines = file_content.splitlines(keepends=True)
        if insert_line >= len(src_lines):
            insert_line = len(src_lines) - 1

        # Detect indentation from the function line
        func_line = src_lines[insert_line] if insert_line < len(src_lines) else ""
        indent = len(func_line) - len(func_line.lstrip())
        indent_str = " " * indent

        # Format each line of the doc with the same indentation
        doc_lines = doc_text.splitlines()
        indented_doc = "\n".join(indent_str + l for l in doc_lines) + "\n"

        patched = (
            "".join(src_lines[:insert_line])
            + indented_doc
            + "".join(src_lines[insert_line:])
        )

        filepath.write_text(patched, encoding="utf-8")
        print(f"[DocWriter] Added doc to '{func_name}' in {issue.get('file')}:{insert_line+1}")

        return DocResult(
            success=True,
            file=issue.get("file", ""),
            function_name=func_name,
            doc_added=doc_text,
            patched_code=patched,
        )

    def generate_docs_for_file(self, repo_path: str, source_file: str) -> list[DocResult]:
        """Generate docs for all undocumented functions in a single file."""
        filepath = Path(repo_path) / source_file
        lang = _EXT_LANG.get(filepath.suffix.lower(), "python")

        if lang == "python":
            issues = _find_undocumented_python(filepath, repo_path)
        else:
            issues = _find_undocumented_ts(filepath, repo_path, lang)

        if not issues:
            print(f"[DocWriter] No undocumented functions in {source_file}")
            return []

        # Process in reverse line order so inserts don't shift line numbers
        issues_sorted = sorted(issues, key=lambda i: i["line"], reverse=True)
        results = []
        for issue in issues_sorted:
            result = self.generate_doc(repo_path, issue)
            results.append(result)

        return results


# -- CLI test ------------------------------------------------------------------

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    agent = DocWriterAgent()
    issues = agent.scan_missing_docs(repo)
    print(f"\n-- Missing Docs ------------------------------------------")
    for i in issues[:10]:
        print(f"  {i['file']}:{i['line']} - {i['description']}")
