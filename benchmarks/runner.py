"""
benchmarks/runner.py

Runs the codebase agent on a set of synthetic benchmark cases and
measures fix accuracy, confidence calibration, and false positive rate.

Usage:
    python benchmarks/runner.py [--cases benchmarks/cases] [--out benchmarks/results]
"""
import sys
import ast
import json
import shutil
import argparse
import tempfile
import textwrap
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Benchmark case metadata ───────────────────────────────────────────────────

@dataclass
class BenchCase:
    name: str
    filepath: Path
    issue_type: str
    severity: str
    description: str
    validate_expr: str      # Python expression evaluated with fixed_code in scope


@dataclass
class BenchResult:
    case: BenchCase
    status: str             # "passed" | "failed" | "error" | "skipped"
    confidence: float = 0.0
    reviewer_iters: int = 0
    fixed_code: str = ""
    explanation: str = ""
    elapsed: float = 0.0
    error: str = ""
    validation_detail: str = ""


# ── Parse case metadata from file comments ────────────────────────────────────

def _parse_case(path: Path) -> Optional[BenchCase]:
    lines = path.read_text().splitlines()
    meta = {}
    for line in lines:
        if line.startswith("# ") and ": " in line:
            key, _, val = line[2:].partition(": ")
            meta[key.strip()] = val.strip()
        elif line and not line.startswith("#"):
            break  # stop at first non-comment line

    required = ["issue_type", "severity", "description", "validate"]
    if not all(k in meta for k in required):
        return None

    return BenchCase(
        name=path.stem,
        filepath=path,
        issue_type=meta["issue_type"],
        severity=meta["severity"],
        description=meta["description"],
        validate_expr=meta["validate"],
    )


# ── Validation ────────────────────────────────────────────────────────────────

def _strip_metadata_comments(source: str) -> str:
    """Remove the leading # BENCHMARK CASE metadata block from source."""
    lines = source.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].startswith("#"):
        i += 1
    # Skip blank lines after comments
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    return "".join(lines[i:])


def _validate(case: BenchCase, fixed_code: str) -> tuple[bool, str]:
    """Evaluate the validate expression with fixed_code in scope.
    Strips metadata comments from fixed_code before validation so the
    validation strings don't accidentally match their own comment text."""
    clean_code = _strip_metadata_comments(fixed_code)
    safe_builtins = {"len": len, "list": list, "any": any, "all": all,
                     "sum": sum, "min": min, "max": max, "range": range}
    try:
        result = eval(case.validate_expr, {"fixed_code": clean_code, "__builtins__": safe_builtins})
        return bool(result), f"eval({case.validate_expr!r}) = {result}"
    except Exception as e:
        return False, f"validation error: {e}"


# ── Single case runner ────────────────────────────────────────────────────────

def _run_case(case: BenchCase, tmp_dir: Path) -> BenchResult:
    """Copy case file to a temp git repo, run fix pipeline, validate."""
    import git
    from agents.coder import CoderAgent, QuotaExhaustedException
    from agents.reviewer import ReviewerAgent
    from memory.chroma import CodebaseRAG

    # Set up isolated git repo — write clean source (no metadata comments)
    repo_dir = tmp_dir / case.name
    repo_dir.mkdir(parents=True, exist_ok=True)
    clean_source = _strip_metadata_comments(case.filepath.read_text())
    (repo_dir / case.filepath.name).write_text(clean_source)
    repo = git.Repo.init(str(repo_dir))
    repo.index.add([case.filepath.name])
    repo.index.commit("initial")

    issue = {
        "file": case.filepath.name,
        "line": 1,
        "issue_type": case.issue_type,
        "severity": case.severity,
        "description": case.description,
        "language": "python",
        "fix_strategy": {
            "undefined_name": "Remove the unused import statement.",
            "dead_code":      "Delete the unreachable or unused function.",
            "security":       "Replace the insecure API with its secure equivalent.",
            "deep_nesting":   "Flatten nested conditions using early-return guard clauses.",
            "long_function":  "Split into focused helper functions of ≤30 lines each.",
        }.get(case.issue_type, "Refactor to improve quality."),
    }

    coder = CoderAgent()
    reviewer = ReviewerAgent()
    rag = CodebaseRAG(str(repo_dir))

    t0 = time.time()
    try:
        rag_ctx = rag.search_for_issue(issue, n_results=3)

        # Ensure max_iterations is an int, providing a default if config.MAX_REVIEW_ITERATIONS is None.
        # config.validate() in main() should ideally ensure this, but mypy needs explicit handling.
        review_max_iterations = config.MAX_REVIEW_ITERATIONS if config.MAX_REVIEW_ITERATIONS is not None else 5

        final_review, fixed_code, _ = reviewer.review_loop(
            issue=issue,
            coder=coder,
            repo_path=str(repo_dir),
            rag_context=rag_ctx,
            max_iterations=review_max_iterations,
        )
        elapsed = time.time() - t0

        if not fixed_code:
            return BenchResult(case=case, status="error",
                               error="No fix generated", elapsed=elapsed)

        passed, detail = _validate(case, fixed_code)
        return BenchResult(
            case=case,
            status="passed" if passed else "failed",
            confidence=getattr(final_review, "confidence", 0.0),
            reviewer_iters=getattr(final_review, "iteration", 1),
            fixed_code=fixed_code,
            explanation=getattr(final_review, "feedback", ""),
            elapsed=elapsed,
            validation_detail=detail,
        )

    except Exception as e:
        return BenchResult(case=case, status="error",
                           error=str(e), elapsed=time.time() - t0)


# ── Report generation ─────────────────────────────────────────────────────────

def _build_html_report(results: list[BenchResult], out_dir: Path) -> Path:
    passed  = [r for r in results if r.status == "passed"]
    failed  = [r for r in results if r.status == "failed"]
    errors  = [r for r in results if r.status == "error"]
    total   = len(results)
    acc     = len(passed) / total if total else 0
    avg_conf = sum(r.confidence for r in results) / total if total else 0
    avg_iter = sum(r.reviewer_iters for r in results) / total if total else 0

    rows = ""
    for r in results:
        icon = {"passed": "✅", "failed": "❌", "error": "💥"}.get(r.status, "⚪")
        rows += f"""
        <tr>
          <td>{icon} {r.case.name}</td>
          <td>{r.case.issue_type}</td>
          <td>{r.case.severity}</td>
          <td>{r.status}</td>
          <td>{r.confidence:.2f}</td>
          <td>{r.reviewer_iters}</td>
          <td>{r.elapsed:.1f}s</td>
          <td style="font-size:11px;max-width:300px">{r.validation_detail or r.error}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Codebase Agent Benchmark Report</title>
  <style>
    body {{ font-family: sans-serif; max-width: 960px; margin: 40px auto; color: #333 }}
    h1 {{ color: #1a1a2e }}
    .metrics {{ display: flex; gap: 24px; margin: 24px 0 }}
    .metric {{ background: #f4f6fb; border-radius: 8px; padding: 16px 24px; text-align: center }}
    .metric .val {{ font-size: 2rem; font-weight: bold; color: #4361ee }}
    .metric .lbl {{ font-size: 0.85rem; color: #666 }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 24px }}
    th {{ background: #4361ee; color: white; padding: 8px 12px; text-align: left }}
    td {{ padding: 7px 12px; border-bottom: 1px solid #eee; font-size: 13px }}
    tr:hover {{ background: #f8f9ff }}
    .ts {{ color: #999; font-size: 0.8rem }}
  </style>
</head>
<body>
  <h1>🧬 Codebase Agent — Benchmark Report</h1>
  <p class="ts">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

  <div class="metrics">
    <div class="metric"><div class="val">{total}</div><div class="lbl">Total Cases</div></div>
    <div class="metric"><div class="val">{len(passed)}/{total}</div><div class="lbl">Passed</div></div>
    <div class="metric"><div class="val">{acc*100:.1f}%</div><div class="lbl">Accuracy</div></div>
    <div class="metric"><div class="val">{avg_conf:.2f}</div><div class="lbl">Avg Confidence</div></div>
    <div class="metric"><div class="val">{avg_iter:.1f}</div><div class="lbl">Avg Review Iters</div></div>
    <div class="metric"><div class="val">{len(errors)}</div><div class="lbl">Errors</div></div>
  </div>

  <h2>Results by Case</h2>
  <table>
    <thead>
      <tr>
        <th>Case</th><th>Issue Type</th><th>Severity</th>
        <th>Result</th><th>Confidence</th><th>Iters</th>
        <th>Time</th><th>Detail</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2>Summary</h2>
  <ul>
    <li><strong>Accuracy:</strong> {len(passed)}/{total} ({acc*100:.1f}%)</li>
    <li><strong>Failed cases:</strong> {', '.join(r.case.name for r in failed) or 'none'}</li>
    <li><strong>Errors:</strong> {', '.join(r.case.name for r in errors) or 'none'}</li>
    <li><strong>Avg reviewer confidence:</strong> {avg_conf:.3f}</li>
    <li><strong>Avg reviewer iterations:</strong> {avg_iter:.2f}</li>
  </ul>
</body>
</html>"""

    report_path = out_dir / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    report_path.write_text(html)
    return report_path


def _build_json_report(results: list[BenchResult]) -> dict:
    total   = len(results)
    passed  = sum(1 for r in results if r.status == "passed")
    return {
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "avg_confidence": round(sum(r.confidence for r in results) / total, 4) if total else 0,
        "avg_reviewer_iters": round(sum(r.reviewer_iters for r in results) / total, 4) if total else 0,
        "cases": [
            {
                "name": r.case.name,
                "issue_type": r.case.issue_type,
                "severity": r.case.severity,
                "status": r.status,
                "confidence": r.confidence,
                "reviewer_iters": r.reviewer_iters,
                "elapsed": round(r.elapsed, 2),
                "validation_detail": r.validation_detail,
                "error": r.error,
                "explanation": r.explanation,
            }
            for r in results
        ],
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run codebase-agent benchmark suite")
    parser.add_argument("--cases", default="benchmarks/cases",
                        help="Directory containing benchmark case files")
    parser.add_argument("--out", default="benchmarks/results",
                        help="Output directory for reports")
    parser.add_argument("--case", default=None,
                        help="Run only this specific case (by filename stem)")
    args = parser.parse_args()

    config.validate()

    cases_dir = Path(args.cases)
    out_dir   = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load cases
    all_files: list[Path] = sorted(cases_dir.glob("case_*.py"))
    if args.case:
        all_files = [f for f in all_files if f.stem == args.case]

    cases = [c for f in all_files if (c := _parse_case(f)) is not None]
    if not cases:
        print("No benchmark cases found.")
        return

    print(f"\n{'='*60}")
    print(f"  Codebase Agent Benchmark — {len(cases)} case(s)")
    print(f"{'='*60}\n")

    results: list[BenchResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case.name} ({case.issue_type}) ... ", end="", flush=True)
            result = _run_case(case, tmp_path)
            results.append(result)
            icon = {"passed": "✅", "failed": "❌", "error": "💥"}.get(result.status, "⚪")
            print(f"{icon}  conf={result.confidence:.2f}  iters={result.reviewer_iters}  {result.elapsed:.1f}s")
            if result.status != "passed":
                print(f"     detail: {result.validation_detail or result.error}")

    # Summary
    total  = len(results)
    passed = sum(1 for r in results if r.status == "passed")
    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{total} passed  ({passed/total*100:.1f}% accuracy)")
    print(f"{'='*60}\n")

    # Save reports
    html_path = _build_html_report(results, out_dir)
    json_data = _build_json_report(results)
    json_path = out_dir / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_path.write_text(json.dumps(json_data, indent=2))

    print(f"HTML report : {html_path}")
    print(f"JSON report : {json_path}")
    return json_data


if __name__ == "__main__":
    main()