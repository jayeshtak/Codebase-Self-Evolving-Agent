"""
core/agent_config.py

Loads per-project .agentconfig.yaml from the repository root.
All fields are optional — missing keys fall back to system defaults from config.py.
"""
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


@dataclass
class AgentConfig:
    exclude_paths: list[str] = field(default_factory=lambda: [
        "venv/", "node_modules/", ".git/", "__pycache__/", "dist/", "build/",
    ])
    enabled_checks: dict[str, bool] = field(default_factory=lambda: {
        "complexity": True,
        "dead_code": True,
        "security": True,
        "type_hints": True,
        "sql_injection": True,
        "duplicate": True,
        "deep_nesting": True,
        "long_function": True,
    })
    severity_threshold: str = "low"      # low | medium | high
    max_issues_per_file: int = 20
    confidence_threshold: float = 0.85
    max_auto_fix: int = 5

    def is_path_excluded(self, filepath: str) -> bool:
        for exc in self.exclude_paths:
            if exc.rstrip("/") in filepath:
                return True
        return False

    def is_check_enabled(self, check_name: str) -> bool:
        return self.enabled_checks.get(check_name, True)

    def passes_severity_threshold(self, severity: str) -> bool:
        order = {"low": 0, "medium": 1, "high": 2}
        return order.get(severity, 0) >= order.get(self.severity_threshold, 0)


_SEVERITY_LEVELS = {"low", "medium", "high"}


def load_agent_config(repo_path: str) -> AgentConfig:
    """
    Load .agentconfig.yaml from repo_path if it exists.
    Returns default AgentConfig if file is missing or YAML is unavailable.
    """
    cfg = AgentConfig()
    config_file = Path(repo_path) / ".agentconfig.yaml"

    if not config_file.exists():
        return cfg

    if not _YAML_AVAILABLE:
        print("[AgentConfig] PyYAML not installed — ignoring .agentconfig.yaml")
        return cfg

    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}

        if "exclude_paths" in data and isinstance(data["exclude_paths"], list):
            cfg.exclude_paths = [str(p) for p in data["exclude_paths"]]

        if "enabled_checks" in data and isinstance(data["enabled_checks"], dict):
            for k, v in data["enabled_checks"].items():
                cfg.enabled_checks[str(k)] = bool(v)

        if "severity_threshold" in data:
            val = str(data["severity_threshold"]).lower()
            if val in _SEVERITY_LEVELS:
                cfg.severity_threshold = val

        if "max_issues_per_file" in data:
            cfg.max_issues_per_file = max(1, int(data["max_issues_per_file"]))

        if "confidence_threshold" in data:
            cfg.confidence_threshold = max(0.0, min(1.0, float(data["confidence_threshold"])))

        if "max_auto_fix" in data:
            cfg.max_auto_fix = max(1, int(data["max_auto_fix"]))

        print(f"[AgentConfig] Loaded .agentconfig.yaml from {repo_path}")

    except Exception as e:
        print(f"[AgentConfig] Failed to parse .agentconfig.yaml: {e} — using defaults")

    return cfg
