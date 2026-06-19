import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.error_parser import RuntimeIssue


# ── Result model ──────────────────────────────────────────────────────────────

@dataclass
class BrowserScanResult:
    routes_tested: list[str] = field(default_factory=list)
    issues: list[RuntimeIssue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)  # paths to error screenshots


# ── Browser tester ────────────────────────────────────────────────────────────

class BrowserTesterAgent:
    """
    Uses Playwright (headless Chromium) to open each route in a real browser,
    capture JS console errors, uncaught exceptions, and network failures.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        self._playwright = None
        self._browser = None

    # ── Playwright lifecycle ──────────────────────────────────────────────────

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            return True
        except Exception as e:
            print(f"[BrowserTester] Failed to start Playwright: {e}")
            print("[BrowserTester] Run: playwright install chromium")
            return False

    def _stop(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ── JS error → RuntimeIssue ───────────────────────────────────────────────

    def _parse_js_error(self, message: str, url: str, page_url: str) -> Optional[RuntimeIssue]:
        """
        Convert a browser console error / page crash into a RuntimeIssue.
        Tries to extract file and line from the stack trace embedded in the message.
        """
        # Try to find file:line in the message (e.g. "app.js:42")
        match = re.search(r'([^\s(]+\.(?:js|ts|jsx|tsx)):(\d+)', message)
        if match:
            raw_file = match.group(1)
            line = int(match.group(2))
            # Strip URL origin to get relative path
            rel = re.sub(r'^https?://[^/]+/', '', raw_file)
            # Try to match against repo files
            target_file = rel
            for f in self.repo_path.rglob(Path(rel).name):
                if "node_modules" not in str(f):
                    try:
                        target_file = str(f.relative_to(self.repo_path))
                        break
                    except ValueError:
                        pass
        else:
            target_file = url or page_url
            line = 0

        error_class = "JSError"
        em = re.match(r'^([A-Za-z][A-Za-z0-9_]*Error)', message)
        if em:
            error_class = em.group(1)

        desc = message[:150].replace("\n", " ")
        return RuntimeIssue(
            file=target_file,
            line=line,
            issue_type="runtime_error",
            severity="high",
            error_class=error_class,
            description=f"{error_class}: {desc}",
            traceback=message[:800],
            language="javascript",
        )

    # ── Single route scan ─────────────────────────────────────────────────────

    def _test_route(
        self, page, route: dict, base_url: str, screenshot_dir: Path
    ) -> list[RuntimeIssue]:
        """Open one route, collect console errors, page exceptions, and network failures."""
        issues = []
        path = route["path"]
        url = base_url.rstrip("/") + path

        console_errors: list[str] = []
        page_errors: list[str] = []
        network_failures: list[str] = []

        def on_console(msg):
            if msg.type in ("error", "warning"):
                console_errors.append(msg.text)

        def on_page_error(err):
            page_errors.append(str(err))

        def on_request_failed(request):
            failure = request.failure or "unknown"
            # Skip the page's own URL failing — already caught via HTTP status
            if request.url != url:
                network_failures.append(
                    f"Network request failed: {request.method} {request.url} — {failure}"
                )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)

        try:
            resp = page.goto(url, timeout=8000, wait_until="networkidle")
            status = resp.status if resp else 0
            print(f"[BrowserTester] {route['method']} {path} → {status} "
                  f"| console:{len(console_errors)} net_fail:{len(network_failures)}")

            # Screenshot on 4xx/5xx
            if status >= 400:
                ss_path = screenshot_dir / f"error_{path.replace('/', '_')}.png"
                try:
                    page.screenshot(path=str(ss_path), full_page=True)
                except Exception:
                    pass

            # Console errors
            for msg in console_errors:
                issue = self._parse_js_error(msg, url="", page_url=path)
                if issue:
                    issues.append(issue)

            # Uncaught JS exceptions
            for err in page_errors:
                issue = self._parse_js_error(err, url="", page_url=path)
                if issue:
                    issues.append(issue)

            # Network failures (failed fetch/XHR/resource loads)
            for fail_msg in network_failures:
                issues.append(RuntimeIssue(
                    file=path,
                    line=0,
                    issue_type="runtime_error",
                    severity="high",
                    error_class="NetworkError",
                    description=fail_msg[:150],
                    traceback=fail_msg,
                    language="javascript",
                ))

        except Exception as e:
            print(f"[BrowserTester] Error on {url}: {e}")
        finally:
            page.remove_listener("console", on_console)
            page.remove_listener("pageerror", on_page_error)
            page.remove_listener("requestfailed", on_request_failed)

        return issues

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self, routes: list[dict], base_url: str) -> BrowserScanResult:
        """
        Open each route in a headless browser.
        Returns BrowserScanResult with JS issues found.
        """
        result = BrowserScanResult()

        if not routes:
            return result

        screenshot_dir = self.repo_path / "data" / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        if not self._start():
            result.errors.append(
                "Playwright not available. Run: pip install playwright && playwright install chromium"
            )
            return result

        try:
            context = self._browser.new_context(
                ignore_https_errors=True,
                java_script_enabled=True,
            )
            page = context.new_page()

            for route in routes:
                path = route["path"]
                # Skip parameterised routes
                if "{" in path or "<" in path or path.count(":") > 1:
                    print(f"[BrowserTester] Skipping parameterised route: {path}")
                    continue
                # Only GET routes make sense in a browser
                if route.get("method", "GET") != "GET":
                    continue

                route_issues = self._test_route(page, route, base_url, screenshot_dir)
                result.routes_tested.append(f"BROWSER {path}")

                # Deduplicate by (file, line, error_class)
                existing = {(i.file, i.line, i.error_class) for i in result.issues}
                for iss in route_issues:
                    key = (iss.file, iss.line, iss.error_class)
                    if key not in existing:
                        existing.add(key)
                        result.issues.append(iss)

            page.close()
            context.close()

        except Exception as e:
            print(f"[BrowserTester] Scan error: {e}")
            result.errors.append(str(e))
        finally:
            self._stop()

        print(f"[BrowserTester] Done. {len(result.issues)} JS issue(s) found.")
        return result


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:5000"

    tester = BrowserTesterAgent(repo)
    routes = [{"method": "GET", "path": "/"}]
    result = tester.scan(routes, base)

    print(f"\n── Browser Scan Results ──────────────────────────────")
    print(f"Routes tested: {result.routes_tested}")
    print(f"Issues found : {len(result.issues)}")
    for i in result.issues:
        print(f"\n  [{i.error_class}] {i.file}:{i.line}")
        print(f"  {i.description}")
