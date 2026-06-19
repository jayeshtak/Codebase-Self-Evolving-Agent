"""
Generate an HTML report summarising scan results, fixes applied, and runtime issues.
"""
from datetime import datetime
from pathlib import Path


def _severity_color(severity: str) -> str:
    return {"high": "#e74c3c", "medium": "#f39c12", "low": "#27ae60"}.get(severity, "#95a5a6")


def _status_icon(status: str) -> str:
    return {"applied": "✅", "ready_for_approval": "🔄", "tests_failed": "❌",
            "failed": "❌", "rolled_back": "↩️", "open": "🔵"}.get(status, "⚪")


def generate_html_report(
    repo_path: str,
    scan_results: dict,
    plan: object = None,
    fix_history: list = None,
    runtime_results: dict = None,
    stats: dict = None,
) -> str:
    """
    Build a self-contained HTML report string.
    All data is embedded — no external assets needed.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    issues = scan_results.get("issues", []) if scan_results else []
    scan_id = scan_results.get("scan_id", "N/A") if scan_results else "N/A"
    fix_history = fix_history or []
    runtime_issues = (runtime_results or {}).get("issues", [])
    stats = stats or {}

    # ── Issue rows ────────────────────────────────────────────────────────────
    issue_rows = ""
    for issue in issues:
        sev = issue.get("severity", "low")
        color = _severity_color(sev)
        issue_rows += f"""
        <tr>
            <td><span style="color:{color};font-weight:bold">{sev.upper()}</span></td>
            <td>{issue.get('issue_type','')}</td>
            <td><code>{issue.get('file','')}:{issue.get('line','')}</code></td>
            <td>{issue.get('description','')[:100]}</td>
        </tr>"""

    # ── Runtime issue rows ────────────────────────────────────────────────────
    rt_rows = ""
    for issue in runtime_issues:
        extra = issue.get("extra", {})
        ec = extra.get("error_class", issue.get("issue_type", ""))
        rt_rows += f"""
        <tr>
            <td><span style="color:#e74c3c;font-weight:bold">HIGH</span></td>
            <td>{ec}</td>
            <td><code>{issue.get('file','')}:{issue.get('line','')}</code></td>
            <td>{issue.get('description','')[:100]}</td>
        </tr>"""

    # ── Fix history rows ──────────────────────────────────────────────────────
    fix_rows = ""
    for entry in fix_history:
        icon = _status_icon(entry.get("status", ""))
        fix_rows += f"""
        <tr>
            <td>{icon} {entry.get('status','')}</td>
            <td><code>{entry.get('file','')}</code></td>
            <td>{entry.get('description','')[:80]}</td>
            <td>{entry.get('time','')}</td>
        </tr>"""

    # ── Plan summary ──────────────────────────────────────────────────────────
    plan_section = ""
    if plan:
        summary_text = getattr(plan, "summary", "") or ""
        plan_section = f"""
        <h2>Fix Plan</h2>
        <p>{summary_text}</p>
        <ul>
            <li>Actionable: <strong>{len(plan.actionable()) if hasattr(plan,'actionable') else 0}</strong></li>
            <li>Skipped: <strong>{len(plan.skipped) if hasattr(plan,'skipped') else 0}</strong></li>
        </ul>"""

    high = sum(1 for i in issues if i.get("severity") == "high")
    med  = sum(1 for i in issues if i.get("severity") == "medium")
    low  = sum(1 for i in issues if i.get("severity") == "low")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Codebase Agent Report — {now}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f5f6fa; color: #2d3436; padding: 32px; }}
  h1 {{ font-size: 28px; margin-bottom: 4px; }}
  h2 {{ font-size: 18px; margin: 32px 0 12px; color: #636e72; border-bottom: 2px solid #dfe6e9; padding-bottom: 6px; }}
  .meta {{ color: #636e72; font-size: 13px; margin-bottom: 32px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }}
  .card {{ background: white; border-radius: 10px; padding: 20px 28px; min-width: 140px;
           box-shadow: 0 2px 8px rgba(0,0,0,.07); }}
  .card .num {{ font-size: 36px; font-weight: 700; }}
  .card .lbl {{ font-size: 13px; color: #636e72; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,.07); }}
  th {{ background: #2d3436; color: white; padding: 10px 14px; text-align: left; font-size: 13px; }}
  td {{ padding: 9px 14px; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
  tr:last-child td {{ border-bottom: none; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  .empty {{ background: white; border-radius: 10px; padding: 20px;
            color: #636e72; font-style: italic;
            box-shadow: 0 2px 8px rgba(0,0,0,.07); }}
  .footer {{ margin-top: 48px; text-align: center; color: #b2bec3; font-size: 12px; }}
</style>
</head>
<body>
<h1>🧬 Codebase Agent Report</h1>
<p class="meta">Generated: {now} &nbsp;|&nbsp; Repo: {repo_path} &nbsp;|&nbsp; Scan ID: {scan_id}</p>

<div class="cards">
  <div class="card"><div class="num">{len(issues)}</div><div class="lbl">Static Issues</div></div>
  <div class="card"><div class="num" style="color:#e74c3c">{high}</div><div class="lbl">High Severity</div></div>
  <div class="card"><div class="num" style="color:#f39c12">{med}</div><div class="lbl">Medium Severity</div></div>
  <div class="card"><div class="num" style="color:#27ae60">{low}</div><div class="lbl">Low Severity</div></div>
  <div class="card"><div class="num">{len(runtime_issues)}</div><div class="lbl">Runtime Issues</div></div>
  <div class="card"><div class="num" style="color:#27ae60">{stats.get('fixed',0)}</div><div class="lbl">Fixes Applied</div></div>
  <div class="card"><div class="num">{stats.get('total_scans',0)}</div><div class="lbl">Total Scans</div></div>
</div>

{plan_section}

<h2>Static Issues ({len(issues)})</h2>
{"<table><tr><th>Severity</th><th>Type</th><th>Location</th><th>Description</th></tr>" + issue_rows + "</table>"
  if issues else '<p class="empty">No static issues found.</p>'}

<h2>Runtime Issues ({len(runtime_issues)})</h2>
{"<table><tr><th>Severity</th><th>Error Class</th><th>Location</th><th>Description</th></tr>" + rt_rows + "</table>"
  if runtime_issues else '<p class="empty">No runtime issues found.</p>'}

<h2>Fix History ({len(fix_history)})</h2>
{"<table><tr><th>Status</th><th>File</th><th>Description</th><th>Time</th></tr>" + fix_rows + "</table>"
  if fix_history else '<p class="empty">No fixes applied yet.</p>'}

<div class="footer">Generated by Codebase Self-Evolving Agent &nbsp;·&nbsp; MCAP21 Mini Project 1B</div>
</body>
</html>"""
    return html


def save_report(repo_path: str, html: str) -> str:
    """Save report to data/report_<timestamp>.html and return the path."""
    out_dir = Path(repo_path) / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"report_{ts}.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
