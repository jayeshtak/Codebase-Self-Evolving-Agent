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
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.groq = Groq(api_key=config.GROQ_API_KEY)

    def _call_llm(self, prompt: str, max_tokens: int = 1000) -> str:
        """Call Gemini Flash, fall back to Groq on 429."""
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
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
        print("[Planner] Gemini rate limited. Falling back to Groq...")
        response = self.groq.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()

    # ── Priority scoring ──────────────────────────────────────────────────────

    def _score_issue(self, issue: dict, impact: dict) -> float:
        """
        Score = severity_weight × (1 + impact_score / 10)
        Higher = fix sooner.
        """
        severity_weights = {"high": 3.0, "medium": 2.0, "low": 1.0}
        base = severity_weights.get(issue.get("severity", "low"), 1.0)
        impact_score = impact.get("impact_score", 0)
        return round(base * (1 + impact_score / 10), 3)

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
            f"- [{i.severity.upper()}] {i.file}:{i.line} — {i.description}"
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
    ) -> Plan:
        """
        Build a prioritized fix plan from scanner issues.

        issues       : list of issue dicts from scanner
        impact_radii : {filepath: impact_dict} from graph
        rag_context  : search hits from ChromaDB
        rejections   : past rejections from SQLite
        issue_ids    : DB ids corresponding to each issue (parallel list)
        """
        impact_radii = impact_radii or {}
        rag_context = rag_context or []
        rejections = rejections or []
        issue_ids = issue_ids or [None] * len(issues)

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
            score = self._score_issue(issue, impact)
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
            )

            if rejected:
                skipped_items.append(item)
                print(f"[Planner] Skipped (rejected): {filepath}:{issue.get('line')} — {reason}")
            else:
                plan_items.append(item)

        # Sort: high severity first, then lowest impact radius (safest high-value fix first)
        _sev_order = {"high": 0, "medium": 1, "low": 2}
        plan_items.sort(key=lambda x: (_sev_order.get(x.severity, 2), x.impact_radius))

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

    print("\n── Skipped Items ─────────────────────────────────────────")
    for item in plan.skipped:
        print(f"  {item.file}:{item.line} — {item.skip_reason}")

    # Save plan
    with open("plan.json", "w") as f:
        json.dump(plan.to_dict(), f, indent=2)
    print("\n[Planner] Plan saved to plan.json")
