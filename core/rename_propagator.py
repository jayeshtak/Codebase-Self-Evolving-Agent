"""
core/rename_propagator.py

Detects function/class renames introduced by a fix and propagates them
across all files in the repo using AST-aware search-replace.

Supported languages: Python (ast), JS/TS/Java/Go/Rust (tree-sitter).
"""
import ast
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from tree_sitter_languages import get_parser


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class RenameOp:
    old_name: str
    new_name: str
    kind: str       # "function" | "class" | "variable"
    source_file: str


@dataclass
class PropagationResult:
    renames: list[RenameOp]
    files_updated: list[str]
    replacements: int
    errors: list[str] = field(default_factory=list)


# ── AST differ ────────────────────────────────────────────────────────────────

def _python_top_level_names(source: str) -> dict[str, str]:
    """Return {name: kind} for top-level defs in Python source."""
    names = {}
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names[node.name] = "function"
            elif isinstance(node, ast.AsyncFunctionDef):
                names[node.name] = "function"
            elif isinstance(node, ast.ClassDef):
                names[node.name] = "class"
    except SyntaxError:
        pass
    return names


def _ts_top_level_names(source: str, lang: str) -> dict[str, str]:
    """Return {name: kind} for top-level defs using tree-sitter."""
    names = {}
    _fn_types = {
        "javascript": ["function_declaration", "method_definition"],
        "typescript": ["function_declaration", "method_definition"],
        "java":       ["method_declaration", "constructor_declaration"],
        "go":         ["function_declaration", "method_declaration"],
        "rust":       ["function_item"],
        "cpp":        ["function_definition"],
        "php":        ["function_definition", "method_declaration"],
    }
    _class_types = {
        "javascript": ["class_declaration"],
        "typescript": ["class_declaration"],
        "java":       ["class_declaration"],
        "go":         ["type_spec"],
        "rust":       ["struct_item", "enum_item", "impl_item"],
    }
    try:
        parser = get_parser(lang)
        tree = parser.parse(source.encode())

        def walk(node):
            if node.type in _fn_types.get(lang, []):
                for child in node.children:
                    if child.type == "identifier":
                        names[child.text.decode()] = "function"
                        break
            if node.type in _class_types.get(lang, []):
                for child in node.children:
                    if child.type == "identifier":
                        names[child.text.decode()] = "class"
                        break
            for child in node.children:
                walk(child)

        walk(tree.root_node)
    except Exception:
        pass
    return names


_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".go": "go", ".rs": "rust",
    ".cpp": "cpp", ".cc": "cpp", ".php": "php",
}


def detect_renames(
    original_code: str,
    new_code: str,
    filepath: str,
) -> list[RenameOp]:
    """
    Compare top-level definitions before and after a fix.
    Returns RenameOps for names that were removed from old and a
    matching-kind name was added in new (simple 1-to-1 heuristic).
    """
    ext = Path(filepath).suffix
    lang = _EXT_LANG.get(ext, "")

    if lang == "python":
        old_names = _python_top_level_names(original_code)
        new_names = _python_top_level_names(new_code)
    elif lang:
        old_names = _ts_top_level_names(original_code, lang)
        new_names = _ts_top_level_names(new_code, lang)
    else:
        return []

    removed = {n: k for n, k in old_names.items() if n not in new_names}
    added   = {n: k for n, k in new_names.items() if n not in old_names}

    renames = []
    # Match removed → added by kind (function→function, class→class)
    for old_n, old_k in removed.items():
        candidates = [n for n, k in added.items() if k == old_k]
        if len(candidates) == 1:
            renames.append(RenameOp(
                old_name=old_n,
                new_name=candidates[0],
                kind=old_k,
                source_file=filepath,
            ))
    return renames


# ── Repo-wide propagation ─────────────────────────────────────────────────────

_SKIP_DIRS = {".git", "venv", "node_modules", "__pycache__", "target", "dist", "build", ".mypy_cache"}


def _find_source_files(repo_path: Path) -> list[Path]:
    files = []
    for p in repo_path.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _EXT_LANG and p.is_file():
            files.append(p)
    return files


def _safe_word_replace(content: str, old: str, new: str) -> tuple[str, int]:
    """Replace whole-word occurrences of old with new. Returns (new_content, count)."""
    pattern = rf'\b{re.escape(old)}\b'
    new_content, count = re.subn(pattern, new, content)
    return new_content, count


def propagate_renames(
    repo_path: str,
    renames: list[RenameOp],
    changed_file: str,
) -> PropagationResult:
    """
    Apply a list of RenameOps across all source files in the repo
    (excluding the already-patched source file).
    """
    root = Path(repo_path).resolve()
    files = _find_source_files(root)
    changed_abs = (root / changed_file).resolve()

    files_updated = []
    total_replacements = 0
    errors = []

    for path in files:
        if path.resolve() == changed_abs:
            continue  # already patched by the coder
        try:
            original = path.read_text(encoding="utf-8", errors="ignore")
            content = original
            file_replacements = 0
            for op in renames:
                content, n = _safe_word_replace(content, op.old_name, op.new_name)
                file_replacements += n
            if file_replacements > 0:
                path.write_text(content, encoding="utf-8")
                rel = str(path.relative_to(root))
                files_updated.append(rel)
                total_replacements += file_replacements
                print(f"[Rename] {rel}: {file_replacements} replacement(s) "
                      f"({', '.join(f'{r.old_name}->{r.new_name}' for r in renames)})")
        except Exception as e:
            errors.append(f"{path}: {e}")

    return PropagationResult(
        renames=renames,
        files_updated=files_updated,
        replacements=total_replacements,
        errors=errors,
    )
