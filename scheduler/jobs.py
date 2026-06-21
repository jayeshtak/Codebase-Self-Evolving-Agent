import sys
import uuid
import json
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


def run_scan_job(repo_path: str, full_scan: bool = False):
    from agents.scanner import Scanner
    from core.graph import DependencyGraph
    from memory.chroma import CodebaseRAG
    from memory.sqlite import SQLiteMemory, IssueRecord

    scan_id = str(uuid.uuid4())[:8]
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[Jobs] Scan started: {scan_id} at {started}")
    print(f"[Jobs] Repo: {repo_path}")
    print(f"{'='*60}\n")

    mem = SQLiteMemory()
    mem.start_scan(scan_id, repo_path)

    try:
        print("[Jobs] Step 1/3 - Scanning...")
        scanner = Scanner(repo_path)
        issues_raw = scanner.scan(full_scan=full_scan)
        issues = [i.to_dict() for i in issues_raw]

        print("\n[Jobs] Step 2/3 - Building dependency graph...")
        dg = DependencyGraph(repo_path)
        dg.build()
        impact_radii = dg.get_all_impact_radii()

        print("\n[Jobs] Step 3/3 - Indexing codebase for RAG...")
        rag = CodebaseRAG(repo_path)
        changed_files = scanner.get_changed_files()
        rag.index_repo(changed_only=True, changed_files=changed_files)

        issue_records = []
        for issue in issues:
            record = IssueRecord(
                scan_id=scan_id,
                file=issue.get("file", ""),
                line=issue.get("line", 0),
                issue_type=issue.get("issue_type", ""),
                severity=issue.get("severity", "low"),
                description=issue.get("description", ""),
                language=issue.get("language", "python"),
                extra=issue.get("extra", {}),
            )
            issue_records.append(record)

        mem.save_issues_bulk(issue_records)
        mem.finish_scan(scan_id, total_issues=len(issues))
        mem.record_health_score(scan_id, repo_path, issues)

        results = {
            "scan_id": scan_id,
            "started_at": started,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "repo_path": repo_path,
            "total_issues": len(issues),
            "issues": issues,
            "impact_radii": impact_radii,
        }
        out_path = Path(repo_path) / "data" / "last_scan.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

        print(f"\n{'='*60}")
        print(f"[Jobs] Scan complete: {len(issues)} issue(s) found.")
        print(f"[Jobs] Results saved to {out_path}")
        print(f"{'='*60}\n")

        return results

    except Exception as e:
        print(f"[Jobs] Scan failed: {e}")
        mem.finish_scan(scan_id, total_issues=0)
        raise


class ScanScheduler:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.scheduler = BackgroundScheduler()
        self._running = False

    def start_daily(self, hour: int = None):
        hour = hour if hour is not None else config.DEFAULT_SCAN_HOUR
        self.scheduler.add_job(
            func=run_scan_job,
            trigger=CronTrigger(hour=hour, minute=0),
            kwargs={"repo_path": self.repo_path, "full_scan": False},
            id="daily_scan",
            name=f"Daily scan at {hour:02d}:00",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        print(f"[Scheduler] Daily scan scheduled at {hour:02d}:00")

    def start_interval(self, minutes: int = 60):
        self.scheduler.add_job(
            func=run_scan_job,
            trigger=IntervalTrigger(minutes=minutes),
            kwargs={"repo_path": self.repo_path, "full_scan": False},
            id="interval_scan",
            name=f"Scan every {minutes}min",
            replace_existing=True,
        )
        self.scheduler.start()
        self._running = True
        print(f"[Scheduler] Interval scan scheduled every {minutes} minute(s)")

    def run_now(self, full_scan: bool = False):
        print("[Scheduler] Running immediate scan...")
        return run_scan_job(self.repo_path, full_scan=full_scan)

    def stop(self):
        if self._running:
            self.scheduler.shutdown()
            self._running = False
            print("[Scheduler] Stopped.")

    def status(self) -> dict:
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time),
            })
        return {"running": self._running, "jobs": jobs}


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."

    scheduler = ScanScheduler(repo)
    results = scheduler.run_now(full_scan=True)

    print(f"\n-- Scan Results ------------------------------------------")
    print(f"Scan ID     : {results['scan_id']}")
    print(f"Total issues: {results['total_issues']}")
    print(f"Started     : {results['started_at']}")
    print(f"Finished    : {results['finished_at']}")

    print(f"\n-- Issues Found (first 5) --------------------------------")
    for i, issue in enumerate(results["issues"][:5], 1):
        print(f"[{i}] {issue['severity'].upper()} | {issue['issue_type']}")
        print(f"     {issue['file']}:{issue['line']} - {issue['description']}")
