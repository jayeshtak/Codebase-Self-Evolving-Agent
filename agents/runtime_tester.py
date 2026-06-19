import ast
import json
import os
import re
import sys
import time
import signal
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.error_parser import (
    RuntimeIssue, parse_python_traceback, parse_http_error, parse_node_error
)


# ── Result models ─────────────────────────────────────────────────────────────

@dataclass
class RuntimeScanResult:
    repo_path: str
    issues: list[RuntimeIssue] = field(default_factory=list)
    routes_tested: list[str] = field(default_factory=list)
    server_started: bool = False
    run_command: str = ""
    framework: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "total_runtime_issues": len(self.issues),
            "issues": [i.to_dict() for i in self.issues],
            "routes_tested": self.routes_tested,
            "server_started": self.server_started,
            "run_command": self.run_command,
            "framework": self.framework,
            "errors": self.errors,
        }


# ── RuntimeTesterAgent ────────────────────────────────────────────────────────

class RuntimeTesterAgent:
    TEST_PORT = 19871  # unlikely to conflict

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()

    # ── Framework / entry point detection ────────────────────────────────────

    def detect_framework(self) -> tuple[str, str]:
        """
        Returns (framework, run_command).
        Supports: flask, fastapi, django, node, spring, laravel, symfony, script, unknown
        """
        repo = self.repo_path

        # ── Node / Express ───────────────────────────────────────────────────
        pkg = repo / "package.json"
        if pkg.exists():
            data = json.loads(pkg.read_text())
            start = data.get("scripts", {}).get("start", "")
            dev = data.get("scripts", {}).get("dev", "")
            if not start and not dev:
                for name in ["index.js", "server.js", "app.js", "src/index.js"]:
                    if (repo / name).exists():
                        start = f"node {name}"
                        break
            cmd = start or dev or "node index.js"
            if cmd in (data.get("scripts", {}).get("start", ""),
                       data.get("scripts", {}).get("dev", "")):
                script_name = "start" if start else "dev"
                cmd = f"npm run {script_name}"
            return "node", cmd

        # ── Java Spring Boot (Maven or Gradle) ───────────────────────────────
        pom = repo / "pom.xml"
        gradle = repo / "build.gradle"
        gradle_kts = repo / "build.gradle.kts"
        if pom.exists() or gradle.exists() or gradle_kts.exists():
            # Verify it's Spring Boot by checking for the annotation or parent POM
            is_spring = False
            try:
                if pom.exists():
                    pom_text = pom.read_text(encoding="utf-8", errors="ignore")
                    is_spring = "spring-boot" in pom_text
                if not is_spring and (gradle.exists() or gradle_kts.exists()):
                    gf = gradle if gradle.exists() else gradle_kts
                    g_text = gf.read_text(encoding="utf-8", errors="ignore")
                    is_spring = "spring-boot" in g_text
                # Also scan Java source for @SpringBootApplication
                if not is_spring:
                    for jf in list(repo.rglob("*.java"))[:20]:
                        if "node_modules" in str(jf):
                            continue
                        if "@SpringBootApplication" in jf.read_text(encoding="utf-8", errors="ignore"):
                            is_spring = True
                            break
            except Exception:
                pass
            if is_spring:
                if pom.exists():
                    return "spring", f"mvn spring-boot:run -Dspring-boot.run.arguments=--server.port={self.TEST_PORT} -q"
                else:
                    wrapper = "./gradlew" if (repo / "gradlew").exists() else "gradle"
                    return "spring", f"{wrapper} bootRun --args='--server.port={self.TEST_PORT}'"

        # ── PHP Laravel ──────────────────────────────────────────────────────
        artisan = repo / "artisan"
        if artisan.exists():
            return "laravel", f"php artisan serve --port={self.TEST_PORT} --host=127.0.0.1"

        # ── PHP Symfony ──────────────────────────────────────────────────────
        composer = repo / "composer.json"
        if composer.exists():
            try:
                comp_data = json.loads(composer.read_text())
                deps = {**comp_data.get("require", {}), **comp_data.get("require-dev", {})}
                if any("symfony" in k for k in deps):
                    public = repo / "public"
                    docroot = "public" if public.exists() else "."
                    return "symfony", f"php -S 127.0.0.1:{self.TEST_PORT} -t {docroot}"
            except Exception:
                pass

        # ── Python frameworks ────────────────────────────────────────────────
        py_files = [
            f for f in repo.rglob("*.py")
            if "venv" not in str(f) and ".git" not in str(f)
        ]

        flask_files, fastapi_files, django_files = [], [], []
        for f in py_files:
            try:
                src = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "from flask import" in src or "import flask" in src.lower():
                flask_files.append(f)
            if "from fastapi import" in src or "import fastapi" in src.lower():
                fastapi_files.append(f)
            if "django" in src.lower() and "settings" in src.lower():
                django_files.append(f)

        if fastapi_files:
            entry = self._find_fastapi_entry(fastapi_files)
            return "fastapi", f"uvicorn {entry}:app --port {self.TEST_PORT} --host 127.0.0.1"

        if flask_files:
            entry = self._find_flask_entry(flask_files)
            return "flask", f"python {entry}"

        if django_files or (repo / "manage.py").exists():
            return "django", f"python manage.py runserver 127.0.0.1:{self.TEST_PORT} --noreload"

        # Plain Python script
        for name in ["main.py", "app.py", "run.py", "server.py", "index.py"]:
            if (repo / name).exists():
                return "script", f"python {name}"

        return "unknown", ""

    def _find_flask_entry(self, flask_files: list[Path]) -> str:
        """Find the file that calls app.run() — that's the entry point."""
        for f in flask_files:
            src = f.read_text(encoding="utf-8", errors="ignore")
            if "app.run(" in src or 'if __name__' in src:
                rel = f.relative_to(self.repo_path)
                return str(rel)
        rel = flask_files[0].relative_to(self.repo_path)
        return str(rel)

    def _find_fastapi_entry(self, fastapi_files: list[Path]) -> str:
        """Find the FastAPI app module path (dotted)."""
        for f in fastapi_files:
            src = f.read_text(encoding="utf-8", errors="ignore")
            if re.search(r'\bapp\s*=\s*FastAPI\(', src):
                rel = f.relative_to(self.repo_path)
                # Convert path to module: src/main.py -> src.main
                module = str(rel).replace(os.sep, ".").removesuffix(".py")
                return module
        rel = fastapi_files[0].relative_to(self.repo_path)
        module = str(rel).replace(os.sep, ".").removesuffix(".py")
        return module

    # ── Route discovery ───────────────────────────────────────────────────────

    def discover_routes(self, framework: str) -> list[dict]:
        """
        Returns list of {"method": "GET", "path": "/route"}.
        Parses source files for route decorators / definitions.
        """
        routes = []

        if framework == "node":
            js_files = [
                f for f in list(self.repo_path.rglob("*.js")) +
                           list(self.repo_path.rglob("*.ts"))
                if "node_modules" not in str(f) and ".git" not in str(f)
            ]
            for jf in js_files:
                try:
                    jsrc = jf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for jm in re.finditer(
                    r'(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*["\`\']([^"\'`]+)',
                    jsrc
                ):
                    routes.append({
                        "method": jm.group(1).upper(),
                        "path": jm.group(2),
                    })

        elif framework == "spring":
            # Scan Java files for Spring MVC annotations
            java_files = [
                f for f in self.repo_path.rglob("*.java")
                if "node_modules" not in str(f) and ".git" not in str(f)
            ]
            for jf in java_files:
                try:
                    src = jf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                # @GetMapping("/path"), @PostMapping, @RequestMapping(method=...)
                for m in re.finditer(
                    r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
                    src
                ):
                    verb = m.group(1).upper()
                    path = m.group(2)
                    method = "GET" if verb == "REQUEST" else verb
                    routes.append({"method": method, "path": path})

        elif framework in ("laravel", "symfony"):
            # Scan PHP route files
            route_dirs = [
                self.repo_path / "routes",       # Laravel
                self.repo_path / "config" / "routes",  # Symfony
                self.repo_path,
            ]
            php_files = []
            for d in route_dirs:
                if d.exists() and d.is_dir():
                    php_files.extend(d.glob("*.php"))
            for pf in php_files:
                try:
                    src = pf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                # Route::get('/path', ...) or Route::post(...)
                for m in re.finditer(
                    r'Route::(get|post|put|delete|patch|any)\s*\(\s*["\']([^"\']+)["\']',
                    src
                ):
                    verb = m.group(1).upper()
                    path = m.group(2)
                    method = "GET" if verb == "ANY" else verb
                    routes.append({"method": method, "path": path})
                # Symfony #[Route('/path', methods: ['GET'])]
                for m in re.finditer(
                    r'#\[Route\(["\']([^"\']+)["\'](?:[^]]*methods:\s*\[([^\]]+)\])?',
                    src
                ):
                    path = m.group(1)
                    method = "GET"
                    if m.group(2):
                        methods_str = m.group(2).replace("'", "").replace('"', "").strip()
                        method = methods_str.split(",")[0].strip().upper() or "GET"
                    routes.append({"method": method, "path": path})

        else:
            # Python: Flask / FastAPI / Django
            py_files = [
                f for f in self.repo_path.rglob("*.py")
                if "venv" not in str(f) and ".git" not in str(f)
            ]
            for fpath in py_files:
                try:
                    src = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if framework in ("flask", "fastapi"):
                    for m in re.finditer(
                        r'@\w+\.(route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
                        src
                    ):
                        verb = m.group(1).upper()
                        path = m.group(2)
                        method = "GET" if verb == "ROUTE" else verb
                        routes.append({"method": method, "path": path})

        # Always include root
        if not any(r["path"] == "/" for r in routes):
            routes.insert(0, {"method": "GET", "path": "/"})

        # Deduplicate
        seen = set()
        unique = []
        for r in routes:
            key = (r["method"], r["path"])
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return unique

    # ── Script runner (no server) ─────────────────────────────────────────────

    def run_script(self, script_path: str, timeout: int = 30) -> list[RuntimeIssue]:
        """Run a plain Python script and collect any errors."""
        print(f"[RuntimeTester] Running script: {script_path}")
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.repo_path),
            )
            if result.returncode != 0 and result.stderr:
                issues = parse_python_traceback(result.stderr, str(self.repo_path))
                print(f"[RuntimeTester] Script errors: {len(issues)}")
                return issues
            print("[RuntimeTester] Script ran cleanly.")
            return []
        except subprocess.TimeoutExpired:
            print("[RuntimeTester] Script timed out (may be a server — use web mode).")
            return []
        except Exception as e:
            print(f"[RuntimeTester] Script run error: {e}")
            return []

    # ── Web server runner ─────────────────────────────────────────────────────

    def _wait_for_server(self, host: str, port: int, timeout: int = 15) -> bool:
        """Poll until the server is accepting connections."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                requests.get(f"http://{host}:{port}/", timeout=1)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def _inject_flask_port(self, run_cmd: str) -> str:
        """Make Flask listen on our test port via env var."""
        return run_cmd  # handled via env FLASK_RUN_PORT

    def _ensure_node_modules(self):
        """Run npm install if node_modules is missing."""
        nm = self.repo_path / "node_modules"
        pkg = self.repo_path / "package.json"
        if pkg.exists() and not nm.exists():
            print("[RuntimeTester] node_modules not found — running npm install...")
            try:
                subprocess.run(
                    ["npm", "install"],
                    cwd=str(self.repo_path),
                    timeout=120,
                    capture_output=True,
                )
                print("[RuntimeTester] npm install done.")
            except Exception as e:
                print(f"[RuntimeTester] npm install failed: {e}")

    def start_server(
        self, run_command: str, framework: str
    ) -> Optional[subprocess.Popen]:
        """Start the web server as a background process."""
        env = os.environ.copy()
        env["PORT"] = str(self.TEST_PORT)
        env["FLASK_RUN_PORT"] = str(self.TEST_PORT)
        env["FLASK_RUN_HOST"] = "127.0.0.1"
        env["NODE_ENV"] = "development"

        # Inject port into command for frameworks that need it
        if framework == "flask" and "app.run(" not in run_command:
            cmd = f"flask run --port {self.TEST_PORT} --host 127.0.0.1"
        elif framework == "node":
            self._ensure_node_modules()
            cmd = run_command
        elif framework == "laravel":
            cmd = f"php artisan serve --port={self.TEST_PORT} --host=127.0.0.1"
        elif framework == "spring":
            # Maven/Gradle port already injected in detect_framework
            cmd = run_command
        else:
            cmd = run_command

        print(f"[RuntimeTester] Starting server: {cmd}")
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(self.repo_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            return proc
        except Exception as e:
            print(f"[RuntimeTester] Failed to start server: {e}")
            return None

    def stop_server(self, proc: subprocess.Popen):
        """Kill the server process group."""
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ── HTTP route tester ─────────────────────────────────────────────────────

    def test_routes(
        self, routes: list[dict], base_url: str, server_stderr_so_far: str
    ) -> tuple[list[RuntimeIssue], list[str]]:
        """
        Hit each route and collect issues from non-2xx responses.
        Returns (issues, routes_tested).
        """
        issues = []
        tested = []

        for route in routes:
            method = route["method"]
            path = route["path"]

            # Skip routes with path parameters — can't guess values
            if "{" in path or "<" in path or path.count(":") > 1:
                print(f"[RuntimeTester] Skipping parameterised route: {path}")
                continue

            url = base_url.rstrip("/") + path
            tested.append(f"{method} {path}")

            try:
                resp = getattr(requests, method.lower())(url, timeout=5)
                code = resp.status_code
                print(f"[RuntimeTester] {method} {path} → {code}")

                if code >= 500:
                    route_issues = parse_http_error(
                        route=path,
                        status_code=code,
                        response_body=resp.text,
                        server_stderr=server_stderr_so_far,
                        repo_path=str(self.repo_path),
                    )
                    issues.extend(route_issues)

            except requests.ConnectionError:
                print(f"[RuntimeTester] Connection refused on {url} — server may have crashed")
                break
            except Exception as e:
                print(f"[RuntimeTester] Error hitting {url}: {e}")

        return issues, tested

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self, run_command: str = "", timeout: int = 20) -> RuntimeScanResult:
        """
        Full runtime scan:
        1. Detect framework + run command (or use provided)
        2. If script → run directly, collect errors
        3. If web app → start server, discover + hit routes, collect errors
        """
        result = RuntimeScanResult(repo_path=str(self.repo_path))

        framework, detected_cmd = self.detect_framework()
        cmd = run_command or detected_cmd
        result.framework = framework
        result.run_command = cmd

        print(f"[RuntimeTester] Framework: {framework} | Command: {cmd}")

        if not cmd:
            result.errors.append("Could not detect how to run this project.")
            return result

        # ── Plain script ──
        if framework == "script":
            script = cmd.replace("python ", "").strip()
            full_path = str(self.repo_path / script)
            result.issues = self.run_script(full_path, timeout=timeout)
            return result

        # ── Web app ──
        routes = self.discover_routes(framework)
        print(f"[RuntimeTester] Discovered {len(routes)} route(s): "
              f"{[r['path'] for r in routes]}")

        proc = self.start_server(cmd, framework)
        if not proc:
            result.errors.append("Failed to start server process.")
            return result

        base_url = f"http://127.0.0.1:{self.TEST_PORT}"
        server_ready = self._wait_for_server("127.0.0.1", self.TEST_PORT, timeout=timeout)

        if not server_ready:
            # Collect startup errors from stderr
            try:
                stderr_out = proc.stderr.read() if proc.stderr else ""
            except Exception:
                stderr_out = ""
            self.stop_server(proc)

            if stderr_out:
                issues = parse_python_traceback(stderr_out, str(self.repo_path))
                if not issues:
                    issues = parse_node_error(stderr_out, str(self.repo_path))
                result.issues = issues
                result.errors.append("Server failed to start.")
            else:
                result.errors.append("Server did not respond in time.")
            return result

        result.server_started = True
        print(f"[RuntimeTester] Server ready at {base_url}")

        # Read any stderr accumulated during startup
        stderr_buf = ""

        # Test all routes via HTTP
        route_issues, tested = self.test_routes(routes, base_url, stderr_buf)
        result.routes_tested = tested
        result.issues.extend(route_issues)

        # Browser testing (Playwright) — catches frontend JS errors
        try:
            from agents.browser_tester import BrowserTesterAgent
            print(f"[RuntimeTester] Running browser tests via Playwright...")
            browser_tester = BrowserTesterAgent(str(self.repo_path))
            browser_result = browser_tester.scan(routes, base_url)
            result.routes_tested.extend(browser_result.routes_tested)
            result.errors.extend(browser_result.errors)
            # Deduplicate by (file, line, error_class)
            existing = {(i.file, i.line, i.error_class) for i in result.issues}
            for iss in browser_result.issues:
                key = (iss.file, iss.line, iss.error_class)
                if key not in existing:
                    existing.add(key)
                    result.issues.append(iss)
            if browser_result.issues:
                print(f"[RuntimeTester] Browser: {len(browser_result.issues)} JS issue(s) found.")
        except ImportError:
            pass  # playwright not installed — skip silently
        except Exception as e:
            print(f"[RuntimeTester] Browser test error: {e}")
            result.errors.append(f"Browser test error: {e}")

        # Read final stderr for any remaining errors
        try:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=3)
            if stderr:
                startup_issues = parse_python_traceback(stderr, str(self.repo_path))
                if not startup_issues:
                    startup_issues = parse_node_error(stderr, str(self.repo_path))
                # Deduplicate by (file, line)
                existing_fl = {(i.file, i.line) for i in result.issues}
                for iss in startup_issues:
                    if (iss.file, iss.line) not in existing_fl:
                        result.issues.append(iss)
        except Exception:
            pass
        finally:
            self.stop_server(proc)

        print(f"[RuntimeTester] Done. {len(result.issues)} runtime issue(s) found.")
        return result


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    tester = RuntimeTesterAgent(repo)
    result = tester.scan()

    print(f"\n── Runtime Scan Results ──────────────────────────────")
    print(f"Framework   : {result.framework}")
    print(f"Command     : {result.run_command}")
    print(f"Server up   : {result.server_started}")
    print(f"Routes hit  : {result.routes_tested}")
    print(f"Issues found: {len(result.issues)}")
    for i in result.issues:
        print(f"\n  [{i.error_class}] {i.file}:{i.line}")
        print(f"  {i.description}")
