import ast
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx
from tree_sitter_languages import get_parser


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FileNode:
    path: str
    language: str
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


# ── Extension → language map ──────────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py":   "python",
    ".js":   "javascript",
    ".mjs":  "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".java": "java",
    ".cpp":  "cpp",
    ".cxx":  "cpp",
    ".cc":   "cpp",
    ".php":  "php",
    ".go":   "go",
    ".rs":   "rust",
}

# tree-sitter node types that define a named function / method per language
_FN_TYPES = {
    "python":     ["function_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function", "function_expression"],
    "typescript": ["function_declaration", "method_definition", "arrow_function", "function_expression"],
    "java":       ["method_declaration", "constructor_declaration"],
    "cpp":        ["function_definition"],
    "php":        ["function_definition", "method_declaration"],
    "go":         ["function_declaration", "method_declaration"],
    "rust":       ["function_item"],
}

# tree-sitter node types that represent imports per language
_IMPORT_TYPES = {
    "python":     ["import_statement", "import_from_statement"],
    "javascript": ["import_statement", "call_expression"],   # require() also
    "typescript": ["import_statement"],
    "java":       ["import_declaration"],
    "cpp":        ["preproc_include"],
    "php":        ["namespace_use_declaration", "require_expression", "include_expression"],
    "go":         ["import_declaration", "import_spec"],
    "rust":       ["use_declaration"],
}


# ── Dependency Graph ──────────────────────────────────────────────────────────

class DependencyGraph:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self.graph = nx.DiGraph()
        self._file_nodes: dict[str, FileNode] = {}

    # ── Python parsing (AST — more reliable for imports) ─────────────────────

    def _parse_python_file(self, filepath: Path) -> FileNode:
        node = FileNode(
            path=str(filepath.relative_to(self.repo_path)),
            language="python",
        )
        try:
            source = filepath.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
            for item in ast.walk(tree):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    node.functions.append(item.name)
                elif isinstance(item, ast.Import):
                    for alias in item.names:
                        node.imports.append(alias.name)
                elif isinstance(item, ast.ImportFrom):
                    if item.module:
                        node.imports.append(item.module)
        except Exception as e:
            print(f"[Graph] Python parse error on {filepath}: {e}")
        return node

    # ── Generic tree-sitter parsing ───────────────────────────────────────────

    def _parse_ts_file(self, filepath: Path, lang_name: str) -> FileNode:
        node = FileNode(
            path=str(filepath.relative_to(self.repo_path)),
            language=lang_name,
        )
        try:
            parser = get_parser(lang_name)
            source_bytes = filepath.read_bytes()
            tree = parser.parse(source_bytes)

            fn_types    = set(_FN_TYPES.get(lang_name, []))
            import_types = set(_IMPORT_TYPES.get(lang_name, []))

            def walk(n):
                if n.type in fn_types:
                    name = _extract_name(n, lang_name)
                    if name:
                        node.functions.append(name)
                if n.type in import_types:
                    imp = _extract_import(n, lang_name, source_bytes)
                    if imp:
                        node.imports.append(imp)
                for child in n.children:
                    walk(child)

            walk(tree.root_node)
        except Exception as e:
            print(f"[Graph] tree-sitter parse error on {filepath}: {e}")
        return node

    # ── Build graph ───────────────────────────────────────────────────────────

    def build(self) -> nx.DiGraph:
        """Walk repo, parse all supported files, build directed dependency graph."""
        all_files = []
        for ext in _EXT_TO_LANG:
            all_files.extend(self.repo_path.rglob(f"*{ext}"))

        # Skip venv, node_modules, hidden dirs
        skip_parts = {"venv", "node_modules", "__pycache__", "target", "build", "dist"}
        files = [
            f for f in all_files
            if not (set(f.parts) & skip_parts)
            and not any(part.startswith(".") for part in f.parts)
        ]

        print(f"[Graph] Building dependency graph for {len(files)} file(s)...")

        for filepath in files:
            lang_name = _EXT_TO_LANG[filepath.suffix.lower()]
            if lang_name == "python":
                file_node = self._parse_python_file(filepath)
            else:
                file_node = self._parse_ts_file(filepath, lang_name)

            rel = file_node.path
            self._file_nodes[rel] = file_node
            self.graph.add_node(rel, language=lang_name,
                                functions=file_node.functions,
                                imports=file_node.imports)

        # Connect edges based on imports
        module_to_file = self._build_module_map()
        for rel, node in self._file_nodes.items():
            for imp in node.imports:
                target = module_to_file.get(imp)
                if target and target != rel:
                    self.graph.add_edge(rel, target)

        print(f"[Graph] Graph built: {self.graph.number_of_nodes()} nodes, "
              f"{self.graph.number_of_edges()} edges.")
        return self.graph

    def _build_module_map(self) -> dict[str, str]:
        """Map module names / stems to relative file paths."""
        module_map = {}
        for rel in self._file_nodes:
            p = Path(rel)
            stem = p.stem
            # Python dot-module
            py_module = rel.replace("/", ".").replace("\\", ".").removesuffix(".py")
            module_map[py_module] = rel
            module_map[stem] = rel
            # JS/TS: ./utils -> utils
            module_map[f"./{stem}"] = rel
            module_map[f"../{stem}"] = rel
        return module_map

    # ── Impact radius ─────────────────────────────────────────────────────────

    def get_impact_radius(self, filepath: str) -> dict:
        rel = str(Path(filepath).relative_to(self.repo_path)) \
            if Path(filepath).is_absolute() else filepath

        if rel not in self.graph:
            return {
                "file": rel,
                "direct_dependents": [],
                "transitive_dependents": [],
                "impact_score": 0,
                "risk_level": "low",
            }

        direct = list(self.graph.predecessors(rel))
        try:
            transitive = list(nx.ancestors(self.graph, rel))
        except Exception:
            transitive = []

        impact_score = len(direct) * 2 + len(transitive)
        risk_level = (
            "high"   if impact_score >= 10 else
            "medium" if impact_score >= 4  else
            "low"
        )

        return {
            "file": rel,
            "direct_dependents": direct,
            "transitive_dependents": transitive,
            "impact_score": impact_score,
            "risk_level": risk_level,
        }

    def get_all_impact_radii(self) -> dict[str, dict]:
        return {rel: self.get_impact_radius(rel) for rel in self._file_nodes}

    def get_function_map(self) -> dict[str, list[str]]:
        return {rel: node.functions for rel, node in self._file_nodes.items()}

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.graph.nodes(data=True)),
            "edges": list(self.graph.edges()),
            "impact_radii": self.get_all_impact_radii(),
            "function_map": self.get_function_map(),
        }

    def save(self, output_path: str = "graph.json"):
        data = self.to_dict()
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[Graph] Saved to {output_path}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_name(node, lang_name: str) -> Optional[str]:
    """Get the function/method name from a tree-sitter function node."""
    # Most languages: look for first 'identifier' child
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8", errors="ignore")
        # Java / PHP: method name is often named 'name'
        if child.type == "name":
            return child.text.decode("utf-8", errors="ignore")
    return None


def _extract_import(node, lang_name: str, source_bytes: bytes) -> Optional[str]:
    """Extract the imported module/path string from an import node."""
    try:
        if lang_name == "python":
            # Handled by Python ast
            return None
        elif lang_name in ("javascript", "typescript"):
            # import ... from "module"  or  require("module")
            for child in node.children:
                if child.type == "string":
                    val = child.text.decode("utf-8", errors="ignore").strip("'\"` ")
                    return val
        elif lang_name == "java":
            # import com.example.Foo;  ->  text minus 'import ' and ';'
            text = node.text.decode("utf-8", errors="ignore")
            text = text.replace("import", "").replace(";", "").strip()
            # Return just the simple class name (last segment)
            return text.split(".")[-1] if text else None
        elif lang_name == "cpp":
            # #include "header.h" or <header.h>
            for child in node.children:
                if child.type in ("string_literal", "system_lib_string"):
                    val = child.text.decode("utf-8", errors="ignore").strip("<>\"")
                    return Path(val).stem  # just the stem
        elif lang_name == "php":
            for child in node.children:
                if child.type == "string":
                    val = child.text.decode("utf-8", errors="ignore").strip("'\"")
                    return Path(val).stem
        elif lang_name == "go":
            for child in node.children:
                if child.type == "interpreted_string_literal":
                    val = child.text.decode("utf-8", errors="ignore").strip('"')
                    return val.split("/")[-1]  # last path segment
        elif lang_name == "rust":
            # use std::fs;  ->  take first identifier after 'use'
            text = node.text.decode("utf-8", errors="ignore")
            text = text.replace("use ", "").replace(";", "").strip()
            return text.split("::")[-1] if text else None
    except Exception:
        pass
    return None


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    dg = DependencyGraph(repo)
    dg.build()

    print("\n── Impact Radii ──────────────────────────────────────────")
    radii = dg.get_all_impact_radii()
    for filepath, radius in sorted(radii.items(), key=lambda x: -x[1]["impact_score"]):
        if radius["impact_score"] > 0:
            print(f"\n  {filepath}")
            print(f"    Risk       : {radius['risk_level'].upper()}")
            print(f"    Score      : {radius['impact_score']}")
            print(f"    Direct     : {radius['direct_dependents']}")
            print(f"    Transitive : {radius['transitive_dependents']}")

    print("\n── Function Map ──────────────────────────────────────────")
    for filepath, fns in dg.get_function_map().items():
        if fns:
            print(f"  {filepath}: {fns}")

    dg.save("graph.json")
