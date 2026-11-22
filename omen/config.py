"""Layered configuration: config.yaml defaults -> config.local.yaml -> .env.

Secrets are never stored in config.yaml or logged. ``Config.redacted()`` is the
only representation that should be printed or sent to a model.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SECRET_HINTS = ("api_key", "secret", "token", "password", "credential")


class ConfigError(RuntimeError):
    """Raised for invalid or missing configuration. Fail loudly."""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def _read_dotenv(path: Path) -> dict[str, str]:
    """Small .env parser: KEY=VALUE, # comments, optional surrounding quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"{path}:{line_no}: empty key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if any(h in key.lower() for h in SECRET_HINTS) else _mask(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


@dataclass
class Config:
    """Validated, layered configuration with six required sections."""

    sections: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None
    local_source: Path | None = None
    env_source: Path | None = None

    def __getitem__(self, key: str) -> Any:
        return self.sections[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup, e.g. cost.monthly_budget_usd."""
        node: Any = self.sections
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def redacted(self) -> dict[str, Any]:
        return _mask(self.sections)

    def __repr__(self) -> str:  # pragma: no cover - defensive against secret leaks
        return f"Config(source={self.source!r}, sections={self.redacted()!r})"


def load_config(root: Path | str | None = None) -> Config:
    """Load defaults, apply git-ignored local overrides, then inject .env secrets."""
    base = Path(root).resolve() if root is not None else _project_root()
    config_path = Path(os.environ.get("OMEN_CONFIG", base / "config.yaml")).resolve()
    local_path = base / "config.local.yaml"
    env_path = base / ".env"

    defaults = _read_yaml(config_path)
    if not defaults:
        raise ConfigError(f"No configuration found at {config_path}")
    overrides = _read_yaml(local_path)
    merged = _deep_merge(defaults, overrides)

    env = _read_dotenv(env_path)
    for adapter_name, adapter in (merged.get("providers", {}).get("adapters") or {}).items():
        if not isinstance(adapter, dict):
            continue
        env_name = adapter.get("api_key_env")
        if env_name and env_name in env:
            adapter["api_key"] = env[env_name]
        # Keep the name of the env var, never the value, in the config display.
        if env_name and env_path.exists() and env_name not in env:
            raise ConfigError(
                f"Provider {adapter_name!r} references {env_name!r} but it is unset in .env"
            )
        if "api_key" in adapter and env_name not in env:
            raise ConfigError(
                f"Provider {adapter_name!r} has an inline api_key; secrets must live in .env"
            )

    required = ("system", "providers", "tools", "safety", "resources", "cost")
    missing = [name for name in required if not isinstance(merged.get(name), dict)]
    if missing:
        raise ConfigError(f"Missing config sections: {', '.join(missing)}")
    # Phase 6 dashboard section is additive and optional for older configs.
    if "dashboard" not in merged or not isinstance(merged.get("dashboard"), dict):
        merged["dashboard"] = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8765,
            "auth_enabled": True,
            "token_env": "OMEN_DASHBOARD_TOKEN",
            "allow_remote": False,
            "remote_read_only": True,
            "tls_cert": None,
            "tls_key": None,
            "push_notifications": False,
        }

    cfg = Config(
        sections=merged,
        source=config_path,
        local_source=local_path if local_path.exists() else None,
        env_source=env_path if env_path.exists() else None,
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Fail loudly on unsafe or inconsistent values."""
    providers = cfg["providers"]
    adapters = providers.get("adapters") or {}
    if not adapters:
        raise ConfigError("At least one provider adapter must be configured")
    default = providers.get("default")
    if default not in adapters:
        raise ConfigError(f"Default provider {default!r} is not configured")
    for name in providers.get("fallback") or []:
        if name not in adapters:
            raise ConfigError(f"Fallback provider {name!r} is not configured")

    safety = cfg["safety"]
    ttl = safety.get("approval_ttl_seconds", 0)
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        raise ConfigError("safety.approval_ttl_seconds must be positive")

    resources = cfg["resources"]
    warn = resources["warn_percent"]
    degrade = resources["degrade_percent"]
    emergency = resources["emergency_percent"]
    if not 0 < warn < degrade < emergency <= 100:
        raise ConfigError("Resource thresholds must satisfy warn < degrade < emergency <= 100")
    if resources["ram_target_gb"] > resources["ram_hard_ceiling_gb"]:
        raise ConfigError("RAM target cannot exceed hard ceiling")
    for item in resources.get("never_degrade") or []:
        if item in (resources.get("degradation_order") or []):
            raise ConfigError(f"Protected component {item!r} cannot be degraded")

    cost = cfg["cost"]
    if cost["monthly_budget_usd"] < 0 or cost["per_request_token_cap"] <= 0:
        raise ConfigError("Cost budget must be >= 0 and token cap must be positive")
    if not 0 < cost["warn_at_percent"] <= 100:
        raise ConfigError("cost.warn_at_percent must be in (0, 100]")
