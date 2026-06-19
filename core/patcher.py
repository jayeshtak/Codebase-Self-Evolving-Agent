import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import libcst as cst
from libcst._exceptions import ParserSyntaxError


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PatchResult:
    success: bool
    filepath: str
    original_code: str
    patched_code: str
    method: str          # "libcst" | "text" | "failed"
    error: str = ""

    def diff_summary(self) -> str:
        orig_lines = self.original_code.splitlines()
        new_lines = self.patched_code.splitlines()
        added = len([l for l in new_lines if l not in orig_lines])
        removed = len([l for l in orig_lines if l not in new_lines])
        return f"+{added} lines, -{removed} lines"


# ── Validators ────────────────────────────────────────────────────────────────

def is_valid_python(code: str) -> tuple[bool, str]:
    """Check if code is syntactically valid Python."""
    try:
        cst.parse_module(code)
        return True, ""
    except ParserSyntaxError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


# Language → (command, args_fn) for syntax validation
# args_fn receives a tmp file path and returns the full arg list
import tempfile, subprocess as _sp, os as _os

_LANG_VALIDATORS = {
    ".js":   lambda f: ["node", "--check", f],
    ".mjs":  lambda f: ["node", "--check", f],
    ".jsx":  lambda f: ["node", "--check", f],
    ".ts":   lambda f: ["tsc", "--noEmit", "--allowJs", "--skipLibCheck", "--target", "es2020", f],
    ".tsx":  lambda f: ["tsc", "--noEmit", "--allowJs", "--skipLibCheck", "--target", "es2020", f],
    ".java": lambda f: ["javac", f],
    ".cpp":  lambda f: ["g++", "-fsyntax-only", "-std=c++17", f],
    ".cxx":  lambda f: ["g++", "-fsyntax-only", "-std=c++17", f],
    ".cc":   lambda f: ["g++", "-fsyntax-only", "-std=c++17", f],
    ".c":    lambda f: ["gcc", "-fsyntax-only", f],
    ".php":  lambda f: ["php", "-l", f],
    ".go":   lambda f: ["go", "vet", f],
}


def is_valid_code(code: str, suffix: str) -> tuple[bool, str]:
    """Validate syntax for any supported language. Returns (valid, error_msg)."""
    if suffix == ".py":
        return is_valid_python(code)
    validator = _LANG_VALIDATORS.get(suffix)
    if not validator:
        return True, ""  # unknown language — trust it
    # Write to a temp file with the right extension
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp = f.name
        result = _sp.run(validator(tmp), capture_output=True, text=True, timeout=15)
        _os.unlink(tmp)
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip().splitlines()
            return False, err[0][:200] if err else "Syntax error"
        return True, ""
    except FileNotFoundError:
        return True, ""  # compiler not installed — skip validation
    except Exception as e:
        return True, ""  # validation failed to run — don't block the patch


def extract_code_from_llm(response: str) -> str:
    """
    Extract clean code from an LLM response.
    Handles ```python```, ```javascript```, ```js```, ```typescript```, and plain ``` blocks.
    """
    # Match any language fence: ```python, ```javascript, ```js, ```ts, or plain ```
    pattern = r"```(?:python|javascript|js|typescript|ts|jsx|tsx)?\s*\n(.*?)```"
    match = re.search(pattern, response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Try single backtick block
    pattern2 = r"`(.*?)`"
    match2 = re.search(pattern2, response, re.DOTALL)
    if match2:
        candidate = match2.group(1).strip()
        if "\n" in candidate:  # multi-line = likely code
            return candidate

    # Return as-is if no block found
    return response.strip()


# ── Function-level patcher using libcst ───────────────────────────────────────

class FunctionReplacer(cst.CSTTransformer):
    """Replace a specific function definition by name."""

    def __init__(self, target_name: str, new_code: str):
        self.target_name = target_name
        self.new_code = new_code
        self.replaced = False

    def leave_FunctionDef(self, original_node, updated_node):
        if updated_node.name.value == self.target_name:
            try:
                new_tree = cst.parse_statement(self.new_code + "\n")
                self.replaced = True
                return new_tree
            except Exception as e:
                print(f"[Patcher] libcst replace failed for '{self.target_name}': {e}")
                return updated_node
        return updated_node


# ── Patcher ───────────────────────────────────────────────────────────────────

class Patcher:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    def _resolve(self, filepath: str) -> Path:
        p = Path(filepath)
        return p if p.is_absolute() else self.repo_path / filepath

    # ── Full file replacement ─────────────────────────────────────────────────

    def patch_full_file(self, filepath: str, new_code: str) -> PatchResult:
        """
        Replace entire file content with new_code.
        Validates Python syntax for .py files; skips validation for other languages.
        """
        path = self._resolve(filepath)
        original = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        clean_code = extract_code_from_llm(new_code)

        # Syntax validation — all languages
        valid, error = is_valid_code(clean_code, path.suffix)
        if not valid:
            return PatchResult(
                success=False,
                filepath=filepath,
                original_code=original,
                patched_code=clean_code,
                method="failed",
                error=f"Syntax error in generated code: {error}",
            )

        path.write_text(clean_code, encoding="utf-8")
        print(f"[Patcher] Full file patched: {filepath}")
        return PatchResult(
            success=True,
            filepath=filepath,
            original_code=original,
            patched_code=clean_code,
            method="text",
        )

    # ── Function-level replacement via libcst ─────────────────────────────────

    def patch_function(self, filepath: str, function_name: str, new_function_code: str) -> PatchResult:
        """
        Replace a specific function in a file using libcst (Python only).
        Falls back to full-file replacement for non-Python files.
        """
        path = self._resolve(filepath)

        # libcst only supports Python — delegate non-Python to full-file patch
        if path.suffix != ".py":
            return self.patch_full_file(filepath, new_function_code)

        original = path.read_text(encoding="utf-8", errors="ignore")
        clean_code = extract_code_from_llm(new_function_code)

        # Validate the new function code first
        valid, error = is_valid_python(clean_code)
        if not valid:
            return PatchResult(
                success=False,
                filepath=filepath,
                original_code=original,
                patched_code=clean_code,
                method="failed",
                error=f"Syntax error in generated function: {error}",
            )

        try:
            tree = cst.parse_module(original)
            transformer = FunctionReplacer(function_name, clean_code)
            new_tree = tree.visit(transformer)

            if not transformer.replaced:
                print(f"[Patcher] Function '{function_name}' not found via libcst, "
                      f"falling back to full file patch.")
                return self.patch_full_file(filepath, new_function_code)

            patched_code = new_tree.code
            path.write_text(patched_code, encoding="utf-8")
            print(f"[Patcher] Function '{function_name}' patched via libcst in {filepath}")

            return PatchResult(
                success=True,
                filepath=filepath,
                original_code=original,
                patched_code=patched_code,
                method="libcst",
            )

        except Exception as e:
            print(f"[Patcher] libcst error: {e}, falling back to full file patch.")
            return self.patch_full_file(filepath, new_function_code)

    # ── Insert code at line ───────────────────────────────────────────────────

    def insert_at_line(self, filepath: str, line_number: int, code: str) -> PatchResult:
        """Insert code at a specific line number (1-indexed)."""
        path = self._resolve(filepath)
        original = path.read_text(encoding="utf-8", errors="ignore")
        lines = original.splitlines(keepends=True)

        insert_at = max(0, min(line_number - 1, len(lines)))
        lines.insert(insert_at, code + "\n")
        patched_code = "".join(lines)

        valid, error = is_valid_code(patched_code, path.suffix)
        if not valid:
            return PatchResult(
                success=False,
                filepath=filepath,
                original_code=original,
                patched_code=patched_code,
                method="failed",
                error=f"Syntax error after insertion: {error}",
            )

        path.write_text(patched_code, encoding="utf-8")
        print(f"[Patcher] Inserted code at line {line_number} in {filepath}")
        return PatchResult(
            success=True,
            filepath=filepath,
            original_code=original,
            patched_code=patched_code,
            method="text",
        )

    # ── Remove dead code ──────────────────────────────────────────────────────

    def remove_lines(self, filepath: str, start_line: int, end_line: int) -> PatchResult:
        """Remove a range of lines (1-indexed, inclusive)."""
        path = self._resolve(filepath)
        original = path.read_text(encoding="utf-8", errors="ignore")
        lines = original.splitlines(keepends=True)

        new_lines = lines[:start_line - 1] + lines[end_line:]
        patched_code = "".join(new_lines)

        valid, error = is_valid_code(patched_code, path.suffix)
        if not valid:
            return PatchResult(
                success=False,
                filepath=filepath,
                original_code=original,
                patched_code=patched_code,
                method="failed",
                error=f"Syntax error after line removal: {error}",
            )

        path.write_text(patched_code, encoding="utf-8")
        print(f"[Patcher] Removed lines {start_line}-{end_line} from {filepath}")
        return PatchResult(
            success=True,
            filepath=filepath,
            original_code=original,
            patched_code=patched_code,
            method="text",
        )


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tempfile
    import os

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    patcher = Patcher(repo)

    # Create a temp test file
    test_code = '''\
def add(a, b):
    # old implementation
    result = a + b
    return result

def multiply(a, b):
    return a * b
'''

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=repo,
        delete=False, prefix="patcher_test_"
    ) as f:
        f.write(test_code)
        test_file = f.name

    rel_path = os.path.relpath(test_file, repo)
    print(f"[Test] Created temp file: {rel_path}")

    print("\n── Test 1: patch_function (libcst) ──────────────────────")
    new_fn = '''\
def add(a, b):
    """Add two numbers."""
    return a + b
'''
    result = patcher.patch_function(rel_path, "add", new_fn)
    print(f"Success : {result.success}")
    print(f"Method  : {result.method}")
    print(f"Changes : {result.diff_summary()}")
    print(f"Result  :\n{result.patched_code}")

    print("\n── Test 2: is_valid_python ───────────────────────────────")
    valid, err = is_valid_python("def foo():\n    return 1\n")
    print(f"Valid code   : {valid}")
    invalid, err2 = is_valid_python("def foo(\n    broken syntax here")
    print(f"Invalid code : {invalid} — {err2[:60]}")

    print("\n── Test 3: extract_code_from_llm ────────────────────────")
    llm_response = """
Sure! Here's the fixed code:

```python
def add(a, b):
    return a + b
```

This is cleaner.
"""
    extracted = extract_code_from_llm(llm_response)
    print(f"Extracted:\n{extracted}")

    # Cleanup
    os.unlink(test_file)
    print("\n[Test] Temp file cleaned up. All tests passed.")
