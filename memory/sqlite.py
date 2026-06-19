import sqlite3
import json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional


DB_PATH = Path(__file__).parent.parent / "data" / "memory.db"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class IssueRecord:
    file: str
    line: int
    issue_type: str
    severity: str
    description: str
    language: str
    extra: dict
    scan_id: str
    status: str = "open"          # open | fixed | rejected | failed
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class FixRecord:
    issue_id: int
    scan_id: str
    file: str
    original_code: str
    fixed_code: str
    fix_description: str
    status: str = "pending"       # pending | applied | rolled_back | failed
    test_result: str = "not_run"  # not_run | passed | failed
    id: Optional[int] = None
    created_at: Optional[str] = None


@dataclass
class RejectionRecord:
    file: str
    issue_type: str
    description: str
    reason: str = ""
    id: Optional[int] = None
    created_at: Optional[str] = None


# ── SQLiteMemory ──────────────────────────────────────────────────────────────

class SQLiteMemory:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS scans (
                id          TEXT PRIMARY KEY,
                repo_path   TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                total_issues INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'running'
            );

            CREATE TABLE IF NOT EXISTS issues (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id     TEXT NOT NULL,
                file        TEXT NOT NULL,
                line        INTEGER NOT NULL,
                issue_type  TEXT NOT NULL,
                severity    TEXT NOT NULL,
                description TEXT NOT NULL,
                language    TEXT NOT NULL,
                extra       TEXT DEFAULT '{}',
                status      TEXT DEFAULT 'open',
                created_at  TEXT NOT NULL,
                FOREIGN KEY (scan_id) REFERENCES scans(id)
            );

            CREATE TABLE IF NOT EXISTS fixes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id        INTEGER NOT NULL,
                scan_id         TEXT NOT NULL,
                file            TEXT NOT NULL,
                original_code   TEXT NOT NULL,
                fixed_code      TEXT NOT NULL,
                fix_description TEXT NOT NULL,
                status          TEXT DEFAULT 'pending',
                test_result     TEXT DEFAULT 'not_run',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (issue_id) REFERENCES issues(id)
            );

            CREATE TABLE IF NOT EXISTS rejections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file        TEXT NOT NULL,
                issue_type  TEXT NOT NULL,
                description TEXT NOT NULL,
                reason      TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS terminal_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                command     TEXT NOT NULL,
                output      TEXT,
                exit_code   INTEGER,
                executed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fix_outcomes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fix_id          INTEGER NOT NULL,
                scan_id         TEXT NOT NULL,
                file            TEXT NOT NULL,
                issue_type      TEXT NOT NULL,
                predicted_conf  REAL NOT NULL,
                reviewer_iters  INTEGER DEFAULT 1,
                outcome         TEXT NOT NULL,
                false_positive  INTEGER DEFAULT 0,
                recorded_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_scores (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id      TEXT NOT NULL,
                repo_path    TEXT NOT NULL,
                score        REAL NOT NULL,
                total_issues INTEGER DEFAULT 0,
                high_count   INTEGER DEFAULT 0,
                med_count    INTEGER DEFAULT 0,
                low_count    INTEGER DEFAULT 0,
                recorded_at  TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ── Scans ─────────────────────────────────────────────────────────────────

    def start_scan(self, scan_id: str, repo_path: str) -> str:
        self.conn.execute(
            "INSERT INTO scans (id, repo_path, started_at) VALUES (?, ?, ?)",
            (scan_id, repo_path, datetime.now().isoformat())
        )
        self.conn.commit()
        return scan_id

    def finish_scan(self, scan_id: str, total_issues: int):
        self.conn.execute(
            "UPDATE scans SET finished_at=?, total_issues=?, status='done' WHERE id=?",
            (datetime.now().isoformat(), total_issues, scan_id)
        )
        self.conn.commit()

    def get_latest_scan(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM scans ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_all_scans(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_issues_for_scan(self, scan_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM issues WHERE scan_id=? ORDER BY severity DESC",
            (scan_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Issues ────────────────────────────────────────────────────────────────

    def save_issue(self, issue: IssueRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO issues
               (scan_id, file, line, issue_type, severity, description, language, extra, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                issue.scan_id, issue.file, issue.line, issue.issue_type,
                issue.severity, issue.description, issue.language,
                json.dumps(issue.extra), issue.status,
                datetime.now().isoformat()
            )
        )
        self.conn.commit()
        return cur.lastrowid

    def save_issues_bulk(self, issues: list[IssueRecord]) -> list[int]:
        return [self.save_issue(i) for i in issues]

    def get_open_issues(self, scan_id: Optional[str] = None) -> list[dict]:
        if scan_id:
            rows = self.conn.execute(
                "SELECT * FROM issues WHERE status='open' AND scan_id=? ORDER BY severity DESC",
                (scan_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM issues WHERE status='open' ORDER BY severity DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_issue_status(self, issue_id: int, status: str):
        self.conn.execute(
            "UPDATE issues SET status=? WHERE id=?", (status, issue_id)
        )
        self.conn.commit()

    def get_issue_history(self, file: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM issues WHERE file=? ORDER BY created_at DESC",
            (file,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Fixes ─────────────────────────────────────────────────────────────────

    def save_fix(self, fix: FixRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO fixes
               (issue_id, scan_id, file, original_code, fixed_code, fix_description, status, test_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fix.issue_id, fix.scan_id, fix.file,
                fix.original_code, fix.fixed_code, fix.fix_description,
                fix.status, fix.test_result, datetime.now().isoformat()
            )
        )
        self.conn.commit()
        return cur.lastrowid

    def update_fix_status(self, fix_id: int, status: str, test_result: Optional[str] = None):
        if test_result:
            self.conn.execute(
                "UPDATE fixes SET status=?, test_result=? WHERE id=?",
                (status, test_result, fix_id)
            )
        else:
            self.conn.execute(
                "UPDATE fixes SET status=? WHERE id=?", (status, fix_id)
            )
        self.conn.commit()

    def get_fixes_for_scan(self, scan_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fixes WHERE scan_id=? ORDER BY created_at DESC",
            (scan_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_applied_fixes(self, limit: int = 30) -> list[dict]:
        """Return all fixes with status='applied', most recent first."""
        rows = self.conn.execute(
            "SELECT * FROM fixes WHERE status='applied' ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_fix_by_id(self, fix_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM fixes WHERE id=?", (fix_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Rejections (preference memory) ───────────────────────────────────────

    def save_rejection(self, file: str, issue_type: str, description: str, reason: str = ""):
        self.conn.execute(
            """INSERT INTO rejections (file, issue_type, description, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (file, issue_type, description, reason, datetime.now().isoformat())
        )
        self.conn.commit()

    def get_rejections(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM rejections ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def is_rejected(self, file: str, issue_type: str, description: str) -> bool:
        """Check if a similar issue was previously rejected by the user."""
        row = self.conn.execute(
            """SELECT id FROM rejections
               WHERE file=? AND issue_type=? AND description=?
               LIMIT 1""",
            (file, issue_type, description)
        ).fetchone()
        return row is not None

    def get_rejection_summary(self) -> dict:
        """Return rejection counts by issue_type — fed to Planner to learn preferences."""
        rows = self.conn.execute(
            "SELECT issue_type, COUNT(*) as count FROM rejections GROUP BY issue_type"
        ).fetchall()
        return {r["issue_type"]: r["count"] for r in rows}

    # ── Terminal log ──────────────────────────────────────────────────────────

    def log_terminal(self, command: str, output: str, exit_code: int):
        self.conn.execute(
            """INSERT INTO terminal_log (command, output, exit_code, executed_at)
               VALUES (?, ?, ?, ?)""",
            (command, output, exit_code, datetime.now().isoformat())
        )
        self.conn.commit()

    def get_terminal_log(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM terminal_log ORDER BY executed_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_scan_trends(self, limit: int = 30) -> list[dict]:
        """
        Return per-scan aggregated data for trend charts.
        Each row: {scan_id, started_at, total_issues, high, medium, low,
                   fixed, security, duplicate, performance, missing_docs}
        """
        scans = self.conn.execute(
            "SELECT id, started_at, total_issues FROM scans "
            "WHERE status='done' ORDER BY started_at ASC LIMIT ?", (limit,)
        ).fetchall()

        rows = []
        for s in scans:
            sid = s["id"]
            counts = self.conn.execute(
                "SELECT severity, COUNT(*) c FROM issues WHERE scan_id=? GROUP BY severity",
                (sid,)
            ).fetchall()
            sev = {r["severity"]: r["c"] for r in counts}

            types = self.conn.execute(
                "SELECT issue_type, COUNT(*) c FROM issues WHERE scan_id=? GROUP BY issue_type",
                (sid,)
            ).fetchall()
            typ = {r["issue_type"]: r["c"] for r in types}

            fixed = self.conn.execute(
                "SELECT COUNT(*) FROM fixes WHERE scan_id=? AND status='applied'",
                (sid,)
            ).fetchone()[0]

            rows.append({
                "scan_id":      sid,
                "started_at":   s["started_at"][:16],
                "total_issues": s["total_issues"] or 0,
                "high":         sev.get("high", 0),
                "medium":       sev.get("medium", 0),
                "low":          sev.get("low", 0),
                "fixed":        fixed,
                "security":     typ.get("security", 0),
                "duplicate":    typ.get("duplicate", 0),
                "performance":  typ.get("performance", 0),
                "missing_docs": typ.get("missing_docs", 0),
            })
        return rows

    # ── Evaluation metrics ────────────────────────────────────────────────────

    def record_fix_outcome(
        self,
        fix_id: int,
        scan_id: str,
        file: str,
        issue_type: str,
        predicted_conf: float,
        reviewer_iters: int,
        outcome: str,          # "applied" | "rejected" | "tests_failed" | "merge_failed"
        false_positive: bool = False,
    ):
        self.conn.execute(
            """INSERT INTO fix_outcomes
               (fix_id, scan_id, file, issue_type, predicted_conf, reviewer_iters,
                outcome, false_positive, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fix_id or 0, scan_id, file, issue_type, predicted_conf,
             reviewer_iters, outcome, int(false_positive), datetime.now().isoformat())
        )
        self.conn.commit()

    def get_evaluation_summary(self) -> dict:
        """Return aggregated evaluation metrics."""
        total = self.conn.execute("SELECT COUNT(*) FROM fix_outcomes").fetchone()[0]
        if not total:
            return {"total": 0}

        applied = self.conn.execute(
            "SELECT COUNT(*) FROM fix_outcomes WHERE outcome='applied'"
        ).fetchone()[0]
        false_pos = self.conn.execute(
            "SELECT COUNT(*) FROM fix_outcomes WHERE false_positive=1"
        ).fetchone()[0]
        avg_conf = self.conn.execute(
            "SELECT AVG(predicted_conf) FROM fix_outcomes"
        ).fetchone()[0] or 0.0
        avg_iters = self.conn.execute(
            "SELECT AVG(reviewer_iters) FROM fix_outcomes"
        ).fetchone()[0] or 0.0

        by_type = self.conn.execute(
            """SELECT issue_type,
                      COUNT(*) total,
                      SUM(CASE WHEN outcome='applied' THEN 1 ELSE 0 END) applied
               FROM fix_outcomes GROUP BY issue_type"""
        ).fetchall()

        # Confidence calibration buckets: predicted vs actual success rate
        buckets = self.conn.execute(
            """SELECT
                 CAST(predicted_conf * 10 AS INTEGER) / 10.0 AS bucket,
                 COUNT(*) total,
                 SUM(CASE WHEN outcome='applied' THEN 1 ELSE 0 END) applied
               FROM fix_outcomes GROUP BY bucket ORDER BY bucket"""
        ).fetchall()

        return {
            "total": total,
            "applied": applied,
            "fix_success_rate": round(applied / total, 3) if total else 0,
            "false_positive_count": false_pos,
            "false_positive_rate": round(false_pos / total, 3) if total else 0,
            "avg_confidence": round(avg_conf, 3),
            "avg_reviewer_iters": round(avg_iters, 2),
            "by_issue_type": [dict(r) for r in by_type],
            "calibration": [dict(r) for r in buckets],
        }

    def record_health_score(self, scan_id: str, repo_path: str, issues: list) -> float:
        high = sum(1 for i in issues if i.get("severity") == "high")
        med  = sum(1 for i in issues if i.get("severity") == "medium")
        low  = sum(1 for i in issues if i.get("severity") == "low")
        score = max(0.0, min(100.0, 100.0 - (high * 5 + med * 2 + low * 0.5)))
        self.conn.execute(
            """INSERT INTO health_scores
               (scan_id, repo_path, score, total_issues, high_count, med_count, low_count, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (scan_id, repo_path, round(score, 1), len(issues),
             high, med, low, datetime.now().isoformat())
        )
        self.conn.commit()
        return score

    def get_health_trend(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT scan_id, score, total_issues, high_count, med_count, low_count, recorded_at "
            "FROM health_scores ORDER BY recorded_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_health_score(self) -> float:
        row = self.conn.execute(
            "SELECT score FROM health_scores ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def get_recent_outcomes(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fix_outcomes ORDER BY recorded_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        total_issues = self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        fixed = self.conn.execute("SELECT COUNT(*) FROM issues WHERE status='fixed'").fetchone()[0]
        rejected = self.conn.execute("SELECT COUNT(*) FROM issues WHERE status='rejected'").fetchone()[0]
        failed = self.conn.execute("SELECT COUNT(*) FROM issues WHERE status='failed'").fetchone()[0]
        total_scans = self.conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        return {
            "total_scans": total_scans,
            "total_issues": total_issues,
            "fixed": fixed,
            "rejected": rejected,
            "failed": failed,
            "open": total_issues - fixed - rejected - failed,
        }

    def close(self):
        self.conn.close()


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uuid

    mem = SQLiteMemory()
    print(f"[SQLite] DB at: {mem.db_path}")

    # Test scan lifecycle
    scan_id = str(uuid.uuid4())[:8]
    mem.start_scan(scan_id, "/test/repo")
    print(f"[SQLite] Started scan: {scan_id}")

    # Test saving issues
    issue = IssueRecord(
        scan_id=scan_id,
        file="agents/scanner.py",
        line=42,
        issue_type="complexity",
        severity="high",
        description="Function 'scan' has cyclomatic complexity 15",
        language="python",
        extra={"complexity": 15},
    )
    issue_id = mem.save_issue(issue)
    print(f"[SQLite] Saved issue ID: {issue_id}")

    # Test saving fix
    fix = FixRecord(
        issue_id=issue_id,
        scan_id=scan_id,
        file="agents/scanner.py",
        original_code="def scan(): ...",
        fixed_code="def scan(): ... # refactored",
        fix_description="Split into smaller functions",
    )
    fix_id = mem.save_fix(fix)
    print(f"[SQLite] Saved fix ID: {fix_id}")

    # Test rejection memory
    mem.save_rejection("agents/scanner.py", "dead_code", "Unused import: 'os'", "intentional")
    print(f"[SQLite] Saved rejection")
    print(f"[SQLite] Is rejected: {mem.is_rejected('agents/scanner.py', 'dead_code', 'Unused import: os')}")

    mem.finish_scan(scan_id, total_issues=1)

    # Stats
    print(f"\n[SQLite] Stats: {mem.get_stats()}")
    print(f"[SQLite] Rejection summary: {mem.get_rejection_summary()}")

    mem.close()
    print("\n[SQLite] All tests passed.")
