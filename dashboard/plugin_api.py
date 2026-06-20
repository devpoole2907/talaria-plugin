"""Talaria admin plugin — iOS app control surface.

Mounted at /api/plugins/talaria/ by the Hermes dashboard plugin system.

Endpoints:
  GET  /status    — agent health + current config summary
  GET  /model     — read current model
  POST /model     — switch model (persisted to config.yaml)
  POST /session/reset — reset/clear active session
  GET  /tools     — list toolsets and their enabled state
  POST /tools     — enable/disable a toolset
  GET  /skills    — list installed skills
  GET  /memory    — read memory usage/info
  POST /memory    — clear memory
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

try:
    from fastapi import APIRouter, HTTPException, Query, status as http_status
except Exception:
    class APIRouter:  # type: ignore
        def get(self, *_args, **_kwargs): return lambda fn: fn
        def post(self, *_args, **_kwargs): return lambda fn: fn
    class HTTPException(Exception): pass  # type: ignore

log = logging.getLogger(__name__)
router = APIRouter()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"config load failed: {e}")


def _save_config(cfg: dict) -> None:
    try:
        from hermes_cli.config import save_config
        save_config(cfg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"config save failed: {e}")


def _get_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        import os
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


# ──────────────────────────────────────────────
# GET /status
# ──────────────────────────────────────────────

@router.get("/status")
def get_status():
    """Quick health + current model/config summary."""
    cfg = _load_config()
    model_cfg = cfg.get("model", {})
    return {
        "ok": True,
        "model": {
            "default": model_cfg.get("default", "unknown"),
            "provider": model_cfg.get("provider", "auto"),
        },
        "terminal": {
            "backend": cfg.get("terminal", {}).get("backend", "local"),
        },
        "agent": {
            "max_turns": cfg.get("agent", {}).get("max_turns", 90),
        },
        "memory": {
            "enabled": cfg.get("memory", {}).get("memory_enabled", True),
        },
    }


# ──────────────────────────────────────────────
# GET /model  — read current model
# ──────────────────────────────────────────────

@router.get("/model")
def get_model():
    cfg = _load_config()
    model_cfg = cfg.get("model", {})
    return {
        "default": model_cfg.get("default", "unknown"),
        "provider": model_cfg.get("provider", "auto"),
        "base_url": model_cfg.get("base_url", None),
        "context_length": model_cfg.get("context_length", 0),
    }


# ──────────────────────────────────────────────
# POST /model  — switch model
# ──────────────────────────────────────────────

from pydantic import BaseModel, Field  # type: ignore

class ModelSwitchBody(BaseModel):
    model: str = Field(..., description="Full model ID, e.g. anthropic/claude-sonnet-4")
    provider: Optional[str] = Field(None, description="Provider to pin (optional)")


@router.post("/model")
def switch_model(body: ModelSwitchBody):
    cfg = _load_config()
    model_cfg = cfg.setdefault("model", {})

    old_model = model_cfg.get("default", "unknown")
    model_cfg["default"] = body.model

    if body.provider:
        model_cfg["provider"] = body.provider
        old_provider = cfg.get("model", {}).get("provider", "auto")
    else:
        old_provider = None

    _save_config(cfg)

    log.info("Talaria: model switched from %s/%s to %s/%s",
             old_model, old_provider or "auto", body.model, body.provider or "auto")

    return {
        "ok": True,
        "previous": {"model": old_model, "provider": old_provider},
        "current": {"model": body.model, "provider": body.provider or model_cfg.get("provider", "auto")},
    }


# ──────────────────────────────────────────────
# GET /tools  — list toolsets
# ──────────────────────────────────────────────

@router.get("/tools")
def list_tools():
    cfg = _load_config()
    toolsets = cfg.get("platform_toolsets", {})

    result = {}
    for platform, config in toolsets.items():
        if isinstance(config, dict):
            result[platform] = {
                "enabled": config.get("enabled", []),
                "disabled": config.get("disabled", []),
            }
        elif isinstance(config, list):
            result[platform] = {"enabled": config, "disabled": []}

    return {"platforms": result}


# ──────────────────────────────────────────────
# POST /tools  — enable/disable a toolset for API server
# ──────────────────────────────────────────────

class ToolsToggleBody(BaseModel):
    toolset: str = Field(..., description="Toolset name, e.g. web, browser, terminal")
    action: str = Field(..., description="enable or disable")
    platform: str = Field("api_server", description="Platform to affect (default: api_server)")


@router.post("/tools")
def toggle_toolset(body: ToolsToggleBody):
    if body.action not in ("enable", "disable"):
        raise HTTPException(status_code=400, detail="action must be 'enable' or 'disable'")

    cfg = _load_config()
    platform_toolsets = cfg.setdefault("platform_toolsets", {})
    platform_cfg = platform_toolsets.setdefault(body.platform, {})

    if not isinstance(platform_cfg, dict):
        platform_cfg = {"enabled": [], "disabled": []}
        platform_toolsets[body.platform] = platform_cfg

    enabled: list = list(platform_cfg.get("enabled", []))
    disabled: list = list(platform_cfg.get("disabled", []))

    if body.action == "enable":
        if body.toolset in disabled:
            disabled.remove(body.toolset)
        if body.toolset not in enabled:
            enabled.append(body.toolset)
    else:
        if body.toolset in enabled:
            enabled.remove(body.toolset)
        if body.toolset not in disabled:
            disabled.append(body.toolset)

    platform_cfg["enabled"] = enabled
    platform_cfg["disabled"] = disabled
    _save_config(cfg)

    log.info("Talaria: toolset %s %sd for platform %s", body.toolset, body.action, body.platform)

    return {
        "ok": True,
        "platform": body.platform,
        "toolset": body.toolset,
        "action": body.action,
        "enabled": enabled,
        "disabled": disabled,
    }


# ──────────────────────────────────────────────
# GET /skills  — list installed skills
# ──────────────────────────────────────────────

@router.get("/skills")
def list_skills():
    skills_dir = _get_hermes_home() / "skills"
    result = []
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    result.append({
                        "name": skill_dir.name,
                        "path": str(skill_md),
                    })
    return {"skills": result}


# ──────────────────────────────────────────────
# POST /session/reset  — signal session reset
# ──────────────────────────────────────────────

class SessionResetBody(BaseModel):
    session_id: Optional[str] = Field(None, description="Session ID to reset (optional)")


@router.post("/session/reset")
def reset_session(body: SessionResetBody):
    """Signal a session reset. Returns instructions for the client.

    Since the API server creates a new AIAgent per request, the client
    simply needs to start a new session via POST /api/sessions.
    This endpoint is a hint — the actual reset happens client-side.
    """
    return {
        "ok": True,
        "action": "create_new_session",
        "hint": "POST /api/sessions to create a fresh session, then POST /v1/runs or POST /api/sessions/{id}/chat",
        "previous_session_id": body.session_id,
    }


# ──────────────────────────────────────────────
# GET /memory  — read memory info
# ──────────────────────────────────────────────

@router.get("/memory")
def get_memory_info():
    cfg = _load_config()
    mem_cfg = cfg.get("memory", {})
    return {
        "enabled": mem_cfg.get("memory_enabled", True),
        "user_profile_enabled": mem_cfg.get("user_profile_enabled", True),
        "provider": mem_cfg.get("provider", "built-in"),
        "storage_path": str(_get_hermes_home() / "memories"),
    }


# ──────────────────────────────────────────────
# POST /memory  — clear memory (stub)
# ──────────────────────────────────────────────

@router.post("/memory")
def clear_memory():
    """Placeholder — memory clearing not yet implemented via config."""
    return {
        "ok": False,
        "reason": "Memory clearing from external APIs is not yet supported. Use 'hermes memory off' via CLI/SSH.",
    }


# ──────────────────────────────────────────────
# GET /config  — full config readout (safe subset)
# ──────────────────────────────────────────────

@router.get("/config")
def get_full_config():
    cfg = _load_config()

    # Return a safe subset — no secrets
    safe = {}
    for section in ("model", "agent", "terminal", "memory", "compression", "display"):
        if section in cfg:
            section_data = dict(cfg[section])
            # Redact any key-looking fields
            for key in list(section_data.keys()):
                if "key" in key.lower() or "secret" in key.lower() or "password" in key.lower() or "token" in key.lower():
                    section_data[key] = "[redacted]"

            safe[section] = section_data

    return {"config": safe}
