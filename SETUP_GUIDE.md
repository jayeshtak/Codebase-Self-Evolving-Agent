# 🧬 Codebase Agent Complete Setup & Execution Guide (Mac & Windows)

This guide provides the complete, end-to-end steps to set up, install dependencies, configure, and run the **Codebase Self-Evolving Agent** project on both macOS and Windows.

---

## 📋 Table of Contents
1. [Prerequisites](#1-prerequisites)
2. [Step 1: Install Python 3.12](#step-1-install-python-312)
3. [Step 2: Create a Virtual Environment](#step-2-create-a-virtual-environment)
4. [Step 3: Install Required Dependencies](#step-3-install-required-dependencies)
5. [Step 4: Initialize Git Repository](#step-4-initialize-git-repository)
6. [Step 5: Run the Streamlit Dashboard](#step-5-run-the-streamlit-dashboard)
7. [Troubleshooting & Bug Fix Summary](#troubleshooting--bug-fix-summary)

---

## 1. Prerequisites

### 🍏 macOS
Ensure you have [Homebrew](https://brew.sh/) installed.

### 🪟 Windows
Ensure you have [Git for Windows](https://gitforwindows.org/) installed and added to your path.

---

## Step 1: Install Python 3.12

The project packages are not yet fully compatible with Python 3.14 (the latest standard). Install Python 3.12 to ensure stability:

### 🍏 macOS
```bash
brew install python@3.12
```

### 🪟 Windows
1. Download Python 3.12 installer from [Python.org](https://www.python.org/downloads/release/python-3120/).
2. Run the installer and check the box **"Add python.exe to PATH"** before installing.

---

## Step 2: Create a Virtual Environment

Navigate to the project root directory (`CodeBaseAgent-master`) and create a clean Python 3.12 virtual environment:

### 🍏 macOS
```bash
# Delete old venv (if it exists)
rm -rf venv

# Create new venv using Python 3.12
/opt/homebrew/bin/python3.12 -m venv venv
```

### 🪟 Windows (PowerShell or Command Prompt)
```cmd
:: Delete old venv (if it exists)
rmdir /s /q venv

:: Create new venv using Python 3.12
py -3.12 -m venv venv
```

---

## Step 3: Install Required Dependencies

Activate the virtual environment and install the required packages.

### 🍏 macOS
```bash
# Activate virtual environment
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install project requirements
pip install -r requirements.txt

# Install torchvision (needed for transformers/image models)
pip install torchvision

# Install Tkinter (System Library for macOS)
brew install python-tk@3.12
```

### 🪟 Windows (PowerShell)
```powershell
# Activate virtual environment
.\venv\Scripts\Activate.ps1

# Upgrade pip
pip install --upgrade pip

# Install project requirements
pip install -r requirements.txt

# Install torchvision
pip install torchvision
```

### 🪟 Windows (Command Prompt)
```cmd
:: Activate virtual environment
call venv\Scripts\activate.bat

:: Upgrade pip
pip install --upgrade pip

:: Install project requirements
pip install -r requirements.txt

:: Install torchvision
pip install torchvision
```

---

## Step 4: Initialize Git Repository

The agent relies on Git to track changes, create branches, and auto-rollback code. The folder must be initialized as a Git repository:

### 🍏 macOS / 🪟 Windows
```bash
# Initialize git
git init

# Configure standard user details
git config user.name "Developer"
git config user.email "dev@example.com"

# Stage and commit the codebase
git add .
git commit -m "Initial commit"
```

---

## Step 5: Launch Developer Command Center (Recommended)

Launch the production-hardened Minimal Black Developer Command Center (FastAPI + SPA):

### 🍏 macOS / Linux
```bash
source venv/bin/activate
uvicorn ui.server:app --host 0.0.0.0 --port 8000
```

### 🪟 Windows (PowerShell)
```powershell
.\venv\Scripts\Activate.ps1
uvicorn ui.server:app --host 0.0.0.0 --port 8000
```

Open **[http://localhost:8000](http://localhost:8000)** in your browser to access the full-stack developer command center (real-time telemetry, AST diff viewer, interactive dependency graph, and auto-fix loop).

*(Alternative Legacy UI: `streamlit run ui/app.py` on port 8501).*

---

## Step 6: 1-Click Live Cloud Deployment (Render / Railway)

The repository is fully configured for zero-configuration cloud deployment:

- **GitHub Repository**: [https://github.com/jayeshtak/Codebase-Self-Evolving-Agent](https://github.com/jayeshtak/Codebase-Self-Evolving-Agent)
- **1-Click Render Deploy**: Click the Deploy to Render button in the README or navigate to Render -> New -> Blueprint -> connect `Codebase-Self-Evolving-Agent`.
- **Docker PaaS**: Uses [`Dockerfile`](Dockerfile) with automated port binding and `DEMO_MODE=true` sandbox security.


---

## Troubleshooting & Bug Fix Summary

We fixed several issues to get the setup working:

1. **Python 3.14 Compatibility**: Replaced with **Python 3.12** since packages like `tree-sitter-languages` and `semgrep` don't support 3.14 yet.
2. **`ModuleNotFoundError: No module named '_tkinter'`**: Installed `python-tk@3.12` via Homebrew on Mac (Tkinter comes preinstalled with Python on Windows).
3. **`ModuleNotFoundError: No module named 'torchvision'`**: Installed `torchvision` within the virtual environment.
4. **`Not a git repository`**: Ran `git init` and made a commit since the agent requires Git features to function.
5. **Slow embedding model download (Stuck on startup)**: Updated [memory/chroma.py](file:///Users/thespiderdev/Downloads/CodeBaseAgent-master/memory/chroma.py#L11) configuration to use `all-MiniLM-L6-v2` (~90MB) instead of `all-mpnet-base-v2` (~438MB), accelerating the startup process.
