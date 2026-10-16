import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Static fallback strategies per issue_type — used when LLM doesn't return one
_FALLBACK_STRATEGIES: dict[str, str] = {
    "undefined_name":  "Remove the unused import statement from the top of the file.",
    "dead_code":       "Delete the unreachable or unused function/variable; if needed elsewhere, export it from a shared module.",
    "complexity":      "Apply Extract Function refactoring — split the complex function into smaller, single-responsibility helpers.",
    "deep_nesting":    "Flatten nested conditionals using early-return (guard clauses) or extract the inner block into a helper function.",
    "long_function":   "Apply Extract Function refactoring to break the function into cohesive sub-functions of ≤30 lines each.",
    "security":        "Replace the insecure API/pattern with its recommended secure equivalent (see description for specifics).",
    "duplicate":       "Extract the duplicated block into a shared helper function and call it from both locations.",
    "style":           "Apply the style fix described — rename, reformat, or restructure to match the project's coding conventions.",
    "missing_docs":    "Add a concise docstring describing the function's purpose, parameters, and return value.",
    "performance":     "Profile the hot path and apply the most impactful optimisation (caching, algorithm change, or I/O batching).",
    "syntax_error":    "Fix the syntax error at the indicated line — check for missing colons, brackets, or indentation.",
    "runtime_error":      "Add a guard check or try/except to handle the edge case that causes the runtime error.",
    "missing_type_hints": "Add PEP 484 type annotations to all function arguments and the return type.",
}

from google import genai
from google.genai import types
from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PlanItem:
    issue_id: Optional[int]
    file: str
    line: int
    issue_type: str
    severity: str
    description: str
    language: str
    priority_score: float
    risk_level: str
    impact_radius: int
    fix_strategy: str
    skip_reason: str = ""
    skipped: bool = False
    extra: dict = field(default_factory=dict)
    priority_explanation: str = ""
    estimated_difficulty: str = "moderate"
    regression_likelihood: str = "low"
    has_test_coverage: bool = False
    requires_human_approval: bool = False
    patch_scope: str = "targeted"
    group_id: str = ""
    conflicts_with: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "issue_id": self.issue_id,
            "file": self.file,
            "line": self.line,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "language": self.language,
            "priority_score": self.priority_score,
            "risk_level": self.risk_level,
            "impact_radius": self.impact_radius,
            "fix_strategy": self.fix_strategy,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "priority_explanation": self.priority_explanation,
            "estimated_difficulty": self.estimated_difficulty,
            "regression_likelihood": self.regression_likelihood,
            "has_test_coverage": self.has_test_coverage,
            "requires_human_approval": self.requires_human_approval,
            "patch_scope": self.patch_scope,
            "group_id": self.group_id,
            "conflicts_with": self.conflicts_with,
        }


@dataclass
class Plan:
    scan_id: str
    items: list[PlanItem]
    skipped: list[PlanItem]
    total_issues: int
    summary: str

    def actionable(self) -> list[PlanItem]:
        return [i for i in self.items if not i.skipped]

    def to_dict(self):
        return {
            "scan_id": self.scan_id,
            "total_issues": self.total_issues,
            "actionable": len(self.actionable()),
            "skipped": len(self.skipped),
            "summary": self.summary,
            "items": [i.to_dict() for i in self.items],
        }


# ── Planner ───────────────────────────────────────────────────────────────────

class PlannerAgent:
    def __init__(self):
        self.client = genai.Client(api_key=config.GEMINI_API_KEY) if getattr(config, "has_gemini", lambda: bool(config.GEMINI_API_KEY))() else None
        self.groq = Groq(api_key=config.GROQ_API_KEY) if getattr(config, "has_groq", lambda: bool(config.GROQ_API_KEY))() else None

    def _call_llm(self, prompt: str, max_tokens: int = 1000) -> str:
        """Call Gemini Flash, fall back to Groq on 429 or absence."""
        if self.client:
            try:
                response = self.client.models.generate_content(
                    model=config.GEMINI_FLASH,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=max_tokens,
                    )
                )
                return response.text.strip()
            except Exception as e:
                if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e) and "API_KEY" not in str(e):
                    raise
                print(f"[Planner] Gemini rate limited ({e}). Falling back to Groq...")

        if self.groq:
            try:
                response = self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                print(f"[Planner] Groq call failed: {e}")
                raise

        raise RuntimeError("No LLM provider available for planning (set GEMINI_API_KEY or GROQ_API_KEY).")

    # ── Priority scoring & evaluation ─────────────────────────────────────────

    def _detect_test_coverage(self, filepath: str, repo_path: Optional[str] = None) -> bool:
        """Check if automated test files exist for the given source file."""
        try:
            base = Path(repo_path) if repo_path else Path.cwd()
            p = Path(filepath)
            stem = p.stem.lower()
            candidates = [
                base / "tests" / f"test_{stem}.py",
                base / "tests" / f"{stem}.test.js",
                base / "tests" / f"{stem}.test.ts",
                base / "tests" / f"{stem}_test.go",
                base / p.parent / f"test_{stem}.py",
                base / p.parent / f"{stem}_test.py",
            ]
            if any(c.exists() for c in candidates):
                return True
            tests_dir = base / "tests"
            if tests_dir.exists():
                for f in tests_dir.glob("*.*"):
                    if stem in f.name.lower() and f.suffix in (".py", ".js", ".ts", ".go", ".java"):
                        return True
        except Exception:
            pass
        return False

    def _evaluate_priority(
        self,
        issue: dict,
        impact: dict,
        has_tests: bool,
        rejection_counts: dict[str, int],
    ) -> tuple[float, str, str, str, bool, str]:
        """
        Multi-factor transparent priority evaluation:
        - Severity: High (+4.0), Medium (+2.5), Low (+1.0)
        - Code Complexity: CC >= 15 (+1.5), CC >= 10 (+0.8)
        - Dependency Impact: +min(impact_radius * 0.35, 2.0)
        - Regression Likelihood: High (impact >= 4), Medium (impact >= 2), Low
        - Estimated Difficulty: quick_win (+0.5), moderate (0.0), complex (-0.2)
        - Test Presence: +0.8 boost (safe to fix & verify automatically)
        - Past Rejections Penalty: deduction if user frequently rejected this pattern
        - Human Approval: Required if regression_likelihood is High or impact >= 3 or High severity
        Returns: (score, explanation, difficulty, regression_likelihood, requires_approval, patch_scope)
        """
        sev = issue.get("severity", "low").lower()
        sev_weights = {"high": 4.0, "medium": 2.5, "low": 1.0}
        base_sev = sev_weights.get(sev, 1.0)
        parts = [f"Severity {sev.capitalize()} (+{base_sev})"]
        score = base_sev

        # 1. Cyclomatic Complexity
        extra = issue.get("extra") or {}
        cc = extra.get("complexity", 0)
        if cc >= 15:
            score += 1.5
            parts.append(f"High complexity CC={cc} (+1.5)")
        elif cc >= 10:
            score += 0.8
            parts.append(f"Moderate complexity CC={cc} (+0.8)")

        # 2. Dependency Impact (fan-in)
        impact_radius = impact.get("impact_score", 0)
        if impact_radius > 0:
            dep_bonus = min(round(impact_radius * 0.35, 2), 2.0)
            score += dep_bonus
            parts.append(f"Fan-in impact={impact_radius} (+{dep_bonus})")

        # 3. Regression Likelihood
        if impact_radius >= 4 or "core" in issue.get("file", "").lower():
            regression_likelihood = "high"
        elif impact_radius >= 2:
            regression_likelihood = "medium"
        else:
            regression_likelihood = "low"

        # 4. Estimated Fix Difficulty
        itype = issue.get("issue_type", "").lower()
        if itype in ("undefined_name", "dead_code", "missing_docs", "syntax_error"):
            difficulty = "quick_win"
            score += 0.5
            parts.append("Quick-win resolution (+0.5)")
            patch_scope = "targeted"
        elif itype in ("complexity", "deep_nesting", "long_function", "style"):
            difficulty = "moderate"
            patch_scope = "targeted"
        else:
            difficulty = "complex"
            score -= 0.2
            parts.append("Complex architectural fix (-0.2)")
            patch_scope = "targeted" if itype != "duplicate" else "rewrite"

        # 5. Test Availability
        if has_tests:
            score += 0.8
            parts.append("Automated test coverage verified (+0.8)")
        else:
            parts.append("No automated test coverage")

        # 6. Past Rejection Pattern
        rej_count = rejection_counts.get(itype, 0)
        if rej_count > 0:
            penalty = min(round(rej_count * 0.4, 2), 1.5)
            score -= penalty
            parts.append(f"Historical rejection pattern penalty (-{penalty})")

        # 7. Required Human Approval Gate
        requires_approval = (
            regression_likelihood == "high" or
            impact_radius >= 3 or
            sev == "high" or
            difficulty == "complex"
        )

        final_score = max(0.1, round(score, 2))
        explanation = " | ".join(parts) + f" => Score {final_score:.2f}"
        return final_score, explanation, difficulty, regression_likelihood, requires_approval, patch_scope

    def _score_issue(self, issue: dict, impact: dict) -> float:
        """Backward compatible scoring method."""
        score, _, _, _, _, _ = self._evaluate_priority(
            issue=issue,
            impact=impact,
            has_tests=False,
            rejection_counts={},
        )
        return score

    def _cluster_issues(self, items: list[PlanItem]) -> None:
        """
        Groups issues in the same file that are within 10 lines of each other.
        Sets group_id and populates conflicts_with to prevent conflicting parallel edits.
        """
        from collections import defaultdict
        by_file: dict[str, list[PlanItem]] = defaultdict(list)
        for it in items:
            by_file[it.file].append(it)

        for filepath, f_items in by_file.items():
            f_items.sort(key=lambda x: x.line)
            cluster_id = 1
            for i in range(len(f_items)):
                cur = f_items[i]
                conflicts = []
                shared_group = cur.group_id
                for j in range(len(f_items)):
                    if i == j:
                        continue
                    other = f_items[j]
                    if abs(cur.line - other.line) <= 10:
                        conflicts.append(f"{other.file}:{other.line}")
                        if other.group_id:
                            shared_group = other.group_id
                if conflicts:
                    if not shared_group:
                        shared_group = f"{filepath}#grp_{cluster_id}"
                        cluster_id += 1
                    cur.group_id = shared_group
                    cur.conflicts_with = conflicts
                    for j in range(len(f_items)):
                        if i != j and abs(cur.line - f_items[j].line) <= 10:
                            f_items[j].group_id = shared_group

    # ── Rejection filter ──────────────────────────────────────────────────────

    def _is_rejected(self, issue: dict, rejections: list[dict]) -> tuple[bool, str]:
        """Check if this issue matches a past rejection."""
        for r in rejections:
            if (r.get("file") == issue.get("file") and
                    r.get("issue_type") == issue.get("issue_type") and
                    r.get("description") == issue.get("description")):
                return True, r.get("reason", "previously rejected by user")
        return False, ""

    # ── LLM: fix strategy ─────────────────────────────────────────────────────

    def _get_fix_strategies(self, issues: list[dict], rag_context: list[dict]) -> dict[str, str]:
        """
        Ask Gemini Flash to suggest a fix strategy for each issue.
        Returns {issue_description: fix_strategy_string}
        """
        if not issues:
            return {}

        context_text = ""
        if rag_context:
            context_text = "\n\nRelevant codebase context:\n"
            for hit in rag_context[:5]:
                context_text += (
                    f"\n--- {hit['metadata']['file']}:"
                    f"{hit['metadata']['start_line']}-{hit['metadata']['end_line']} ---\n"
                    f"{hit['text'][:300]}\n"
                )

        issues_text = json.dumps([{
            "file": i.get("file"),
            "line": i.get("line"),
            "issue_type": i.get("issue_type"),
            "severity": i.get("severity"),
            "description": i.get("description"),
        } for i in issues[:10]], indent=2)

        prompt = f"""You are a senior software engineer reviewing code issues.
For each issue below, provide a concise fix strategy (1-2 sentences max).
Be specific — name the refactoring technique, pattern, or approach.

Issues:
{issues_text}
{context_text}

Respond ONLY with a valid JSON object mapping each issue description to its fix strategy.
Example format:
{{
  "issue description here": "fix strategy here",
  "another issue": "another strategy"
}}

JSON only, no markdown, no explanation."""

        try:
            import re
            raw = self._call_llm(prompt, max_tokens=1000)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            try:
                return json.loads(raw.strip())
            except json.JSONDecodeError:
                raw = re.sub(r'[\x00-\x1f\x7f]', ' ', raw)
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
                return {}
        except Exception as e:
            print(f"[Planner] LLM fix strategy error: {e}")
            return {}

    # ── LLM: plan summary ─────────────────────────────────────────────────────

    def _get_plan_summary(self, items: list[PlanItem]) -> str:
        if not items:
            return "No actionable issues found."

        items_text = "\n".join(
            f"- [{i.severity.upper()}] {i.file}:{i.line} - {i.description}"
            for i in items[:10]
        )

        prompt = f"""You are a senior engineer. Summarize this fix plan in 2-3 sentences.
Focus on what will be improved and any notable risks.

Issues to fix:
{items_text}

Be concise and direct. No bullet points."""

        try:
            return self._call_llm(prompt, max_tokens=200)
        except Exception as e:
            print(f"[Planner] Summary error: {e}")
            return f"{len(items)} issue(s) queued for fixing."

    # ── Main plan ─────────────────────────────────────────────────────────────

    def plan(
        self,
        scan_id: str,
        issues: list[dict],
        impact_radii: Optional[dict] = None,
        rag_context: Optional[list[dict]] = None,
        rejections: Optional[list[dict]] = None,
        issue_ids: Optional[list[int]] = None,
        repo_path: Optional[str] = None,
    ) -> Plan:
        """
        Build an intelligently prioritized fix plan from scanner issues.

        issues       : list of issue dicts from scanner
        impact_radii : {filepath: impact_dict} from graph
        rag_context  : search hits from ChromaDB
        rejections   : past rejections from SQLite
        issue_ids    : DB ids corresponding to each issue (parallel list)
        repo_path    : optional path to the target repository root
        """
        impact_radii = impact_radii or {}
        rag_context = rag_context or []
        rejections = rejections or []
        issue_ids = issue_ids or [None] * len(issues)

        # Build rejection frequency by issue_type for learning
        rejection_counts: dict[str, int] = {}
        for r in rejections:
            it = r.get("issue_type", "")
            if it:
                rejection_counts[it] = rejection_counts.get(it, 0) + 1

        print(f"[Planner] Planning {len(issues)} issue(s)...")

        # Get fix strategies from LLM
        strategies = self._get_fix_strategies(issues, rag_context)

        plan_items = []
        skipped_items = []

        for issue, issue_id in zip(issues, issue_ids):
            filepath = issue.get("file", "")
            impact = impact_radii.get(filepath, {
                "impact_score": 0,
                "risk_level": "low",
            })

            # Check rejection memory
            rejected, reason = self._is_rejected(issue, rejections)
            has_tests = self._detect_test_coverage(filepath, repo_path=repo_path)

            # Evaluate multi-factor priority score & attributes
            (
                score,
                explanation,
                difficulty,
                regression_likelihood,
                requires_approval,
                patch_scope
            ) = self._evaluate_priority(
                issue=issue,
                impact=impact,
                has_tests=has_tests,
                rejection_counts=rejection_counts,
            )

            issue_type = issue.get("issue_type", "")
            fallback = _FALLBACK_STRATEGIES.get(issue_type, "Refactor to improve quality.")
            strategy = strategies.get(issue.get("description", ""), fallback)

            item = PlanItem(
                issue_id=issue_id,
                file=filepath,
                line=issue.get("line", 0),
                issue_type=issue.get("issue_type", ""),
                severity=issue.get("severity", "low"),
                description=issue.get("description", ""),
                language=issue.get("language", "python"),
                priority_score=score,
                risk_level=impact.get("risk_level", "low"),
                impact_radius=impact.get("impact_score", 0),
                fix_strategy=strategy,
                skipped=rejected,
                skip_reason=reason,
                extra=issue.get("extra", {}),
                priority_explanation=explanation,
                estimated_difficulty=difficulty,
                regression_likelihood=regression_likelihood,
                has_test_coverage=has_tests,
                requires_human_approval=requires_approval,
                patch_scope=patch_scope,
            )

            if rejected:
                skipped_items.append(item)
                print(f"[Planner] Skipped (rejected): {filepath}:{issue.get('line')} - {reason}")
            else:
                plan_items.append(item)

        # Detect groups and conflicting proximate edits
        self._cluster_issues(plan_items)

        # Sort: Highest priority score first, tie-break by lowest impact radius (safest high-value fix first)
        plan_items.sort(key=lambda x: (-x.priority_score, x.impact_radius))

        summary = self._get_plan_summary(plan_items)

        print(f"[Planner] Plan: {len(plan_items)} actionable, "
              f"{len(skipped_items)} skipped.")

        return Plan(
            scan_id=scan_id,
            items=plan_items,
            skipped=skipped_items,
            total_issues=len(issues),
            summary=summary,
        )


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Mock issues from scanner
    mock_issues = [
        {
            "file": "agents/scanner.py",
            "line": 42,
            "issue_type": "complexity",
            "severity": "high",
            "description": "'scan' has cyclomatic complexity 15 (high, threshold=10)",
            "language": "python",
            "extra": {"complexity": 15},
        },
        {
            "file": "agents/scanner.py",
            "line": 1,
            "issue_type": "dead_code",
            "severity": "low",
            "description": "Unused import: 'os'",
            "language": "python",
            "extra": {},
        },
        {
            "file": "core/graph.py",
            "line": 80,
            "issue_type": "style",
            "severity": "medium",
            "description": "Function 'build' is 65 lines long (threshold=60)",
            "language": "python",
            "extra": {"length": 65},
        },
    ]

    mock_impact = {
        "agents/scanner.py": {"impact_score": 8, "risk_level": "medium"},
        "core/graph.py": {"impact_score": 12, "risk_level": "high"},
    }

    mock_rejections = [
        {
            "file": "agents/scanner.py",
            "issue_type": "dead_code",
            "description": "Unused import: 'os'",
            "reason": "intentional — used later",
        }
    ]

    planner = PlannerAgent()
    plan = planner.plan(
        scan_id="test-scan-001",
        issues=mock_issues,
        impact_radii=mock_impact,
        rejections=mock_rejections,
    )

    print("\n── Plan Output ───────────────────────────────────────────")
    print(f"Summary  : {plan.summary}")
    print(f"Total    : {plan.total_issues}")
    print(f"Actionable: {len(plan.actionable())}")
    print(f"Skipped  : {len(plan.skipped)}")

    print("\n── Actionable Items (priority order) ────────────────────")
    for i, item in enumerate(plan.actionable(), 1):
        print(f"\n[{i}] Score={item.priority_score} | {item.severity.upper()} | {item.issue_type}")
        print(f"     File     : {item.file}:{item.line}")
        print(f"     Issue    : {item.description}")
        print(f"     Strategy : {item.fix_strategy}")
        print(f"     Risk     : {item.risk_level} (impact={item.impact_radius})")

    print("\n-- Skipped Items -----------------------------------------")
    for item in plan.skipped:
        print(f"  {item.file}:{item.line} - {item.skip_reason}")

    # Save plan
    with open("plan.json", "w") as f:
        json.dump(plan.to_dict(), f, indent=2)
    print("\n[Planner] Plan saved to plan.json")
