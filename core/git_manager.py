import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

import git


class GitError(Exception):
    """Base exception for GitManager operations."""
    pass


class DirtyWorkingTreeError(GitError):
    """Raised when an operation requires a clean working tree but dirty changes exist."""
    pass


# ── GitManager ────────────────────────────────────────────────────────────────

class GitManager:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        try:
            self.repo = git.Repo(self.repo_path)
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            print(f"[Git] Path '{repo_path}' is not a git repository. Initializing new git repository...")
            self.repo = git.Repo.init(self.repo_path, mkdir=True)

        # In-memory snapshot store: {filepath: original_content}
        self._snapshots: dict[str, str] = {}

        # Ensure user.name and user.email are set locally if missing
        try:
            with self.repo.config_reader() as reader:
                has_email = reader.has_option("user", "email")
                has_name = reader.has_option("user", "name")
        except Exception:
            has_email = False
            has_name = False

        if not has_email or not has_name:
            try:
                with self.repo.config_writer() as writer:
                    if not has_name:
                        writer.set_value("user", "name", "Codebase Agent")
                    if not has_email:
                        writer.set_value("user", "email", "agent@example.com")
                print("[Git] Configured local git user identity (name/email).")
            except Exception as e:
                print(f"[Git] Failed to configure local git user: {e}")

        # Check if HEAD is valid (has at least one commit)
        try:
            head_valid = self.repo.head.is_valid()
        except Exception:
            head_valid = False

        if not head_valid:
            print("[Git] Repository has no commits. Creating initial commit to resolve HEAD...")
            try:
                self.repo.git.add(A=True)
                if len(self.repo.index.entries) == 0:
                    readme = self.repo_path / "README.md"
                    if not readme.exists():
                        readme.write_text("# Initialized Repository\n", encoding="utf-8")
                    self.repo.git.add("README.md")
                self.repo.index.commit("Initial commit by Codebase Agent")
                print("[Git] Initial commit created successfully.")
            except Exception as e:
                print(f"[Git] Failed to create initial commit: {e}")

        # Detect the default target branch
        self.default_branch = "master"
        try:
            if not self.repo.head.is_detached:
                self.default_branch = self.repo.active_branch.name
            else:
                head_names = [h.name for h in self.repo.heads]
                if "main" in head_names:
                    self.default_branch = "main"
                elif "master" in head_names:
                    self.default_branch = "master"
                elif head_names:
                    self.default_branch = head_names[0]
        except Exception:
            pass
        print(f"[Git] Detected default branch: {self.default_branch}")

    # ── Working tree safeguards ───────────────────────────────────────────────

    def has_uncommitted_changes(self, include_untracked: bool = False) -> bool:
        """Check if there are uncommitted modifications in the working tree."""
        try:
            return self.repo.is_dirty(untracked_files=include_untracked)
        except Exception:
            return False

    def get_uncommitted_files(self) -> list[str]:
        """Return list of modified or uncommitted files in working tree."""
        try:
            diff_index = [item.a_path for item in self.repo.index.diff("HEAD")]
            diff_working = [item.a_path for item in self.repo.index.diff(None)]
            return list(set(diff_index + diff_working))
        except Exception:
            return []

    # ── Branch management ─────────────────────────────────────────────────────

    def current_branch(self) -> str:
        try:
            return self.repo.active_branch.name
        except TypeError:
            return "DETACHED"

    def create_fix_branch(self, scan_id: str, allow_dirty: bool = False) -> str:
        """
        Create and checkout a new branch for this fix cycle.
        Refuses to run if dirty uncommitted changes exist (protects user work).
        """
        if not allow_dirty and self.has_uncommitted_changes():
            dirty_files = self.get_uncommitted_files()
            raise DirtyWorkingTreeError(
                f"Cannot create fix branch: uncommitted changes in {len(dirty_files)} file(s). "
                "Commit or stash changes before applying fixes."
            )
        branch_name = f"agent-fix-{scan_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        branch = self.repo.create_head(branch_name)
        branch.checkout()
        print(f"[Git] Created and checked out branch: {branch_name}")
        return branch_name

    def checkout_branch(self, branch_name: str):
        self.repo.git.checkout(branch_name)
        print(f"[Git] Checked out: {branch_name}")

    def delete_branch(self, branch_name: str, force: bool = False):
        try:
            self.repo.delete_head(branch_name, force=force)
            print(f"[Git] Deleted branch: {branch_name}")
        except Exception as e:
            print(f"[Git] Could not delete branch {branch_name}: {e}")

    def list_branches(self) -> list[str]:
        return [b.name for b in self.repo.heads]

    # ── Snapshots ─────────────────────────────────────────────────────────────

    def snapshot(self, filepath: str):
        """Save original file content before any modifications."""
        path = Path(filepath)
        if not path.is_absolute():
            path = self.repo_path / filepath
        if path.exists():
            self._snapshots[str(filepath)] = path.read_text(encoding="utf-8", errors="ignore")
            print(f"[Git] Snapshotted: {filepath}")
        else:
            print(f"[Git] Snapshot skipped (file not found): {filepath}")

    def restore_snapshot(self, filepath: str) -> bool:
        """Restore a file to its snapshotted content."""
        if filepath not in self._snapshots:
            print(f"[Git] No snapshot for: {filepath}")
            return False
        path = Path(filepath)
        if not path.is_absolute():
            path = self.repo_path / filepath
        path.write_text(self._snapshots[filepath], encoding="utf-8")
        print(f"[Git] Restored snapshot: {filepath}")
        return True

    def clear_snapshots(self):
        self._snapshots.clear()

    # ── File operations ───────────────────────────────────────────────────────

    def write_file(self, filepath: str, content: str):
        """Write content to a file, creating parent dirs if needed."""
        path = Path(filepath)
        if not path.is_absolute():
            path = self.repo_path / filepath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"[Git] Wrote file: {filepath}")

    def read_file(self, filepath: str) -> Optional[str]:
        path = Path(filepath)
        if not path.is_absolute():
            path = self.repo_path / filepath
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
        return None

    # ── Staging and committing ────────────────────────────────────────────────

    def stage_file(self, filepath: str):
        self.repo.index.add([filepath])
        print(f"[Git] Staged: {filepath}")

    def stage_all(self):
        self.repo.git.add(A=True)
        print("[Git] Staged all changes")

    def commit(self, message: str) -> str:
        """Commit staged changes. Returns commit hash."""
        if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
            # Nothing staged — check if there are any changes at all
            print("[Git] Nothing to commit.")
            return ""
        try:
            commit = self.repo.index.commit(message)
            print(f"[Git] Committed: {commit.hexsha[:8]} — {message}")
            return commit.hexsha
        except Exception as e:
            print(f"[Git] Commit failed: {e}")
            return ""

    # ── Diff ──────────────────────────────────────────────────────────────────

    def get_diff(self, filepath: Optional[str] = None) -> str:
        """Get unstaged diff for a file or entire repo."""
        try:
            if filepath:
                return self.repo.git.diff(filepath)
            return self.repo.git.diff()
        except Exception as e:
            return f"[Git] Diff error: {e}"

    def get_staged_diff(self) -> str:
        try:
            return self.repo.git.diff("--cached")
        except Exception as e:
            return f"[Git] Staged diff error: {e}"

    def get_file_diff(self, original: str, modified: str) -> str:
        """
        Generate a unified diff between two strings.
        Used to show before/after in the Streamlit UI.
        """
        import difflib
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines, mod_lines,
            fromfile="original",
            tofile="modified",
            lineterm=""
        )
        return "\n".join(diff)

    # ── Rollback ──────────────────────────────────────────────────────────────

    def rollback_file(self, filepath: str) -> bool:
        """
        Rollback a file using snapshot if available,
        otherwise use git checkout to restore from HEAD.
        """
        # Try snapshot first
        if filepath in self._snapshots:
            return self.restore_snapshot(filepath)

        # Fall back to git checkout
        try:
            self.repo.git.checkout("HEAD", "--", filepath)
            print(f"[Git] Rolled back via git checkout: {filepath}")
            return True
        except Exception as e:
            print(f"[Git] Rollback failed for {filepath}: {e}")
            return False

    def rollback_branch(self, fix_branch: str, original_branch: Optional[str] = None):
        """
        Abandon the fix branch entirely and return to original branch.
        Called when tests fail after max retries or user rejects fix.
        """
        if original_branch is None:
            original_branch = self.default_branch
        try:
            current = self.current_branch()
            if current == fix_branch:
                self.checkout_branch(original_branch)
            if fix_branch in self.list_branches():
                self.delete_branch(fix_branch, force=True)
            self.clear_snapshots()
            print(f"[Git] Rolled back to '{original_branch}', deleted '{fix_branch}'")
        except Exception as e:
            print(f"[Git] Branch rollback error: {e}")

    # ── Merge ─────────────────────────────────────────────────────────────────

    def merge_branch(self, fix_branch: str, target_branch: Optional[str] = None) -> bool:
        """
        Merge fix branch into target and push to remote. Called after user approves.
        """
        if target_branch is None:
            target_branch = self.default_branch

        if fix_branch not in self.list_branches():
            print(f"[Git] Merge failed: branch '{fix_branch}' does not exist.")
            return False

        try:
            self.checkout_branch(target_branch)
            self.repo.git.merge(fix_branch, "--no-ff",
                                m=f"Agent fix merge: {fix_branch}")
            print(f"[Git] Merged '{fix_branch}' into '{target_branch}'")
        except Exception as e:
            print(f"[Git] Merge failed: {e}")
            try:
                self.repo.git.merge("--abort")
            except Exception:
                pass
            return False

        # Push to remote if one is configured
        try:
            remote = self.repo.remote("origin")
            remote.push(target_branch)
            print(f"[Git] Pushed '{target_branch}' to origin")
        except ValueError:
            print("[Git] No remote 'origin' configured — skipping push")
        except Exception as e:
            print(f"[Git] Push failed (fix is merged locally): {e}")

        return True

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "branch": self.current_branch(),
            "branches": self.list_branches(),
            "is_dirty": self.repo.is_dirty(),
            "untracked": self.repo.untracked_files,
            "snapshots": list(self._snapshots.keys()),
        }


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    gm = GitManager(repo)

    print("── Git Status ────────────────────────────────────────────")
    print(gm.status())

    # Test branch creation
    branch = gm.create_fix_branch("test123")
    print(f"\n── Branches: {gm.list_branches()}")

    # Test snapshot + write + diff + rollback
    test_file = "config.py"
    gm.snapshot(test_file)

    original = gm.read_file(test_file) or ""
    test_content = original + "\n# agent test write\nTEST_VAR = True\n"
    gm.write_file(test_file, test_content)

    diff = gm.get_file_diff(original, test_content)
    print(f"\n── Diff ──────────────────────────────────────────────────")
    print(diff if diff else "(no diff)")

    # Rollback
    gm.rollback_file(test_file)
    restored = gm.read_file(test_file)
    print(f"\n── Rollback successful: {restored == original}")

    # Go back to default branch and clean up
    gm.checkout_branch(gm.default_branch)
    gm.delete_branch(branch, force=True)

    print(f"\n── Final status: {gm.status()}")
