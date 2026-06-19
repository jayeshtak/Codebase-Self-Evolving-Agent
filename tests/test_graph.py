"""Tests for core/graph.py — dependency graph."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.graph import DependencyGraph


@pytest.fixture()
def tmp_repo(tmp_path):
    # a imports b
    (tmp_path / "b.py").write_text(textwrap.dedent("""\
        def helper():
            return 42
    """))
    (tmp_path / "a.py").write_text(textwrap.dedent("""\
        from b import helper

        def main():
            return helper()
    """))
    (tmp_path / "c.py").write_text("# standalone\ndef standalone(): pass\n")
    return tmp_path


class TestBuild:
    def test_builds_without_error(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        g = dg.build()
        assert g is not None
        assert g.number_of_nodes() >= 2

    def test_nodes_include_all_py_files(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        node_names = list(dg._file_nodes.keys())
        assert any("a.py" in n for n in node_names)
        assert any("b.py" in n for n in node_names)

    def test_edge_from_a_to_b(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        edges = list(dg.graph.edges())
        rel_edges = [(str(Path(u).name), str(Path(v).name)) for u, v in edges]
        assert ("a.py", "b.py") in rel_edges


class TestImpactRadius:
    def test_b_has_dependents(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        radius = dg.get_impact_radius("b.py")
        assert len(radius["direct_dependents"]) >= 1

    def test_standalone_has_no_dependents(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        radius = dg.get_impact_radius("c.py")
        assert radius["direct_dependents"] == []
        assert radius["impact_score"] == 0

    def test_risk_levels(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        for rel in dg._file_nodes:
            radius = dg.get_impact_radius(rel)
            assert radius["risk_level"] in ("low", "medium", "high")


class TestFunctionMap:
    def test_functions_detected(self, tmp_repo):
        dg = DependencyGraph(str(tmp_repo))
        dg.build()
        fm = dg.get_function_map()
        a_fns = fm.get("a.py", [])
        b_fns = fm.get("b.py", [])
        assert "main" in a_fns
        assert "helper" in b_fns
