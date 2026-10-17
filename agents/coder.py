import re
import sys
import time


class QuotaExhaustedException(Exception):
    """Raised when both Gemini and Groq quotas are exhausted."""
    pass
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CoderResult:
    success: bool
    file: str
    function_name: Optional[str]
    original_code: str
    generated_code: str
    explanation: str
    error: str = ""

    def to_dict(self):
        return {
            "success": self.success,
            "file": self.file,
            "function_name": self.function_name,
            "original_code": self.original_code,
            "generated_code": self.generated_code,
            "explanation": self.explanation,
            "error": self.error,
        }


# ── CoderAgent ────────────────────────────────────────────────────────────────

class CoderAgent:
    def __init__(self):
        self.client = genai.Client(api_key=config.GEMINI_API_KEY) if getattr(config, "has_gemini", lambda: bool(config.GEMINI_API_KEY))() else None
        self.groq = Groq(api_key=config.GROQ_API_KEY) if getattr(config, "has_groq", lambda: bool(config.GROQ_API_KEY))() else None

    # ── Read file context ─────────────────────────────────────────────────────

    def _read_file(self, repo_path: str, filepath: str) -> str:
        path = Path(repo_path) / filepath
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
        return ""

    def _get_git_diff(self, repo_path: str, filepath: str) -> str:
        """Return recent git diff for the file (last commit vs working tree)."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "diff", "HEAD", "--", filepath],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            diff = result.stdout.strip()
            if not diff:
                # Try staged diff
                result = subprocess.run(
                    ["git", "diff", "--cached", "--", filepath],
                    cwd=repo_path, capture_output=True, text=True, timeout=10
                )
                diff = result.stdout.strip()
            return diff[:1500] if diff else ""
        except Exception:
            return ""

    def _get_dependent_signatures(self, repo_path: str, filepath: str) -> str:
        """Return function signatures from files that import this file."""
        try:
            from core.graph import DependencyGraph
            dg = DependencyGraph(repo_path)
            dg.build()
            radius = dg.get_impact_radius(filepath)
            dependents = radius.get("direct_dependents", [])[:3]
            if not dependents:
                return ""
            lines = ["\nFiles that depend on this module (may break if signatures change):"]
            for dep in dependents:
                dep_path = Path(repo_path) / dep
                if dep_path.exists():
                    content = dep_path.read_text(encoding="utf-8", errors="ignore")
                    # Extract first 8 lines with import/function defs as a signature summary
                    sig_lines = [
                        l for l in content.splitlines()
                        if l.strip().startswith(("def ", "class ", "import ", "from ", "function ", "export "))
                    ][:6]
                    if sig_lines:
                        lines.append(f"\n{dep}:\n" + "\n".join(f"  {s}" for s in sig_lines))
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception:
            return ""

    # ── Build prompt ──────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        issue: dict,
        file_content: str,
        rag_context: list[dict],
        reviewer_feedback: Optional[str] = None,
        previous_attempt: Optional[str] = None,
        repo_path: str = "",
    ) -> str:

        context_text = ""
        if rag_context:
            context_text = "\n\nRelated code from codebase (for context only — do not copy blindly):\n"
            for hit in rag_context[:4]:
                context_text += (
                    f"\n--- {hit['metadata']['file']}:"
                    f"{hit['metadata']['start_line']}-"
                    f"{hit['metadata']['end_line']} ---\n"
                    f"{hit['text'][:400]}\n"
                )

        # Git diff context — shows what recently changed in this file
        git_diff_text = ""
        if repo_path:
            diff = self._get_git_diff(repo_path, issue.get("file", ""))
            if diff:
                git_diff_text = f"\n\nRecent git diff for this file (for context):\n```diff\n{diff}\n```"

        # Dependent signatures — callers that could break if we change signatures
        dep_sig_text = ""
        if repo_path:
            dep_sig_text = self._get_dependent_signatures(repo_path, issue.get("file", ""))

        feedback_text = ""
        if reviewer_feedback and previous_attempt:
            feedback_text = f"""
Previous fix attempt:
```python
{previous_attempt}
```

Reviewer feedback (you MUST address this):
{reviewer_feedback}
"""

        lang = issue.get("language", "python")
        lang_label = {
            "python": "Python", "javascript": "JavaScript/Node.js",
            "typescript": "TypeScript", "java": "Java",
            "cpp": "C++", "c": "C", "php": "PHP (Laravel)",
            "go": "Go", "rust": "Rust",
        }.get(lang, lang.capitalize())
        fence = {
            "python": "python", "javascript": "javascript",
            "typescript": "typescript", "java": "java",
            "cpp": "cpp", "c": "c", "php": "php",
            "go": "go", "rust": "rust",
        }.get(lang, lang)

        # One-shot example tailored to the issue type
        _EXAMPLES = {
            "undefined_name": (
                "# Before\nimport os\nimport sys  # unused\n\ndef greet():\n    print('hi')\n",
                "# After\nimport os\n\ndef greet():\n    print('hi')\n",
                "Removed unused import 'sys'.",
            ),
            "dead_code": (
                "def helper():\n    pass  # never called\n\ndef main():\n    print('hello')\n",
                "def main():\n    print('hello')\n",
                "Removed dead function 'helper' that was never called.",
            ),
            "security": (
                "import hashlib\ndef hash_pw(pw):\n    return hashlib.md5(pw.encode()).hexdigest()\n",
                "import hashlib\ndef hash_pw(pw):\n    return hashlib.sha256(pw.encode()).hexdigest()\n",
                "Replaced weak MD5 hash with SHA256.",
            ),
            "complexity": (
                "def calc(a, b, op):\n    if op=='add':\n        if a>0:\n            if b>0:\n                if a+b>10:\n                    return 'big'\n    return 'other'\n",
                "def _is_big_positive_sum(a, b):\n    return a > 0 and b > 0 and a + b > 10\n\ndef calc(a, b, op):\n    if op == 'add' and _is_big_positive_sum(a, b):\n        return 'big'\n    return 'other'\n",
                "Extracted nested condition into helper to reduce complexity.",
            ),
        }
        ex = _EXAMPLES.get(issue.get("issue_type", ""))
        example_text = ""
        if ex:
            before, after, expl = ex
            example_text = f"""
Example of this type of fix:
BEFORE:
```{fence}
{before}```
AFTER:
```{fence}
{after}```
Explanation: {expl}
"""

        prompt = f"""You are an expert {lang_label} engineer. Fix the following code issue.
{example_text}

Issue:
- File: {issue.get('file')}
- Line: {issue.get('line')}
- Type: {issue.get('issue_type')}
- Severity: {issue.get('severity')}
- Description: {issue.get('description')}
- Fix strategy: {issue.get('fix_strategy', 'Refactor to improve quality.')}

Current file content:
```{fence}
{file_content}
```
{git_diff_text}
{dep_sig_text}
{context_text}
{feedback_text}

Instructions:
1. Fix ONLY the specific issue described. Do not change unrelated code.
2. Preserve all existing functionality and public function signatures exactly.
3. If dependents are listed above, do NOT change the signatures of functions they call.
4. Return the COMPLETE fixed file content, not just the changed function.

Respond using EXACTLY this format and nothing else:

FUNCTION: <name of the primary function changed, or null>
EXPLANATION: <one sentence explaining what was changed and why>
CODE:
```{fence}
<complete fixed file content here>
```"""

        return prompt

    # ── LLM call with Groq fallback ───────────────────────────────────────────

    def _call_llm(self, prompt: str) -> str:
        """Call Gemini. On repeated 429s or if Gemini is absent fall back to Groq automatically."""
        if self.client:
            try:
                response = self.client.models.generate_content(
                    model=config.GEMINI_PRO,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=8000,
                    )
                )
                return response.text.strip()
            except Exception as e:
                err = str(e)
                if "429" not in err and "RESOURCE_EXHAUSTED" not in err and "API_KEY" not in err:
                    raise
                print(f"[Coder] Gemini error/quota hit ({e}). Falling back to Groq...")

        if self.groq:
            try:
                response = self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=8000,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                if "429" in str(e) or "rate_limit" in str(e).lower() or "quota" in str(e).lower():
                    raise QuotaExhaustedException("Both Gemini and Groq quotas exhausted.")
                raise
        
        raise QuotaExhaustedException("No configured LLM provider available (Gemini or Groq required).")

    # ── Generate fix ──────────────────────────────────────────────────────────

    def generate_fix(
        self,
        repo_path: str,
        issue: dict,
        rag_context: Optional[list[dict]] = None,
        reviewer_feedback: Optional[str] = None,
        previous_attempt: Optional[str] = None,
    ) -> CoderResult:
        """
        Generate a fix for a single issue using Gemini Pro.
        If reviewer_feedback is provided, this is a revision attempt.
        """
        rag_context = rag_context or []
        filepath = issue.get("file", "")
        file_content = self._read_file(repo_path, filepath)

        if not file_content:
            return CoderResult(
                success=False,
                file=filepath,
                function_name=None,
                original_code="",
                generated_code="",
                explanation="",
                error=f"File not found or empty: {filepath}",
            )

        prompt = self._build_prompt(
            issue=issue,
            file_content=file_content,
            rag_context=rag_context,
            reviewer_feedback=reviewer_feedback,
            previous_attempt=previous_attempt,
            repo_path=repo_path,
        )

        attempt_type = "revision" if reviewer_feedback else "initial fix"
        print(f"[Coder] Generating {attempt_type} for {filepath}:{issue.get('line')} "
              f"({issue.get('issue_type')})")

        try:
            raw = self._call_llm(prompt)

            # Parse structured format: FUNCTION / EXPLANATION / CODE block
            function_name = None
            explanation = ""
            fixed_code = ""

            fn_match = re.search(r"^FUNCTION:\s*(.+)$", raw, re.MULTILINE)
            if fn_match:
                val = fn_match.group(1).strip()
                function_name = None if val.lower() == "null" else val

            exp_match = re.search(r"^EXPLANATION:\s*(.+)$", raw, re.MULTILINE)
            if exp_match:
                explanation = exp_match.group(1).strip()

            _fence = r"```(?:python|javascript|js|typescript|ts|jsx|tsx|java|cpp|c\+\+|c|php|go|rust)?"
            code_match = re.search(rf"CODE:\s*{_fence}\s*\n(.*?)```", raw, re.DOTALL)
            if code_match:
                fixed_code = code_match.group(1).strip()

            # Fallback: any code block anywhere in response
            if not fixed_code:
                fallback = re.search(rf"{_fence}\s*\n(.*?)```", raw, re.DOTALL)
                if fallback:
                    fixed_code = fallback.group(1).strip()

            if not fixed_code:
                return CoderResult(
                    success=False,
                    file=filepath,
                    function_name=None,
                    original_code=file_content,
                    generated_code="",
                    explanation="",
                    error="LLM returned no code block in response.",
                )

            print(f"[Coder] Fix generated. Function: {function_name}. {explanation}")

            return CoderResult(
                success=True,
                file=filepath,
                function_name=function_name,
                original_code=file_content,
                generated_code=fixed_code,
                explanation=explanation,
            )

        except Exception as e:
            print(f"[Coder] LLM fix unavailable or failed ({e}). Running AST deterministic fix synthesizer...")
            fn, exp, code = self._generate_deterministic_fix(file_content, issue)
            if code and code != file_content:
                print(f"[Coder] Deterministic AST fix synthesized successfully: {exp}")
                return CoderResult(
                    success=True,
                    file=filepath,
                    function_name=fn,
                    original_code=file_content,
                    generated_code=code,
                    explanation=exp,
                )
            if isinstance(e, QuotaExhaustedException):
                raise
            return CoderResult(
                success=False,
                file=filepath,
                function_name=None,
                original_code=file_content,
                generated_code="",
                explanation="",
                error=str(e),
            )

    def _generate_deterministic_fix(self, file_content: str, issue: dict):
        """
        Rule-based AST patch synthesizer for common technical debt issues.
        Ensures high-quality code remediation even when LLM keys are absent.
        Returns: (function_name, explanation, fixed_code)
        """
        desc = issue.get("description", "").lower()
        itype = issue.get("issue_type", "").lower()
        lines = file_content.splitlines()
        target_line = issue.get("line", 1) - 1

        # 1. Unused import: 'X' imported but unused
        if "imported but unused" in desc or (itype == "dead_code" and "unused import" in desc):
            match = re.search(r"'([^']+)'\s+imported but unused", desc) or re.search(r"unused import:\s*'([^']+)'", desc)
            symbol = match.group(1) if match else ""
            new_lines = []
            for i, line in enumerate(lines):
                if symbol and (f"import {symbol}" in line or f", {symbol}" in line or f"{symbol}," in line):
                    if line.strip().startswith(f"import {symbol}") and "," not in line:
                        continue
                    elif f", {symbol}" in line:
                        new_lines.append(line.replace(f", {symbol}", ""))
                    elif f"{symbol}, " in line:
                        new_lines.append(line.replace(f"{symbol}, ", ""))
                    else:
                        continue
                elif i == target_line and line.strip().startswith("import "):
                    continue
                else:
                    new_lines.append(line)
            return None, f"Removed redundant unused import '{symbol or 'module'}'.", "\n".join(new_lines) + "\n"

        # 2. Unused variable: local variable 'conn' is assigned to but never used
        if "assigned to but never used" in desc or "unused variable" in desc:
            match = re.search(r"variable\s*'([^']+)'\s+is assigned", desc) or re.search(r"unused variable:\s*'([^']+)'", desc)
            var_name = match.group(1) if match else "conn"
            new_lines = []
            for i, line in enumerate(lines):
                if i == target_line and f"{var_name} =" in line:
                    rhs = line.split("=", 1)[1].strip()
                    indent = len(line) - len(line.lstrip())
                    spaces = " " * indent
                    if "(" in rhs and ")" in rhs:
                        new_lines.append(f"{spaces}# Executed without unused variable assignment\n{spaces}{rhs}")
                    else:
                        continue
                else:
                    new_lines.append(line)
            return None, f"Cleaned up unused local variable '{var_name}'.", "\n".join(new_lines) + "\n"

        # 3. Security: eval()
        if "eval" in desc or "[b307]" in desc:
            new_content = file_content
            if "import ast" not in new_content:
                new_content = "import ast\n" + new_content
            new_content = re.sub(r"\beval\(", "ast.literal_eval(", new_content)
            return None, "Replaced unsafe dynamic eval() with safe ast.literal_eval().", new_content

        # 4. Security: Hardcoded password
        if "hardcoded password" in desc or "[b105]" in desc:
            new_content = re.sub(
                r'([A-Za-z0-9_]*PASSWORD[A-Za-z0-9_]*)\s*=\s*["\'][^"\']+["\']',
                r'\1 = os.environ.get("\1", "")',
                file_content
            )
            return None, "Moved hardcoded credential into environment variable os.environ.get().", new_content

        # 5. Security: Weak MD5
        if "md5" in desc or "[b324]" in desc:
            new_content = re.sub(r"hashlib\.md5\(", "hashlib.sha256(", file_content)
            return None, "Upgraded legacy weak MD5 hashing to industry-standard SHA-256.", new_content

        # 6. Dead function
        if itype == "dead_code" and "function" in desc:
            match = re.search(r"function:\s*'([^']+)'", desc)
            fn = match.group(1) if match else ""
            if fn:
                new_content = re.sub(rf"def\s+{fn}\s*\(.*?\):(?:\n(?:[ \t]+.*|\s*))*", f"# [DEPRECATED] {fn} was removed as dead code\n", file_content)
                return None, f"Removed dead unreachable function '{fn}'.", new_content

        return None, "", ""


# ── CLI test (requires API key) ───────────────────────────────────────────────

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."

    mock_issue = {
        "file": "agents/scanner.py",
        "line": 1,
        "issue_type": "dead_code",
        "severity": "low",
        "description": "Unused import: 'os'",
        "language": "python",
        "fix_strategy": "Remove the unused import statement.",
        "extra": {},
    }

    coder = CoderAgent()
    result = coder.generate_fix(repo_path=repo, issue=mock_issue)

    print("\n-- Coder Result ------------------------------------------")
    print(f"Success     : {result.success}")
    print(f"Function    : {result.function_name}")
    print(f"Explanation : {result.explanation}")
    if result.error:
        print(f"Error       : {result.error}")
    if result.generated_code:
        print(f"Generated   :\n{result.generated_code[:300]}...")
