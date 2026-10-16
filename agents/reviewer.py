import json
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from groq import Groq
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ReviewResult:
    approved: bool
    confidence: float        # 0.0 - 1.0
    feedback: str
    issues_found: list[str]
    iteration: int
    error: str = ""

    def to_dict(self):
        return {
            "approved": self.approved,
            "confidence": self.confidence,
            "feedback": self.feedback,
            "issues_found": self.issues_found,
            "iteration": self.iteration,
            "error": self.error,
        }


# ── ReviewerAgent ─────────────────────────────────────────────────────────────

class ReviewerAgent:
    def __init__(self):
        self.groq = Groq(api_key=config.GROQ_API_KEY) if getattr(config, "has_groq", lambda: bool(config.GROQ_API_KEY))() else None
        self.gemini = genai.Client(api_key=config.GEMINI_API_KEY) if getattr(config, "has_gemini", lambda: bool(config.GEMINI_API_KEY))() else None

    def _call_llm(self, prompt: str) -> str:
        """Try Groq first (faster/cheaper for review), fall back to Gemini on 429 or absence."""
        if self.groq:
            try:
                response = self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=500,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                if "429" not in str(e) and "rate_limit" not in str(e).lower() and "API_KEY" not in str(e):
                    raise
                print(f"[Reviewer] Groq error/rate limited ({e}). Falling back to Gemini Flash...")

        if self.gemini:
            try:
                response = self.gemini.models.generate_content(
                    model=config.GEMINI_FLASH,
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=500),
                )
                return response.text.strip()
            except Exception as e:
                print(f"[Reviewer] Gemini call failed: {e}")
                raise

        raise RuntimeError("No LLM provider available for review (set GROQ_API_KEY or GEMINI_API_KEY).")

    # ── Build prompt ──────────────────────────────────────────────────────────

    @staticmethod
    def _focused_window(code: str, issue_line: int, context: int = 60) -> str:
        """Return ±context lines around issue_line. Falls back to full code if short."""
        lines = code.splitlines()
        if len(lines) <= context * 2:
            return code
        start = max(0, issue_line - context - 1)
        end = min(len(lines), issue_line + context)
        window = lines[start:end]
        prefix = f"# ... (lines 1-{start} omitted)\n" if start > 0 else ""
        suffix = f"\n# ... (lines {end+1}-{len(lines)} omitted)" if end < len(lines) else ""
        return prefix + "\n".join(window) + suffix

    def _build_prompt(
        self,
        issue: dict,
        original_code: str,
        fixed_code: str,
        explanation: str,
        iteration: int,
    ) -> str:
        issue_line = issue.get("line", 1) or 1
        lang = issue.get("language", "python")
        fence = {
            "python": "python", "javascript": "javascript",
            "typescript": "typescript", "java": "java",
            "cpp": "cpp", "c": "c", "php": "php",
            "go": "go", "rust": "rust",
        }.get(lang, lang)

        orig_window = self._focused_window(original_code, issue_line)
        fixed_window = self._focused_window(fixed_code, issue_line)

        return f"""You are a senior software engineer doing a strict code review.
A junior engineer has proposed a fix for a code issue. Review it carefully.

Original issue:
- File: {issue.get('file')}
- Line: {issue_line}
- Type: {issue.get('issue_type')}
- Description: {issue.get('description')}

Engineer's explanation: {explanation}

Original code (focused around line {issue_line}):
```{fence}
{orig_window}
```

Proposed fix (focused around line {issue_line}):
```{fence}
{fixed_window}
```

Review iteration: {iteration} of {config.MAX_REVIEW_ITERATIONS}

Evaluate the fix on these criteria:
1. Does it actually fix the stated issue?
2. Does it preserve all existing functionality?
3. Is the fix safe — no new bugs introduced?
4. Is the code quality equal or better than before?
5. Are there any syntax errors or obvious mistakes?

Respond with JSON only:
{{
  "approved": true or false,
  "confidence": 0.0 to 1.0,
  "feedback": "specific actionable feedback if not approved, or brief confirmation if approved",
  "issues_found": ["list of specific problems found, empty if approved"]
}}

Approve if you are reasonably confident (>0.6) the fix is correct and safe. Do NOT reject because code is omitted — focus only on the changed area.
JSON only. No markdown. No extra text."""

    # ── Review ────────────────────────────────────────────────────────────────

    def review(
        self,
        issue: dict,
        original_code: str,
        fixed_code: str,
        explanation: str,
        iteration: int = 1,
    ) -> ReviewResult:
        """
        Review a proposed fix. Returns ReviewResult with approval decision.
        """
        print(f"[Reviewer] Reviewing fix for {issue.get('file')}:{issue.get('line')} "
              f"(iteration {iteration}/{config.MAX_REVIEW_ITERATIONS})")

        prompt = self._build_prompt(
            issue=issue,
            original_code=original_code,
            fixed_code=fixed_code,
            explanation=explanation,
            iteration=iteration,
        )

        try:
            raw = self._call_llm(prompt)

            # Strip markdown if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            approved = data.get("approved", False)
            confidence = float(data.get("confidence", 0.0))
            feedback = data.get("feedback", "")
            issues_found = data.get("issues_found", [])

            status = "APPROVED" if approved else "REJECTED"
            print(f"[Reviewer] {status} (confidence={confidence:.2f}): {feedback[:80]}")

            return ReviewResult(
                approved=approved,
                confidence=confidence,
                feedback=feedback,
                issues_found=issues_found,
                iteration=iteration,
            )

        except json.JSONDecodeError as e:
            print(f"[Reviewer] JSON parse error: {e}")
            return ReviewResult(
                approved=False,
                confidence=0.0,
                feedback="Could not parse reviewer response.",
                issues_found=["JSON parse error"],
                iteration=iteration,
                error=str(e),
            )

        except Exception as e:
            print(f"[Reviewer] Error: {e}")
            return ReviewResult(
                approved=False,
                confidence=0.0,
                feedback="Reviewer encountered an error.",
                issues_found=[str(e)],
                iteration=iteration,
                error=str(e),
            )

    # ── Agentic review loop ───────────────────────────────────────────────────

    def review_loop(
        self,
        issue: dict,
        coder,
        repo_path: str,
        rag_context: Optional[list] = None,
        max_iterations: int = None,
    ) -> tuple[ReviewResult, str]:
        """
        Run the full Coder -> Reviewer -> Coder loop.

        Returns (final_review_result, final_fixed_code)

        Flow:
        1. Coder generates fix
        2. Reviewer reviews
        3. If rejected → Coder revises with feedback
        4. Repeat up to max_iterations
        5. Return final result regardless
        """
        max_iterations = max_iterations or config.MAX_REVIEW_ITERATIONS
        rag_context = rag_context or []

        previous_code = None
        reviewer_feedback = None
        final_code = ""
        final_review = None
        final_function_name = None

        for iteration in range(1, max_iterations + 1):
            print(f"\n[ReviewLoop] Iteration {iteration}/{max_iterations}")

            # Coder generates (or revises) the fix
            coder_result = coder.generate_fix(
                repo_path=repo_path,
                issue=issue,
                rag_context=rag_context,
                reviewer_feedback=reviewer_feedback,
                previous_attempt=previous_code,
            )

            if not coder_result.success:
                print(f"[ReviewLoop] Coder failed: {coder_result.error}")
                return ReviewResult(
                    approved=False,
                    confidence=0.0,
                    feedback=f"Coder failed: {coder_result.error}",
                    issues_found=[coder_result.error],
                    iteration=iteration,
                ), "", None

            current_code = coder_result.generated_code
            final_code = current_code
            final_function_name = coder_result.function_name

            # Reviewer evaluates
            review = self.review(
                issue=issue,
                original_code=coder_result.original_code,
                fixed_code=current_code,
                explanation=coder_result.explanation,
                iteration=iteration,
            )
            final_review = review

            if review.approved:
                print(f"[ReviewLoop] Approved on iteration {iteration}.")
                return review, current_code, final_function_name

            # Not approved — prepare feedback for next iteration
            reviewer_feedback = review.feedback
            previous_code = current_code
            print(f"[ReviewLoop] Rejected. Feedback: {review.feedback[:100]}")

        # Max iterations reached without approval
        print(f"[ReviewLoop] Max iterations reached. Returning best attempt.")
        return final_review, final_code, final_function_name


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
    }

    original = "import os\nimport json\n\ndef hello():\n    return 'world'\n"
    fixed = "import json\n\ndef hello():\n    return 'world'\n"

    reviewer = ReviewerAgent()
    result = reviewer.review(
        issue=mock_issue,
        original_code=original,
        fixed_code=fixed,
        explanation="Removed unused 'os' import.",
        iteration=1,
    )

    print("\n-- Reviewer Result ---------------------------------------")
    print(f"Approved    : {result.approved}")
    print(f"Confidence  : {result.confidence}")
    print(f"Feedback    : {result.feedback}")
    print(f"Issues found: {result.issues_found}")
