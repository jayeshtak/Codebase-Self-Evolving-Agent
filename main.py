import sys
import json
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import config

from agents.scanner import Scanner
from agents.planner import PlannerAgent, Plan, PlanItem
from agents.coder import CoderAgent, QuotaExhaustedException
from agents.reviewer import ReviewerAgent
from agents.test_writer import TestWriterAgent
from agents.runtime_tester import RuntimeTesterAgent, RuntimeScanResult
from core.graph import DependencyGraph
from core.git_manager import GitManager
from core.patcher import Patcher
from core.executor import Executor
from core.terminal import Terminal
from memory.chroma import CodebaseRAG
from memory.sqlite import SQLiteMemory, IssueRecord, FixRecord
from scheduler.jobs import ScanScheduler


# ── Pipeline ──────────────────────────────────────────────────────────────────

class CodebaseAgent:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        config.validate()

        # Core
        self.git = GitManager(str(self.repo_path))
        self.patcher = Patcher(str(self.repo_path))
        self.executor = Executor(str(self.repo_path))
        self.terminal = Terminal(str(self.repo_path))

        # Memory
        self.mem = SQLiteMemory()
        self.rag = CodebaseRAG(str(self.repo_path))

        # Agents
        self.planner = PlannerAgent()
        self.coder = CoderAgent()
        self.reviewer = ReviewerAgent()
        self.test_writer = TestWriterAgent()

        # Scheduler
        self.scheduler = ScanScheduler(str(self.repo_path))

        # Runtime tester
        self.runtime_tester = RuntimeTesterAgent(str(self.repo_path))

        print(f"[Main] CodebaseAgent initialized for: {self.repo_path}")

    # ── Step 1: Scan ──────────────────────────────────────────────────────────

    def scan(self, full_scan: bool = False) -> dict:
        """Run full scan pipeline. Returns scan results dict."""
        return self.scheduler.run_now(full_scan=full_scan)

    # ── Step 1b: Profile scan ─────────────────────────────────────────────────

    def profile_scan(self, entry_script: str = "") -> dict:
        """
        Run cProfile (Python) or node --prof (Node) and return performance hotspots
        as a scan-result-like dict compatible with the planner.
        """
        from core.profiler import Profiler
        framework, _ = self.runtime_tester.detect_framework()
        profiler = Profiler(str(self.repo_path))
        issues = profiler.profile(entry_script=entry_script, framework=framework)

        out = {
            "scan_id": f"profile-{uuid.uuid4().hex[:8]}",
            "type": "profile",
            "framework": framework,
            "entry_script": entry_script,
            "total_issues": len(issues),
            "issues": issues,
        }
        out_path = self.repo_path / "data" / "last_profile_scan.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"[Main] Profile scan saved: {len(issues)} hotspot(s) -> {out_path}")
        return out

    # ── Step 1c: Runtime scan ─────────────────────────────────────────────────

    def runtime_scan(self, run_command: str = "") -> dict:
        """
        Run the project and collect runtime errors.
        Returns a scan-result-like dict compatible with the planner.
        """
        result = self.runtime_tester.scan(run_command=run_command)
        issues = [i.to_dict() for i in result.issues]

        # Save alongside static scan
        out = {
            "scan_id": f"runtime-{uuid.uuid4().hex[:8]}",
            "type": "runtime",
            "framework": result.framework,
            "run_command": result.run_command,
            "server_started": result.server_started,
            "routes_tested": result.routes_tested,
            "total_issues": len(issues),
            "issues": issues,
            "errors": result.errors,
        }
        out_path = self.repo_path / "data" / "last_runtime_scan.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"[Main] Runtime scan saved: {len(issues)} issue(s) -> {out_path}")
        return out

    # ── Step 1d: Doc scan (detect undocumented functions) ────────────────────

    def doc_scan(self) -> dict:
        """Scan for undocumented functions and return as issue list."""
        from agents.doc_writer import DocWriterAgent
        agent = DocWriterAgent()
        issues = agent.scan_missing_docs(str(self.repo_path))
        out = {
            "scan_id": f"docs-{uuid.uuid4().hex[:8]}",
            "type": "docs",
            "total_issues": len(issues),
            "issues": issues,
        }
        out_path = self.repo_path / "data" / "last_doc_scan.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"[Main] Doc scan: {len(issues)} undocumented function(s).")
        return out

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fix_dependents(
        self,
        changed_file: str,
        original_code: str,
        new_code: str,
        issue: dict,
    ) -> list[dict]:
        """
        After patching changed_file, check if any dependent files import symbols
        that were renamed or removed. Ask the coder to update each dependent.
        Only runs when issue_type is 'undefined_name' or function_name changed.
        Returns list of {file, diff} for UI display.
        """
        # Only bother for issues that could change a public API
        risky_types = {"undefined_name", "dead_code", "complexity", "security"}
        if issue.get("issue_type") not in risky_types:
            return []

        try:
            from core.graph import DependencyGraph
            dg = DependencyGraph(str(self.repo_path))
            dg.build()
            radius = dg.get_impact_radius(changed_file)
            dependents = radius.get("direct_dependents", [])[:3]  # max 3 callers
            if not dependents:
                return []

            cross_diffs = []
            for dep_file in dependents:
                dep_path = self.repo_path / dep_file
                if not dep_path.exists():
                    continue
                dep_content = dep_path.read_text(encoding="utf-8", errors="ignore")

                # Ask coder to update the dependent given the changed file
                dep_issue = {
                    "file": dep_file,
                    "line": 1,
                    "issue_type": "cross_file_update",
                    "severity": "low",
                    "language": issue.get("language", "python"),
                    "description": (
                        f"File '{changed_file}' was modified. Update any imports or calls "
                        f"in this file that may have been affected."
                    ),
                    "fix_strategy": (
                        f"Review imports and usages of symbols from '{changed_file}'. "
                        f"Update or remove any references that no longer match the new version."
                    ),
                }
                rag_ctx = self.rag.search_for_issue(dep_issue, n_results=3)
                result = self.coder.generate_fix(
                    repo_path=str(self.repo_path),
                    issue=dep_issue,
                    rag_context=rag_ctx,
                )
                if result.success and result.generated_code and result.generated_code != dep_content.strip():
                    patch = self.patcher.patch_full_file(dep_file, result.generated_code)
                    if patch.success:
                        diff = self.git.get_file_diff(dep_content, patch.patched_code)
                        cross_diffs.append({"file": dep_file, "diff": diff})
                        print(f"[Main] Cross-file updated: {dep_file}")
            return cross_diffs
        except Exception as e:
            print(f"[Main] Cross-file fix error (non-fatal): {e}")
            return []

    def _find_existing_tests(self, source_file: str) -> Optional[str]:
        """
        Look for an existing test file for source_file.
        Returns path string if found, else None.
        """
        from pathlib import Path as P
        stem = P(source_file).stem
        suffix = P(source_file).suffix.lower()
        candidates = [
            self.repo_path / "tests" / f"test_{stem}.py",
            self.repo_path / "tests" / f"{stem}.test.js",
            self.repo_path / "tests" / f"{stem}.test.ts",
            self.repo_path / "tests" / f"{stem}Test.java",
            self.repo_path / "tests" / f"{stem}_test.go",
            self.repo_path / f"{stem}_test.go",          # Go convention: same dir
            self.repo_path / f"{stem}.test.js",
            self.repo_path / f"{stem}.test.ts",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        # Also check tests/ directory for any file matching *stem*
        tests_dir = self.repo_path / "tests"
        if tests_dir.exists():
            for f in tests_dir.iterdir():
                if stem.lower() in f.name.lower() and f.suffix in (".py", ".js", ".ts", ".java", ".go"):
                    return str(f)
        return None

    # ── Step 2: Plan ──────────────────────────────────────────────────────────

    def plan(self, scan_results: dict) -> Plan:
        """Build prioritized fix plan from scan results."""
        issues = scan_results.get("issues", [])
        impact_radii = scan_results.get("impact_radii", {})
        scan_id = scan_results.get("scan_id", "unknown")

        # Get rejection history
        rejections = self.mem.get_rejections()

        # Get RAG context for all issues combined
        rag_context = []
        for issue in issues[:5]:  # top 5 for context
            hits = self.rag.search_for_issue(issue, n_results=2)
            rag_context.extend(hits)

        plan = self.planner.plan(
            scan_id=scan_id,
            issues=issues,
            impact_radii=impact_radii,
            rag_context=rag_context,
            rejections=rejections,
        )

        # Save plan to JSON
        plan_path = self.repo_path / "data" / "last_plan.json"
        plan_path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
        print(f"[Main] Plan saved to {plan_path}")

        return plan

    # ── Step 3: Fix single issue ──────────────────────────────────────────────

    def fix_issue(self, issue: dict, scan_id: str) -> dict:
        """
        Run the full fix pipeline for a single issue.
        Returns result dict with status, diff, test results.

        Flow:
        1. Create git branch
        2. Snapshot file
        3. Coder → Reviewer loop
        4. Apply patch
        5. Write tests
        6. Run tests (self-healing)
        7. Return result for user approval
        """
        filepath = issue.get("file", "")
        result = {
            "issue": issue,
            "scan_id": scan_id,
            "fix_branch": None,
            "status": "failed",
            "diff": "",
            "explanation": "",
            "confidence": 0.0,
            "test_file": "",
            "test_result": None,
            "error": "",
            "fix_id": None,
        }

        # Create fix branch
        branch = self.git.create_fix_branch(scan_id[:8])
        result["fix_branch"] = branch

        # Snapshot original
        self.git.snapshot(filepath)
        original_code = self.git.read_file(filepath) or ""

        try:
            # Get RAG context for this issue
            rag_context = self.rag.search_for_issue(issue, n_results=5)

            # Coder -> Reviewer loop
            print(f"\n[Main] Starting Coder->Reviewer loop for {filepath}...")
            final_review, fixed_code, function_name = self.reviewer.review_loop(
                issue=issue,
                coder=self.coder,
                repo_path=str(self.repo_path),
                rag_context=rag_context,
                max_iterations=config.MAX_REVIEW_ITERATIONS,
            )

            if not fixed_code:
                result["error"] = "No fix generated after review loop."
                self.git.rollback_branch(branch, "master")
                return result

            result["explanation"] = final_review.feedback
            result["confidence"] = getattr(final_review, "confidence", 0.0)

            # Apply patch — use function-level patching when possible (safer)
            print(f"[Main] Applying patch to {filepath}...")
            if function_name:
                patch_result = self.patcher.patch_function(filepath, function_name, fixed_code)
            else:
                patch_result = self.patcher.patch_full_file(filepath, fixed_code)

            if not patch_result.success:
                result["error"] = f"Patch failed: {patch_result.error}"
                self.git.rollback_file(filepath)
                self.git.rollback_branch(branch, "master")
                return result

            # Generate diff for UI
            result["diff"] = self.git.get_file_diff(original_code, patch_result.patched_code)

            # ── Rename propagation ────────────────────────────────────────────
            # Detect if any top-level functions/classes were renamed, then
            # propagate the rename across all files in the repo.
            try:
                from core.rename_propagator import detect_renames, propagate_renames
                renames = detect_renames(original_code, patch_result.patched_code, filepath)
                if renames:
                    rename_desc = ", ".join(f"{r.old_name}->{r.new_name}" for r in renames)
                    print(f"[Main] Detected rename(s): {rename_desc}")
                    prop = propagate_renames(str(self.repo_path), renames, filepath)
                    result["renames"] = [{"old": r.old_name, "new": r.new_name, "kind": r.kind} for r in renames]
                    result["rename_files_updated"] = prop.files_updated
                    result["rename_replacements"] = prop.replacements
                    if prop.files_updated:
                        print(f"[Main] Rename propagated to {len(prop.files_updated)} file(s), "
                              f"{prop.replacements} replacement(s) total.")
            except Exception as e:
                print(f"[Main] Rename propagation error (non-fatal): {e}")

            # ── Cross-file fix: update callers if signature changed ───────────
            cross_file_diffs = self._fix_dependents(filepath, original_code, patch_result.patched_code, issue)
            if cross_file_diffs:
                result["cross_file_diffs"] = cross_file_diffs
                print(f"[Main] Cross-file: updated {len(cross_file_diffs)} dependent file(s).")

            # ── Existing test validation ──────────────────────────────────────
            # Run any pre-existing tests BEFORE writing new ones.
            # If the patch breaks existing tests, roll back immediately.
            test_framework = self.executor.detect_test_framework(filepath)
            existing_test_path = self._find_existing_tests(filepath)
            if existing_test_path:
                print(f"[Main] Validating patch against existing tests: {existing_test_path}")
                pre_result = self.executor.run_tests(
                    existing_test_path, framework=test_framework, timeout=60
                )
                if not pre_result.passed and pre_result.total > 0:
                    print(f"[Main] Existing tests BROKE after patch - rolling back.")
                    self.git.rollback_file(filepath)
                    self.git.rollback_branch(branch, "master")
                    result["error"] = (
                        f"Patch broke existing tests: {pre_result.failure_details()[:300]}"
                    )
                    result["status"] = "tests_failed"
                    return result
                print(f"[Main] Existing tests OK: {pre_result.summary()}")

            # Write tests
            print(f"[Main] Writing tests for {filepath}...")
            test_result = self.test_writer.write_tests(
                repo_path=str(self.repo_path),
                source_file=filepath,
                write_to_disk=True,
            )
            result["test_file"] = test_result.test_file

            # test_framework already detected above; pick test path for new tests
            test_path = test_result.test_file if test_result.success else str(self.repo_path / "tests")

            # Run tests with self-healing
            print(f"[Main] Running {test_framework} tests...")

            def heal_fn(test_res, attempt):
                """Self-healing: ask coder to fix the failing fix with full test context."""
                failure_info = test_res.failure_details()
                heal_issue = dict(issue)
                heal_issue["description"] = issue.get("description", "")
                heal_issue["fix_strategy"] = (
                    f"The previous fix caused test failures (attempt {attempt}). "
                    f"Test output:\n{failure_info[:600]}\n"
                    f"Fix the code so that all tests pass without breaking functionality."
                )
                heal_result = self.coder.generate_fix(
                    repo_path=str(self.repo_path),
                    issue=heal_issue,
                    rag_context=rag_context,
                    reviewer_feedback=f"Tests failed:\n{failure_info[:400]}",
                    previous_attempt=fixed_code,
                )
                if heal_result.success:
                    if heal_result.function_name:
                        self.patcher.patch_function(filepath, heal_result.function_name, heal_result.generated_code)
                    else:
                        self.patcher.patch_full_file(filepath, heal_result.generated_code)
                    return True
                return False

            healing = self.executor.run_with_healing(
                test_path=test_path,
                max_attempts=config.MAX_HEAL_ATTEMPTS,
                heal_fn=heal_fn,
                framework=test_framework,
            )

            result["test_result"] = healing.final_result.to_dict() \
                if hasattr(healing.final_result, "to_dict") \
                else {"passed": healing.final_result.passed,
                      "summary": healing.final_result.summary()}

            if healing.final_result.passed:
                result["status"] = "ready_for_approval"
                print(f"[Main] Fix ready for approval. Tests passed.")
            else:
                result["status"] = "tests_failed"
                result["error"] = healing.final_result.failure_details()
                print(f"[Main] Tests failed after {healing.attempts} attempt(s). Rolling back.")
                self.git.rollback_file(filepath)
                self.git.rollback_branch(branch, "master")

            # Save fix to SQLite
            fix_record = FixRecord(
                issue_id=issue.get("id", 0),
                scan_id=scan_id,
                file=filepath,
                original_code=original_code,
                fixed_code=patch_result.patched_code,
                fix_description=result["explanation"],
                status=result["status"],
                test_result="passed" if healing.final_result.passed else "failed",
            )
            fix_db_id = self.mem.save_fix(fix_record)
            result["fix_id"] = fix_db_id

            # Record outcome for evaluation metrics
            outcome = result["status"] if not healing.final_result.passed else "ready_for_approval"
            self.mem.record_fix_outcome(
                fix_id=fix_db_id,
                scan_id=scan_id,
                file=filepath,
                issue_type=issue.get("issue_type", ""),
                predicted_conf=result["confidence"],
                reviewer_iters=getattr(final_review, "iteration", 1),
                outcome=outcome,
            )

            return result

        except QuotaExhaustedException:
            self.git.rollback_branch(branch, "master")
            raise  # propagate up to stop the auto-fix loop
        except Exception as e:
            print(f"[Main] Fix pipeline error: {e}")
            self.git.rollback_file(filepath)
            try:
                self.git.rollback_branch(branch, "master")
            except Exception:
                pass
            result["error"] = str(e)
            return result

    # ── Step 4: Merge approved fix ────────────────────────────────────────────

    def apply_fix(self, fix_branch: str, fix_id: int, issue_id: int):
        """
        Called after user approves fix in UI.
        Stages, commits, and merges the fix branch into master.
        """
        print(f"[Main] Applying approved fix: branch={fix_branch}")
        self.git.checkout_branch(fix_branch)
        self.git.stage_all()
        self.git.commit(f"Agent fix: {fix_branch}")
        success = self.git.merge_branch(fix_branch, "master")

        if success:
            self.mem.update_fix_status(fix_id, "applied", "passed")
            if issue_id:
                self.mem.update_issue_status(issue_id, "fixed")
            self.git.delete_branch(fix_branch, force=True)
            # Update outcome to confirmed "applied"
            fix = self.mem.get_fix_by_id(fix_id)
            if fix:
                self.mem.record_fix_outcome(
                    fix_id=fix_id, scan_id=fix.get("scan_id", ""),
                    file=fix.get("file", ""), issue_type="",
                    predicted_conf=0.0, reviewer_iters=1,
                    outcome="applied",
                )
            print(f"[Main] Fix applied and merged successfully.")
        else:
            print(f"[Main] Merge failed.")

        return success

    def reject_fix(self, fix_id: int, fix_branch: str, issue: dict, reason: str = ""):
        """Called when user clicks Reject in the UI — marks as false positive."""
        self.mem.update_fix_status(fix_id, "rejected")
        fix = self.mem.get_fix_by_id(fix_id) or {}
        self.mem.record_fix_outcome(
            fix_id=fix_id, scan_id=fix.get("scan_id", ""),
            file=fix.get("file", issue.get("file", "")),
            issue_type=issue.get("issue_type", ""),
            predicted_conf=0.0, reviewer_iters=1,
            outcome="rejected", false_positive=True,
        )
        if reason:
            self.mem.save_rejection(
                file=issue.get("file", ""), issue_type=issue.get("issue_type", ""),
                description=issue.get("description", ""), reason=reason,
            )
        try:
            self.git.rollback_branch(fix_branch, "master")
        except Exception:
            pass
        print(f"[Main] Fix #{fix_id} rejected as false positive.")

    # ── Step 4b: Rollback applied fix ────────────────────────────────────────

    def rollback_fix(self, fix_id: int) -> bool:
        """
        Restore a file to its original_code from a previously applied fix.
        Writes the original content back, creates a revert commit, updates DB.
        """
        fix = self.mem.get_fix_by_id(fix_id)
        if not fix:
            print(f"[Main] Rollback: fix {fix_id} not found.")
            return False

        filepath = fix.get("file", "")
        original_code = fix.get("original_code", "")
        if not filepath or not original_code:
            return False

        full_path = self.repo_path / filepath
        try:
            full_path.write_text(original_code, encoding="utf-8")
            # Commit the revert
            self.git.stage_all()
            self.git.commit(f"Revert fix #{fix_id}: {filepath}")
            self.mem.update_fix_status(fix_id, "rolled_back")
            print(f"[Main] Rolled back fix #{fix_id} for {filepath}")
            return True
        except Exception as e:
            print(f"[Main] Rollback error: {e}")
            return False

    # ── Step 5: Reject fix ────────────────────────────────────────────────────

    def reject_fix(self, issue: dict, fix_branch: str, fix_id: int, reason: str = ""):
        """Called when user rejects a fix in UI."""
        self.git.rollback_branch(fix_branch, "master")
        self.mem.save_rejection(
            file=issue.get("file", ""),
            issue_type=issue.get("issue_type", ""),
            description=issue.get("description", ""),
            reason=reason,
        )
        if fix_id:
            self.mem.update_fix_status(fix_id, "rolled_back")
        print(f"[Main] Fix rejected and logged to memory.")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return self.mem.get_stats()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Codebase Self-Evolving Agent")
    parser.add_argument("repo", help="Path to the repository")
    parser.add_argument("--scan", action="store_true", help="Run a scan")
    parser.add_argument("--full", action="store_true", help="Full scan (not just changed files)")
    parser.add_argument("--plan", action="store_true", help="Generate fix plan after scan")
    parser.add_argument("--stats", action="store_true", help="Show stats")
    args = parser.parse_args()

    agent = CodebaseAgent(args.repo)

    if args.stats:
        print(f"\n-- Stats -------------------------------------------------")
        stats = agent.stats()
        for k, v in stats.items():
            print(f"  {k}: {v}")

    if args.scan or args.plan:
        print("\n-- Running Scan ------------------------------------------")
        scan_results = agent.scan(full_scan=args.full)
        print(f"Found {scan_results['total_issues']} issue(s).")

        if args.plan:
            print("\n-- Generating Plan ---------------------------------------")
            plan = agent.plan(scan_results)
            print(f"\nSummary  : {plan.summary}")
            print(f"Actionable: {len(plan.actionable())}")
            print(f"Skipped  : {len(plan.skipped)}")
            print("\nTop 5 items:")
            for item in plan.actionable()[:5]:
                print(f"  [{item.priority_score}] {item.severity.upper()} "
                      f"{item.file}:{item.line} - {item.description[:60]}")
