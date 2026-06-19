#!/usr/bin/env bash
# setup.sh — Codebase Self-Evolving Agent onboarding script
# Run once to install all dependencies and configure the environment.
set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${CYAN}[setup]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[error]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# ── 1. Python ─────────────────────────────────────────────────────────────────
info "Checking Python 3.10+..."
if ! command -v python3 &>/dev/null; then
    fail "Python 3 not found. Install it from https://python.org"
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Python 3.10+ required, found $PY_VER"
fi
ok "Python $PY_VER"

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    info "Creating virtual environment..."
    python3 -m venv venv
    ok "Virtual environment created at ./venv"
else
    ok "Virtual environment already exists"
fi

# Activate venv for the rest of the script
# shellcheck disable=SC1091
source venv/bin/activate

# ── 3. Pip dependencies ───────────────────────────────────────────────────────
info "Installing Python dependencies (this may take a minute)..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "Python dependencies installed"

# ── 4. Optional: Node.js & npm ───────────────────────────────────────────────
info "Checking Node.js / npm (needed for JS/TS scanning and Jest)..."
if command -v node &>/dev/null && command -v npm &>/dev/null; then
    NODE_VER=$(node --version)
    NPM_VER=$(npm --version)
    ok "Node.js $NODE_VER, npm $NPM_VER"
else
    warn "Node.js not found. JS/TS scanning and Jest tests will be skipped."
    warn "Install from: https://nodejs.org"
fi

# ── 5. Optional: Java / Maven (JVM projects) ─────────────────────────────────
info "Checking Java / Maven (needed for Java scanning and JUnit tests)..."
if command -v java &>/dev/null; then
    JAVA_VER=$(java -version 2>&1 | head -1)
    ok "Java: $JAVA_VER"
else
    warn "Java not found. Java/Spring Boot support will be skipped."
fi
if command -v mvn &>/dev/null; then
    ok "Maven: $(mvn --version | head -1)"
else
    warn "Maven not found. JUnit tests will be skipped."
fi

# ── 6. Optional: Go ──────────────────────────────────────────────────────────
info "Checking Go (needed for Go scanning and go test)..."
if command -v go &>/dev/null; then
    ok "Go: $(go version)"
else
    warn "Go not found. Go support will be skipped."
fi

# ── 7. Optional: Rust / Cargo ─────────────────────────────────────────────────
info "Checking Rust / Cargo (needed for Rust scanning and cargo test)..."
if command -v cargo &>/dev/null; then
    ok "Cargo: $(cargo --version)"
else
    warn "Rust/Cargo not found. Rust support will be skipped."
fi

# ── 8. Optional: Playwright ──────────────────────────────────────────────────
info "Checking Playwright (needed for browser/E2E testing)..."
if python3 -c "import playwright" &>/dev/null 2>&1; then
    info "Installing Playwright browsers..."
    python3 -m playwright install --with-deps chromium 2>/dev/null || \
        warn "Playwright browser install failed — run 'playwright install' manually."
    ok "Playwright browsers ready"
else
    warn "Playwright not installed. Browser testing will be skipped."
fi

# ── 9. Optional: bandit / semgrep (security scanning) ────────────────────────
info "Checking security tools..."
if command -v bandit &>/dev/null; then
    ok "bandit: $(bandit --version 2>&1 | head -1)"
else
    warn "bandit not found (should be installed via requirements.txt)"
fi
if command -v semgrep &>/dev/null; then
    ok "semgrep: $(semgrep --version 2>&1 | head -1)"
else
    warn "semgrep not found (should be installed via requirements.txt)"
fi

# ── 10. .env file ─────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    info "Creating .env template..."
    cat > .env << 'EOF'
# Codebase Self-Evolving Agent — environment variables
# Get your Gemini key from: https://aistudio.google.com/app/apikey
GEMINI_API_KEY=your_gemini_api_key_here

# Get your Groq key from: https://console.groq.com/keys
GROQ_API_KEY=your_groq_api_key_here
EOF
    warn ".env created — open it and fill in your API keys before running the app."
else
    ok ".env already exists"
fi

# ── 11. Data directory ────────────────────────────────────────────────────────
mkdir -p data
ok "data/ directory ready"

# ── 12. Run tests ─────────────────────────────────────────────────────────────
info "Running test suite to verify setup..."
if python3 -m pytest tests/ -q --tb=short 2>&1; then
    ok "All tests passed"
else
    warn "Some tests failed — check output above. The app may still work."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your GEMINI_API_KEY and GROQ_API_KEY"
echo "  2. Activate the venv:  source venv/bin/activate"
echo "  3. Launch the UI:      streamlit run ui/app.py"
echo ""
