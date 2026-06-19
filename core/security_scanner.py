"""
Security scanner: Bandit (Python), Semgrep (multi-language),
npm audit (JS/TS deps), safety (Python deps).

All methods return list[Issue]-compatible dicts that can be fed directly
into the existing scanner pipeline.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_pypi_version(package: str) -> Optional[str]:
    """Fetch the latest stable version of a PyPI package."""
    try:
        import urllib.request
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def _npm_latest_version(package: str) -> Optional[str]:
    """Fetch the latest npm package version."""
    try:
        import urllib.request
        url = f"https://registry.npmjs.org/{package}/latest"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("version")
    except Exception:
        return None


def _run(cmd: list[str], cwd: str, timeout: int = 60,
         extra_env: Optional[dict] = None) -> tuple[int, str, str]:
    env = {**os.environ, **(extra_env or {})}
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=cwd, timeout=timeout, env=env,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"Timed out: {' '.join(cmd)}"
    except Exception as e:
        return -1, "", str(e)


def _cmd_exists(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


# ── Issue factory ─────────────────────────────────────────────────────────────

def _issue(file: str, line: int, severity: str, description: str,
           language: str = "python", extra: Optional[dict] = None) -> dict:
    return {
        "file": file,
        "line": line,
        "issue_type": "security",
        "severity": severity,
        "description": description,
        "language": language,
        "extra": extra or {},
        "fix_strategy": "Review and remediate the security vulnerability described above.",
    }


# ── Bandit (Python SAST) ──────────────────────────────────────────────────────

def scan_bandit(repo_path: str, files: list[Path]) -> list[dict]:
    """Run Bandit on Python files. Returns list of issue dicts."""
    if not files:
        return []
    if not _cmd_exists("bandit"):
        print("[Security] bandit not found — skipping Python SAST.")
        return []

    py_paths = [str(f) for f in files if f.suffix == ".py"]
    if not py_paths:
        return []

    print(f"[Security] Running Bandit on {len(py_paths)} Python file(s)...")
    code, stdout, stderr = _run(
        ["bandit", "-r", "-f", "json", "-q"] + py_paths,
        cwd=repo_path, timeout=60,
    )

    if not stdout.strip():
        return []

    issues = []
    try:
        data = json.loads(stdout)
        sev_map = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
        for r in data.get("results", []):
            filepath = r.get("filename", "")
            try:
                rel = str(Path(filepath).relative_to(repo_path))
            except ValueError:
                rel = filepath
            issues.append(_issue(
                file=rel,
                line=r.get("line_number", 0),
                severity=sev_map.get(r.get("issue_severity", "LOW"), "low"),
                description=f"[{r.get('test_id')}] {r.get('issue_text', '')}",
                language="python",
                extra={
                    "test_id": r.get("test_id"),
                    "confidence": r.get("issue_confidence"),
                    "cwe": r.get("issue_cwe", {}).get("id"),
                },
            ))
    except Exception as e:
        print(f"[Security] Bandit parse error: {e}")

    print(f"[Security] Bandit: {len(issues)} issue(s).")
    return issues


# ── Semgrep (multi-language SAST) ─────────────────────────────────────────────

def scan_semgrep(repo_path: str, files: list[Path]) -> list[dict]:
    """Run Semgrep with auto config on all supported files."""
    if not files:
        return []
    if not _cmd_exists("semgrep"):
        print("[Security] semgrep not found — skipping multi-language SAST.")
        return []

    # Semgrep works best on directories; pass the repo root
    print(f"[Security] Running Semgrep (auto rules) on {repo_path}...")
    code, stdout, stderr = _run(
        ["semgrep", "--config=auto", "--json", "--quiet",
         "--no-git-ignore",
         "--exclude=node_modules", "--exclude=venv", "--exclude=.git",
         "."],
        cwd=repo_path, timeout=120,
        extra_env={"SEMGREP_SEND_METRICS": "off"},
    )

    if not stdout.strip():
        return []

    # Determine language from file extension
    _ext_lang = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascript", ".tsx": "typescript", ".java": "java",
        ".go": "go", ".rs": "rust", ".php": "php",
        ".cpp": "cpp", ".c": "c",
    }

    issues = []
    try:
        data = json.loads(stdout)
        sev_map = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}

        for r in data.get("results", []):
            filepath = r.get("path", "")
            try:
                rel = str(Path(filepath).relative_to(repo_path))
            except ValueError:
                rel = filepath
            lang = _ext_lang.get(Path(rel).suffix.lower(), "unknown")
            sev_raw = r.get("extra", {}).get("severity", "WARNING").upper()
            issues.append(_issue(
                file=rel,
                line=r.get("start", {}).get("line", 0),
                severity=sev_map.get(sev_raw, "low"),
                description=f"[{r.get('check_id', 'semgrep')}] {r.get('extra', {}).get('message', '')}",
                language=lang,
                extra={
                    "rule_id": r.get("check_id"),
                    "col": r.get("start", {}).get("col", 0),
                },
            ))
    except Exception as e:
        print(f"[Security] Semgrep parse error: {e}")

    print(f"[Security] Semgrep: {len(issues)} issue(s).")
    return issues


# ── npm audit (JS/TS dependency vulnerabilities) ──────────────────────────────

def scan_npm_audit(repo_path: str) -> list[dict]:
    """Run npm audit on repos with package.json."""
    pkg = Path(repo_path) / "package.json"
    if not pkg.exists():
        return []
    if not _cmd_exists("npm"):
        print("[Security] npm not found — skipping npm audit.")
        return []

    print("[Security] Running npm audit...")
    code, stdout, stderr = _run(
        ["npm", "audit", "--json"], cwd=repo_path, timeout=60,
    )

    if not stdout.strip():
        return []

    issues = []
    try:
        data = json.loads(stdout)
        sev_map = {"critical": "high", "high": "high", "moderate": "medium", "low": "low", "info": "low"}

        # npm v7+ format: {"vulnerabilities": {...}}
        vulns = data.get("vulnerabilities", {})
        if not vulns:
            # npm v6 format: {"advisories": {...}}
            vulns_raw = data.get("advisories", {})
            for adv in vulns_raw.values():
                sev = sev_map.get(adv.get("severity", "low"), "low")
                issues.append(_issue(
                    file="package.json",
                    line=0,
                    severity=sev,
                    description=f"[npm-audit] {adv.get('module_name')}: {adv.get('title', '')} — {adv.get('url', '')}",
                    language="javascript",
                    extra={"package": adv.get("module_name"), "cve": adv.get("cves", [])},
                ))
        else:
            for pkg_name, vuln in vulns.items():
                sev = sev_map.get(vuln.get("severity", "low"), "low")
                via = vuln.get("via", [])
                detail = via[0].get("title", "") if via and isinstance(via[0], dict) else str(via[0] if via else "")
                fix_available = vuln.get("fixAvailable")
                safe_ver = None
                if fix_available and isinstance(fix_available, dict):
                    safe_ver = fix_available.get("version")
                if not safe_ver:
                    safe_ver = _npm_latest_version(pkg_name)
                fix_strategy = (
                    f"Upgrade {pkg_name} to {safe_ver} in package.json to fix this vulnerability."
                    if safe_ver else
                    f"Upgrade {pkg_name} to the latest safe version in package.json."
                )
                issues.append(_issue(
                    file="package.json",
                    line=0,
                    severity=sev,
                    description=f"[npm-audit] {pkg_name}: {detail}",
                    language="javascript",
                    extra={"package": pkg_name, "safe_version": safe_ver,
                           "fixAvailable": fix_available},
                ))
                issues[-1]["fix_strategy"] = fix_strategy
    except Exception as e:
        print(f"[Security] npm audit parse error: {e}")

    print(f"[Security] npm audit: {len(issues)} vulnerability(ies).")
    return issues


# ── safety (Python dependency vulnerabilities) ────────────────────────────────

def scan_safety(repo_path: str) -> list[dict]:
    """Run safety check on requirements.txt / installed packages."""
    if not _cmd_exists("safety"):
        print("[Security] safety not found — skipping Python dep scan.")
        return []

    repo = Path(repo_path)
    req_files = list(repo.glob("requirements*.txt")) + list(repo.glob("**/requirements*.txt"))
    req_files = [f for f in req_files if "venv" not in str(f)][:3]

    issues = []
    targets = [["--full-report", "--json"]]

    for req_file in req_files:
        code, stdout, stderr = _run(
            ["safety", "check", "-r", str(req_file), "--json"],
            cwd=repo_path, timeout=60,
        )
        # safety prepends deprecation banner lines before JSON — skip to first {
        raw = stdout.strip()
        json_start = raw.find("{")
        if json_start < 0:
            json_start = raw.find("[")
        if json_start < 0:
            continue
        raw = raw[json_start:]
        try:
            # Use a JSONDecoder to stop at the first complete object
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(raw)
            # New safety format: {"vulnerabilities": [...], ...}
            vulns = []
            if isinstance(data, dict):
                vulns = data.get("vulnerabilities", [])
                for vuln in vulns:
                    pkg_name = vuln.get("package_name", "")
                    installed = vuln.get("analyzed_version", "")
                    desc = vuln.get("advisory", "") or vuln.get("vulnerability_id", "")
                    vuln_id = vuln.get("vulnerability_id", "")
                    sev = vuln.get("severity", {})
                    if isinstance(sev, dict):
                        sev_val = sev.get("cvssv3", {}).get("base_severity", "medium").lower()
                    else:
                        sev_val = "medium"
                    sev_map = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}
                    safe_ver = _latest_pypi_version(pkg_name)
                    iss = _issue(
                        file=str(req_file.relative_to(repo_path)),
                        line=0,
                        severity=sev_map.get(sev_val, "medium"),
                        description=f"[safety-{vuln_id}] {pkg_name}=={installed} has known vulnerability: {str(desc)[:120]}",
                        language="python",
                        extra={"package": pkg_name, "installed": installed,
                               "safe_version": safe_ver},
                    )
                    iss["fix_strategy"] = (
                        f"Upgrade {pkg_name} to {safe_ver} in requirements.txt."
                        if safe_ver else
                        f"Upgrade {pkg_name} to the latest safe version in requirements.txt."
                    )
                    issues.append(iss)
            elif isinstance(data, list):
                # Legacy safety format: [[pkg, affected, installed, desc, id], ...]
                for vuln in data:
                    if isinstance(vuln, list) and len(vuln) >= 4:
                        pkg_name, affected, installed, desc = vuln[0], vuln[1], vuln[2], vuln[3]
                        vuln_id = vuln[4] if len(vuln) > 4 else ""
                        safe_ver = _latest_pypi_version(pkg_name)
                        iss = _issue(
                            file=str(req_file.relative_to(repo_path)),
                            line=0,
                            severity="high",
                            description=f"[safety-{vuln_id}] {pkg_name}=={installed} has known vulnerability: {str(desc)[:120]}",
                            language="python",
                            extra={"package": pkg_name, "installed": installed,
                                   "affected": affected, "safe_version": safe_ver},
                        )
                        iss["fix_strategy"] = (
                            f"Upgrade {pkg_name} to {safe_ver} in requirements.txt."
                            if safe_ver else
                            f"Upgrade {pkg_name} to the latest safe version in requirements.txt."
                        )
                        issues.append(iss)
        except Exception as e:
            print(f"[Security] safety parse error: {e}")

    if issues:
        print(f"[Security] safety: {len(issues)} vulnerability(ies).")
    return issues


# ── Main SecurityScanner class ────────────────────────────────────────────────

class SecurityScanner:
    def __init__(self, repo_path: str):
        self.repo_path = str(Path(repo_path).resolve())

    def scan(self, files: list[Path]) -> list[dict]:
        """
        Run all applicable security scanners on the given files.
        Returns deduplicated list of issue dicts.
        """
        all_issues: list[dict] = []

        # Bandit — Python SAST
        all_issues += scan_bandit(self.repo_path, files)

        # Semgrep — multi-language SAST (only if there are non-Python files or semgrep available)
        all_issues += scan_semgrep(self.repo_path, files)

        # npm audit — JS/TS dep vulns
        all_issues += scan_npm_audit(self.repo_path)

        # safety — Python dep vulns
        all_issues += scan_safety(self.repo_path)

        # Deduplicate by (file, line, description[:60])
        seen = set()
        unique = []
        for iss in all_issues:
            key = (iss["file"], iss["line"], iss["description"][:60])
            if key not in seen:
                seen.add(key)
                unique.append(iss)

        print(f"[Security] Total: {len(unique)} security issue(s) found.")
        return unique


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    scanner = SecurityScanner(repo)

    from pathlib import Path as P
    all_files = [
        f for ext in [".py", ".js", ".ts", ".java", ".go", ".rs", ".php", ".cpp", ".c"]
        for f in P(repo).rglob(f"*{ext}")
        if "venv" not in str(f) and "node_modules" not in str(f) and ".git" not in str(f)
    ]
    issues = scanner.scan(all_files)

    print(f"\n── Security Issues ───────────────────────────────────────")
    for i, iss in enumerate(issues, 1):
        print(f"\n[{i}] {iss['severity'].upper()} | {iss['file']}:{iss['line']}")
        print(f"     {iss['description'][:100]}")
    print(f"\nTotal: {len(issues)}")
