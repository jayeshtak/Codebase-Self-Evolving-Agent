import json
import re
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
from groq import Groq

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TestWriterResult:
    __test__ = False
    success: bool
    source_file: str
    test_file: str
    test_code: str
    functions_covered: list[str]
    error: str = ""

    def to_dict(self):
        return {
            "success": self.success,
            "source_file": self.source_file,
            "test_file": self.test_file,
            "test_code": self.test_code,
            "functions_covered": self.functions_covered,
            "error": self.error,
        }


# ── TestWriterAgent ───────────────────────────────────────────────────────────

class TestWriterAgent:
    __test__ = False
    def __init__(self):
        self.client = genai.Client(api_key=config.GEMINI_API_KEY) if getattr(config, "has_gemini", lambda: bool(config.GEMINI_API_KEY))() else None
        self.groq = Groq(api_key=config.GROQ_API_KEY) if getattr(config, "has_groq", lambda: bool(config.GROQ_API_KEY))() else None

    def _call_llm(self, prompt: str) -> str:
        """Call Gemini Flash. On repeated 429s or absence fall back to Groq."""
        if self.client:
            try:
                response = self.client.models.generate_content(
                    model=config.GEMINI_FLASH,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=3000,
                    )
                )
                return response.text.strip()
            except Exception as e:
                err = str(e)
                if "429" not in err and "RESOURCE_EXHAUSTED" not in err and "API_KEY" not in err:
                    raise
                print(f"[TestWriter] Gemini rate limited ({e}). Falling back to Groq...")

        if self.groq:
            try:
                response = self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=3000,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                print(f"[TestWriter] Groq call failed: {e}")
                raise

        raise RuntimeError("No LLM provider available for test writing (set GEMINI_API_KEY or GROQ_API_KEY).")

    _LANG_META = {
        ".py":   {"lang": "python",     "framework": "pytest",   "fence": "python",     "ext": ".py",   "prefix": "test_"},
        ".js":   {"lang": "javascript", "framework": "jest",     "fence": "javascript", "ext": ".test.js",  "prefix": ""},
        ".mjs":  {"lang": "javascript", "framework": "jest",     "fence": "javascript", "ext": ".test.mjs", "prefix": ""},
        ".ts":   {"lang": "typescript", "framework": "jest",     "fence": "typescript", "ext": ".test.ts",  "prefix": ""},
        ".tsx":  {"lang": "typescript", "framework": "jest",     "fence": "typescript", "ext": ".test.tsx", "prefix": ""},
        ".jsx":  {"lang": "javascript", "framework": "jest",     "fence": "javascript", "ext": ".test.jsx", "prefix": ""},
        ".java": {"lang": "java",       "framework": "JUnit 5",  "fence": "java",       "ext": "Test.java", "prefix": ""},
        ".cpp":  {"lang": "cpp",        "framework": "GoogleTest","fence": "cpp",       "ext": "_test.cpp", "prefix": ""},
        ".cxx":  {"lang": "cpp",        "framework": "GoogleTest","fence": "cpp",       "ext": "_test.cxx", "prefix": ""},
        ".cc":   {"lang": "cpp",        "framework": "GoogleTest","fence": "cpp",       "ext": "_test.cc",  "prefix": ""},
        ".php":  {"lang": "php",        "framework": "PHPUnit",  "fence": "php",        "ext": "Test.php",  "prefix": ""},
        ".go":   {"lang": "go",         "framework": "go test",  "fence": "go",         "ext": "_test.go",  "prefix": ""},
        ".rs":   {"lang": "rust",       "framework": "cargo test","fence": "rust",      "ext": "_test.rs",  "prefix": ""},
    }

    def _get_lang_meta(self, source_file: str) -> dict:
        suffix = Path(source_file).suffix.lower()
        return self._LANG_META.get(suffix, self._LANG_META[".py"])

    def _get_test_file_path(self, source_file: str, repo_path: str) -> str:
        """Derive test file path from source file path."""
        source = Path(source_file)
        meta = self._get_lang_meta(source_file)
        prefix = meta["prefix"]
        ext = meta["ext"]

        if meta["lang"] == "python":
            test_name = f"{prefix}{source.stem}{ext}"
            return str(Path(repo_path) / "tests" / test_name)
        elif meta["lang"] in ("javascript", "typescript"):
            # Place test next to source: src/utils.js -> src/utils.test.js
            test_name = f"{source.stem}{ext}"
            return str(source.parent / test_name) if source.parent != Path(".") \
                else str(Path(repo_path) / "tests" / test_name)
        elif meta["lang"] == "java":
            # MyClass.java -> MyClassTest.java
            test_name = f"{source.stem}{ext}"
            return str(Path(repo_path) / "tests" / test_name)
        elif meta["lang"] == "go":
            # utils.go -> utils_test.go (same package dir)
            test_name = f"{source.stem}{ext}"
            return str(source.parent / test_name)
        else:
            test_name = f"{prefix}{source.stem}{ext}"
            return str(Path(repo_path) / "tests" / test_name)

    def _read_file(self, repo_path: str, filepath: str) -> str:
        path = Path(repo_path) / filepath
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
        return ""

    def _build_prompt(
        self,
        source_file: str,
        file_content: str,
        function_names: list[str],
        existing_tests: str = "",
    ) -> str:
        meta = self._get_lang_meta(source_file)
        lang_label = meta["lang"].capitalize()
        framework = meta["framework"]
        fence = meta["fence"]
        functions_text = ", ".join(function_names) if function_names else "all functions"
        existing_text = ""
        if existing_tests:
            existing_text = f"\nExisting tests (do not duplicate):\n```{fence}\n{existing_tests[:1000]}\n```\n"

        # Language-specific rules
        lang_rules = {
            "python": (
                "1. Use pytest style (no unittest classes unless needed)\n"
                "2. Use descriptive test function names: test_<function>_<scenario>\n"
                "3. Mock external dependencies with pytest-mock or unittest.mock\n"
                "4. Import the module correctly based on the file path"
            ),
            "javascript": (
                "1. Use Jest (describe/it/expect)\n"
                "2. Use descriptive test names inside describe('<function>', () => { ... })\n"
                "3. Mock external dependencies with jest.mock() or jest.fn()\n"
                "4. Use require() or import based on the file's module style"
            ),
            "typescript": (
                "1. Use Jest with TypeScript (describe/it/expect)\n"
                "2. Use descriptive test names inside describe('<function>', () => { ... })\n"
                "3. Mock with jest.mock() and use proper TypeScript types\n"
                "4. Import using ES module syntax"
            ),
            "java": (
                "1. Use JUnit 5 (@Test, @BeforeEach, assertEquals, assertThrows)\n"
                "2. Place tests in the same package as the class under test\n"
                "3. Mock dependencies with Mockito (@Mock, @InjectMocks)\n"
                "4. Class name must be <SourceClass>Test"
            ),
            "cpp": (
                "1. Use GoogleTest (TEST(), EXPECT_EQ, ASSERT_THROW)\n"
                "2. Include <gtest/gtest.h> and the source header\n"
                "3. Group tests by function in TEST(SuiteName, TestName) blocks\n"
                "4. Add int main() with RUN_ALL_TESTS()"
            ),
            "php": (
                "1. Use PHPUnit (extends TestCase, public function test<Name>)\n"
                "2. Use $this->assertEquals, $this->assertThrows\n"
                "3. Mock dependencies with $this->createMock()\n"
                "4. Class name must be <SourceClass>Test"
            ),
            "go": (
                "1. Use the standard go test package (func TestXxx(t *testing.T))\n"
                "2. Use t.Run() for sub-tests, t.Errorf() for failures\n"
                "3. Use table-driven tests where applicable\n"
                "4. Keep the same package name (or _test suffix)"
            ),
            "rust": (
                "1. Use Rust's built-in test framework (#[cfg(test)], #[test])\n"
                "2. Put tests in a mod tests block at the bottom of the file\n"
                "3. Use assert_eq!, assert!, panic! appropriately\n"
                "4. Use use super::* to import the module under test"
            ),
        }.get(meta["lang"], "1. Write idiomatic tests for the language and framework.")

        return f"""You are an expert {lang_label} engineer writing {framework} tests.

Source file: {source_file}
Functions to test: {functions_text}

Source code:
```{fence}
{file_content[:3000]}
```
{existing_text}

Write comprehensive {framework} tests for the specified functions.

Rules:
{lang_rules}
- Test normal cases, edge cases, and error cases
- Keep tests focused and independent
- Do NOT test private methods unless critical

Respond with JSON only:
{{
  "test_code": "complete {framework} test file content as a single string",
  "functions_covered": ["list", "of", "function", "names", "tested"]
}}

JSON only. No markdown. No extra text."""

    def write_tests(
        self,
        repo_path: str,
        source_file: str,
        function_names: Optional[list[str]] = None,
        write_to_disk: bool = True,
    ) -> TestWriterResult:
        """
        Generate pytest tests for a source file.
        Writes to tests/test_<filename>.py by default.
        """
        function_names = function_names or []
        file_content = self._read_file(repo_path, source_file)

        if not file_content:
            return TestWriterResult(
                success=False,
                source_file=source_file,
                test_file="",
                test_code="",
                functions_covered=[],
                error=f"Source file not found: {source_file}",
            )

        test_file_path = self._get_test_file_path(source_file, repo_path)

        # Read existing tests if any
        existing_tests = ""
        if Path(test_file_path).exists():
            existing_tests = Path(test_file_path).read_text(encoding="utf-8", errors="ignore")

        print(f"[TestWriter] Writing tests for {source_file} -> {Path(test_file_path).name}")

        prompt = self._build_prompt(
            source_file=source_file,
            file_content=file_content,
            function_names=function_names,
            existing_tests=existing_tests,
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
            test_code = data.get("test_code", "").strip()
            functions_covered = data.get("functions_covered", [])

            if not test_code:
                return TestWriterResult(
                    success=False,
                    source_file=source_file,
                    test_file=test_file_path,
                    test_code="",
                    functions_covered=[],
                    error="LLM returned empty test_code",
                )

            # Write to disk
            if write_to_disk:
                Path(test_file_path).parent.mkdir(parents=True, exist_ok=True)
                Path(test_file_path).write_text(test_code, encoding="utf-8")
                print(f"[TestWriter] Written to {test_file_path}")

            print(f"[TestWriter] Covered: {functions_covered}")

            return TestWriterResult(
                success=True,
                source_file=source_file,
                test_file=test_file_path,
                test_code=test_code,
                functions_covered=functions_covered,
            )

        except json.JSONDecodeError as e:
            _any_fence = r"```(?:\w+)?\s*\n(.*?)```"
            match = re.search(_any_fence, raw, re.DOTALL)
            if match:
                test_code = match.group(1).strip()
                if write_to_disk:
                    Path(test_file_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(test_file_path).write_text(test_code, encoding="utf-8")
                return TestWriterResult(
                    success=True,
                    source_file=source_file,
                    test_file=test_file_path,
                    test_code=test_code,
                    functions_covered=[],
                )
            return TestWriterResult(
                success=False,
                source_file=source_file,
                test_file=test_file_path,
                test_code="",
                functions_covered=[],
                error=f"JSON parse failed: {e}",
            )

        except Exception as e:
            print(f"[TestWriter] Error: {e}")
            return TestWriterResult(
                success=False,
                source_file=source_file,
                test_file=test_file_path,
                test_code="",
                functions_covered=[],
                error=str(e),
            )


# -- CLI test (requires API key) -----------------------------------------------

if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."

    writer = TestWriterAgent()
    result = writer.write_tests(
        repo_path=repo,
        source_file="core/graph.py",
        function_names=["build", "get_impact_radius"],
        write_to_disk=False,
    )

    print("\n-- TestWriter Result -------------------------------------")
    print(f"Success   : {result.success}")
    print(f"Test file : {result.test_file}")
    print(f"Covered   : {result.functions_covered}")
    if result.error:
        print(f"Error     : {result.error}")
    if result.test_code:
        print(f"\nGenerated tests:\n{result.test_code[:500]}...")
