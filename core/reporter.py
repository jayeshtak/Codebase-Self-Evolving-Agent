"""
Generate an HTML report summarising scan results, fixes applied, and runtime issues.
"""
from datetime import datetime
from pathlib import Path


def _severity_badge(severity: str) -> str:
    styles = {
        "high": "background: rgba(239, 68, 68, 0.08); color: #B91C1C; border: 1px solid rgba(239, 68, 68, 0.2);",
        "medium": "background: rgba(245, 158, 11, 0.08); color: #B45309; border: 1px solid rgba(245, 158, 11, 0.2);",
        "low": "background: rgba(16, 185, 129, 0.08); color: #047857; border: 1px solid rgba(16, 185, 129, 0.2);"
    }
    style = styles.get(severity, "background: #F3F4F6; color: #374151; border: 1px solid #D1D5DB;")
    return f'<span style="padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; text-transform: uppercase; font-family: monospace; {style}">{severity}</span>'


def _status_badge(status: str) -> str:
    colors = {
        "applied": "background: #ECFDF5; color: #047857; border: 1px solid #A7F3D0;",
        "ready_for_approval": "background: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE;",
        "tests_failed": "background: #FEF2F2; color: #B91C1C; border: 1px solid #FCA5A5;",
        "failed": "background: #FEF2F2; color: #B91C1C; border: 1px solid #FCA5A5;",
        "rolled_back": "background: #F3F4F6; color: #374151; border: 1px solid #D1D5DB;",
        "open": "background: #FFFBEB; color: #B45309; border: 1px solid #FDE68A;",
    }
    style = colors.get(status, "background: #F3F4F6; color: #374151; border: 1px solid #D1D5DB;")
    return f'<span style="padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; text-transform: uppercase; font-family: monospace; {style}">{status}</span>'


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
        badge = _severity_badge(sev)
        issue_rows += f"""
        <tr>
            <td>{badge}</td>
            <td><span style="font-family: monospace; font-size: 11px; background: #F3F4F6; padding: 2px 4px; border-radius: 3px; border: 1px solid #E5E7EB;">{issue.get('issue_type','')}</span></td>
            <td><code>{issue.get('file','')}:{issue.get('line','')}</code></td>
            <td style="font-weight: 500;">{issue.get('description','')}</td>
        </tr>"""

    # ── Runtime issue rows ────────────────────────────────────────────────────
    rt_rows = ""
    for issue in runtime_issues:
        extra = issue.get("extra", {})
        ec = extra.get("error_class", issue.get("issue_type", ""))
        badge = _severity_badge("high")
        rt_rows += f"""
        <tr>
            <td>{badge}</td>
            <td><span style="font-family: monospace; font-size: 11px; background: #F3F4F6; padding: 2px 4px; border-radius: 3px; border: 1px solid #E5E7EB;">{ec}</span></td>
            <td><code>{issue.get('file','')}:{issue.get('line','')}</code></td>
            <td style="font-weight: 500;">{issue.get('description','')}</td>
        </tr>"""

    # ── Fix history rows ──────────────────────────────────────────────────────
    fix_rows = ""
    for entry in fix_history:
        badge = _status_badge(entry.get("status", ""))
        fix_rows += f"""
        <tr>
            <td>{badge}</td>
            <td><code>{entry.get('file','')}</code></td>
            <td style="font-weight: 500;">{entry.get('description','')}</td>
            <td style="font-family: monospace; color: #64748B; font-size: 11px;">{entry.get('time','')}</td>
        </tr>"""

    # ── Plan summary ──────────────────────────────────────────────────────────
    plan_section = ""
    if plan:
        summary_text = getattr(plan, "summary", "") or ""
        plan_section = f"""
        <h2>Fix Plan</h2>
        <p style="font-size: 13px; color: #4B5563; margin-bottom: 12px; line-height: 1.5;">{summary_text}</p>
        <div style="display: flex; gap: 12px; margin-bottom: 24px;">
            <div style="background: #F3F4F6; border: 1px solid #E5E7EB; border-radius: 6px; padding: 10px 16px; font-size: 12px;">
                Actionable: <strong>{len(plan.actionable()) if hasattr(plan,'actionable') else 0}</strong>
            </div>
            <div style="background: #F3F4F6; border: 1px solid #E5E7EB; border-radius: 6px; padding: 10px 16px; font-size: 12px;">
                Skipped: <strong>{len(plan.skipped) if hasattr(plan,'skipped') else 0}</strong>
            </div>
        </div>"""

    high = sum(1 for i in issues if i.get("severity") == "high")
    med  = sum(1 for i in issues if i.get("severity") == "medium")
    low  = sum(1 for i in issues if i.get("severity") == "low")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Codebase Agent Report — {now}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,100..900;1,9..144,100..900&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif;
          background: #F4F6F8; color: #1F2937; padding: 32px; }}
  h1 {{ font-family: 'Fraunces', serif; font-size: 32px; font-weight: 800; margin-bottom: 4px; color: #111827; }}
  h2 {{ font-family: 'Fraunces', serif; font-size: 20px; font-weight: 700; margin: 32px 0 12px; color: #111827; border-bottom: 1px solid #E5E7EB; padding-bottom: 6px; }}
  .meta {{ color: #6B7280; font-size: 12px; margin-bottom: 32px; font-family: monospace; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }}
  .card {{ background: white; border-radius: 8px; padding: 18px 24px; min-width: 140px;
           flex: 1; border: 1px solid #E5E7EB; }}
  .card .num {{ font-family: 'Fraunces', serif; font-size: 36px; font-weight: 700; color: #111827; }}
  .card .lbl {{ font-size: 11px; color: #6B7280; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 8px; overflow: hidden; border: 1px solid #E5E7EB; }}
  th {{ background: #F9FAFB; color: #374151; padding: 12px 16px; text-align: left; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #E5E7EB; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #E5E7EB; font-size: 12px; color: #374151; }}
  tr:last-child td {{ border-bottom: none; }}
  code {{ background: #F3F4F6; padding: 2px 6px; border-radius: 4px; font-size: 11px; border: 1px solid #E5E7EB; font-family: monospace; color: #111827; }}
  .empty {{ background: white; border-radius: 8px; padding: 24px;
            color: #6B7280; font-size: 12px;
            border: 1px solid #E5E7EB; text-align: center; }}
  .footer {{ margin-top: 48px; text-align: center; color: #9CA3AF; font-size: 11px; border-top: 1px solid #E5E7EB; padding-top: 24px; }}
</style>
</head>
<body>
<h1>Codebase Agent Report</h1>
<p class="meta">Generated: {now} &nbsp;|&nbsp; Repo: {repo_path} &nbsp;|&nbsp; Scan ID: {scan_id}</p>

<div class="cards">
  <div class="card"><div class="num">{len(issues)}</div><div class="lbl">Static Issues</div></div>
  <div class="card"><div class="num" style="color:#DC2626">{high}</div><div class="lbl">High Severity</div></div>
  <div class="card"><div class="num" style="color:#D97706">{med}</div><div class="lbl">Medium Severity</div></div>
  <div class="card"><div class="num" style="color:#059669">{low}</div><div class="lbl">Low Severity</div></div>
  <div class="card"><div class="num">{len(runtime_issues)}</div><div class="lbl">Runtime Issues</div></div>
  <div class="card"><div class="num" style="color:#059669">{stats.get('fixed',0)}</div><div class="lbl">Fixes Applied</div></div>
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

<div class="footer">Generated by Codebase Agent &nbsp;·&nbsp; MCAP21 Mini Project 1B</div>
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

