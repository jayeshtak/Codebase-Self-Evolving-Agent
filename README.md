# 🧬 Codebase Self-Evolving Agent

**An Autonomous AI System for Continuous Codebase Improvement**

> MCAP21 — Mini Project 1B, Semester 2  
> MET Institute of Computer Science · Academic Year 2025–2026

---

## Overview

Most developer tools are **reactive** — they wait for you to run them. This agent is **proactive**. It continuously scans your codebase, detects issues, generates fixes using LLMs, reviews them with an independent AI, runs your tests, and asks for your approval before changing anything.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      AGENT PIPELINE                                      │
│                                                                          │
│  ┌─────────┐   ┌─────────┐   ┌───────┐   ┌──────────┐   ┌──────────┐   │
│  │ Scanner │──▶│ Planner │──▶│ Coder │◀─▶│ Reviewer │──▶│ Patcher  │   │
│  │         │   │         │   │       │   │          │   │          │   │
│  │tree-sitter  │ChromaDB │   │Gemini │   │Groq Llama│   │ libcst   │   │
│  │radon    │   │SQLite   │   │Flash  │   │3.3 70B   │   │git branch│   │
│  │vulture  │   │networkx │   │2.5    │   │(loop ×3) │   │          │   │
│  │bandit   │   │         │   │       │   │          │   │          │   │
│  │semgrep  │   └─────────┘   └───────┘   └──────────┘   └────┬─────┘   │
│  └─────────┘                                                  │         │
│                                                               ▼         │
│                                          ┌────────────┐  ┌──────────┐  │
│                                          │ TestWriter │─▶│ Executor │  │
│                                          │  (pytest)  │  │self-heal │  │
│                                          └────────────┘  └────┬─────┘  │
│                                                               │         │
│                                                               ▼         │
│                                                        ┌────────────┐   │
│                                                        │  User UI   │   │
│                                                        │ (Streamlit)│   │
│                                                        └────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Features

### What It Detects (9 languages)

| Check | Languages | Severity |
|-------|-----------|----------|
| Cyclomatic complexity | Python | Medium / High |
| Dead code / unused functions | Python | Low |
| Deep nesting (>4 levels) | All 9 | Medium |
| Long functions (>60 lines) | All 9 | Low |
| Duplicate code blocks | Python | Low |
| Weak security hashes (MD5) | Python | High |
| Hardcoded secrets / API keys | All | High |
| SQL injection patterns | Python, JS, TS, PHP | High |
| Missing type hints | Python | Low |
| Unused imports | Python | Medium |
| Syntax errors | All 9 | High |
| Bandit / Semgrep findings | Python | High |
| ESLint warnings | JS / TS | Low–High |

### Fix Pipeline

1. **Coder Agent** — Gemini 2.5 Flash with git diff context, few-shot examples, RAG-retrieved similar code
2. **Reviewer Agent** — Groq Llama 3.3 70B as independent second opinion — iterates with Coder up to 3 times
3. **AST patching** — libcst for Python (function-level), full-file fallback for all languages
4. **Rename propagation** — detects renamed functions/classes, updates all references across repo
5. **Cross-file fixes** — updates dependent files when a public interface changes

### Safety

- Every fix runs on an isolated **git branch** (`agent-fix-<id>`)
- **Self-healing test loop**: if new tests fail, Gemini re-attempts up to 3 times
- **Automatic rollback** if tests still fail after healing
- **User confirmation** required at every step — nothing merges without approval
- **Confidence threshold**: only auto-apply fixes above configurable threshold (default 0.85)
- **Rejection memory**: rejected issues are never suggested again

### Intelligence

- **ChromaDB RAG**: semantic code search, context injection into every LLM prompt
- **Dependency graph**: networkx maps imports/calls, assigns impact radius per issue
- **Natural language search**: find issues by typing plain English ("show security issues in auth files")
- **Code health score**: 0–100 score + A–F grade per scan, with trend chart
- **Evaluation metrics**: predicted confidence vs actual outcome, calibration analysis
- **APScheduler**: set daily or interval scans to run automatically

---

## Quick Start

### One-command setup

```bash
git clone https://github.com/mdiuser/CodeBaseAgent.git
cd CodeBaseAgent
bash setup.sh
```

### Manual setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env with your API keys
```

**API keys** (both free tier):
- Gemini: [aistudio.google.com](https://aistudio.google.com)
- Groq: [console.groq.com](https://console.groq.com)

### Launch UI

```bash
streamlit run ui/app.py
```

Open [http://localhost:8501](http://localhost:8501) → enter any git repo path → Connect → Run Scan.

### CLI

```bash
python main.py /path/to/repo --scan          # incremental scan
python main.py /path/to/repo --scan --full   # full scan all files
python main.py /path/to/repo --scan --plan   # scan + generate fix plan
python main.py /path/to/repo --stats         # show statistics
```

---

## Per-Project Configuration

Create `.agentconfig.yaml` in any repository root to customise the agent's behaviour for that project:

```yaml
exclude_paths:
  - venv/
  - node_modules/
  - migrations/
  - dist/

enabled_checks:
  complexity: true
  dead_code: true
  security: true
  type_hints: true
  sql_injection: true
  duplicate: true

severity_threshold: medium     # skip issues below this level (low/medium/high)
max_issues_per_file: 5         # cap issues reported per file
confidence_threshold: 0.85     # auto-apply threshold (0.0–1.0)
max_auto_fix: 5                # max issues per auto-fix run
```

---

## Benchmark Results

Evaluated on 10 synthetic benchmark cases (isolated git repos, no prior codebase context):

| Case | Issue Type | Result | Confidence | Time |
|------|-----------|--------|-----------|------|
| Unused import removal | dead_code | ✅ | 0.90 | 4.2s |
| Dead code removal | dead_code | ✅ | 0.90 | 4.8s |
| Deep nesting refactor | deep_nesting | ✅ | 0.90 | 7.2s |
| Long function split | long_function | ✅ | 0.90 | 54.8s |
| MD5 → SHA256 | security | ✅ | 0.90 | 5.2s |
| Hardcoded secret flag | security | ✅ | 0.90 | ~5s |
| SQL injection fix | security | ✅ | 0.90 | ~6s |
| Missing type hints | missing_type_hints | ✅ | 0.90 | ~8s |
| Duplicate code | duplicate | ✅ | 0.90 | ~12s |
| High complexity refactor | complexity | ✅ | 0.90 | ~30s |

**Accuracy: 10/10 (100%) | Avg Confidence: 0.90 | Avg Review Iterations: 1.4**

```bash
python benchmarks/runner.py
```

---

## Fix Safety Flow

```
Issue detected
      │
      ▼
git checkout -b agent-fix-<id>      ← isolated branch
      │
      ▼
Coder generates fix  ──▶  Reviewer critiques  ──▶  (up to 3 iterations)
      │
      ▼
libcst patches file (function-level, Python) or full-file replace
      │
      ▼
Existing tests run ── FAIL? ──▶ rollback immediately
      │ PASS
      ▼
TestWriter writes new pytest tests
      │
      ▼
New tests run ── FAIL? ──▶ self-heal (Gemini re-fixes, 3 attempts)
      │ PASS         │ still FAIL? ──▶ rollback
      ▼
"Ready for Approval" shown in UI
      │
      ▼  user clicks Apply
git merge agent-fix-<id> → master
```

---

## Known Limitations

See [LIMITATIONS.txt](LIMITATIONS.txt) for the full breakdown. Summary:

**Can fix reliably:** dead code, unused imports, weak hashes, deep nesting, long functions, duplicate code, hardcoded secrets, SQL injection patterns, missing docstrings, missing type hints

**Cannot fix:** logic bugs without a specification, business logic known only to the developer, race conditions, memory leaks, flaky tests, build system bugs

---

## Project Structure

```
codebase-agent/
├── agents/
│   ├── scanner.py        # Static analysis (radon, vulture, tree-sitter, bandit)
│   ├── planner.py        # Prioritisation + fix strategy (Gemini Flash)
│   ├── coder.py          # Fix generation with RAG + few-shot examples
│   ├── reviewer.py       # Independent review loop (Groq Llama 3.3)
│   ├── test_writer.py    # Auto pytest generation
│   ├── doc_writer.py     # Auto docstring generation
│   └── runtime_tester.py # Runtime error detection
├── core/
│   ├── patcher.py        # AST-level patching via libcst
│   ├── graph.py          # Dependency graph + impact radius (networkx)
│   ├── git_manager.py    # Branch, snapshot, rollback, merge
│   ├── executor.py       # pytest runner with self-healing loop
│   ├── rename_propagator.py  # Cross-repo rename detection + propagation
│   ├── security_scanner.py   # Bandit, Semgrep, npm audit, safety
│   ├── duplicate_detector.py # Clone detection
│   ├── reporter.py       # HTML report generation
│   └── terminal.py       # Whitelisted subprocess execution
├── memory/
│   ├── sqlite.py         # Issues, fixes, rejections, health scores, outcomes
│   └── chroma.py         # Vector RAG (sentence-transformers + ChromaDB)
├── scheduler/
│   └── jobs.py           # APScheduler (daily + interval scans)
├── ui/
│   └── app.py            # Streamlit dashboard (1200+ lines)
├── benchmarks/
│   ├── runner.py         # Benchmark harness
│   └── cases/            # 10 synthetic test cases
├── tests/                # pytest test suite
├── main.py               # Central CodebaseAgent orchestrator
├── config.py             # API keys and thresholds
├── setup.sh              # One-command setup script
├── .agentconfig.yaml     # Per-repo configuration (optional)
├── .env.example          # API key template
└── LIMITATIONS.txt       # Honest capability breakdown
```

---

## Tech Stack

| Category | Technology |
|----------|-----------|
| LLMs | Gemini 2.5 Flash, Groq Llama 3.3 70B (all **free tier**) |
| AST parsing | tree-sitter (9 languages), radon, vulture, libcst |
| Security scanning | Bandit, Semgrep, Safety, npm audit |
| Embeddings | sentence-transformers all-mpnet-base-v2 (local CPU) |
| Vector DB | ChromaDB (local, no cloud) |
| Dependency graph | networkx |
| Relational DB | SQLite |
| Git integration | GitPython |
| Scheduler | APScheduler |
| UI | Streamlit |
| Graph visualisation | pyvis + plotly |
| Testing | pytest, Playwright |
| Runtime | Python 3.10+, venv |

---

*Built for MCAP21 Mini Project 1B — MET Institute of Computer Science, 2025–2026*
