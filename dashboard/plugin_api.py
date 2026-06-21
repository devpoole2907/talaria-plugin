"""Talaria admin plugin — iOS app control surface.

Mounted at /api/plugins/talaria/ by the Hermes dashboard plugin system.

Endpoints:
  GET    /status      — agent health + current config summary
  GET    /model       — read current model
  POST   /model       — switch model (persisted to config.yaml)
  POST   /session/reset — reset/clear active session
  GET    /tools       — list toolsets and their enabled state
  POST   /tools       — enable/disable a toolset
  GET    /skills      — list installed skills
  GET    /memory      — read memory usage/info
  POST   /memory      — clear memory
  POST   /attachments — upload a file; returns its on-disk path for the agent
  GET    /attachments/{id} — download a previously uploaded file
  DELETE /attachments/{id} — delete an uploaded file
  GET    /admin/model/info    — current model metadata (delegates to dashboard)
  GET    /admin/model/options — provider/model picker catalog (delegates)
  POST   /admin/model/set     — switch model (delegates)
  GET    /admin/config        — full runtime config (delegates)
  GET    /admin/skills        — installed skills + enabled state (delegates)
  GET    /admin/toolsets      — configurable toolsets + state (delegates)

The /admin/* endpoints are stable-path wrappers over the dashboard's own
handlers so the iOS app depends on this plugin, not un-versioned dashboard
routes — see the "Admin facade" section below.
"""

from __future__ import annotations

import inspect
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile, status as http_status
    from fastapi.responses import FileResponse
except Exception:
    class APIRouter:  # type: ignore
        def get(self, *_args, **_kwargs): return lambda fn: fn
        def post(self, *_args, **_kwargs): return lambda fn: fn
        def delete(self, *_args, **_kwargs): return lambda fn: fn
    class HTTPException(Exception): pass  # type: ignore
    def Body(*_args, **_kwargs): return None  # type: ignore
    def File(*_args, **_kwargs): return None  # type: ignore
    def Form(*_args, **_kwargs): return None  # type: ignore
    class UploadFile:  # type: ignore
        filename: str = ""
        content_type: str = ""
    class FileResponse:  # type: ignore
        def __init__(self, *_args, **_kwargs): ...

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


# ──────────────────────────────────────────────
# Attachments — upload / download / delete
# ──────────────────────────────────────────────
#
# Why this lives in the plugin:
#   The Hermes API server (port 8642) rejects file/document content parts —
#   only inline ``image_url`` parts are accepted on /api/sessions/{id}/chat.
#   So the Talaria app can't attach a PDF/text doc through the chat endpoint.
#
# How it works:
#   The app uploads a document here. We stream it to the Hermes host's
#   filesystem and return the absolute ``stored_path``. The app then references
#   that path in a normal chat turn, and the agent's server-side ``read_file`` /
#   ``web_extract`` tools ingest the file. Images keep using the app's inline
#   image_url path; this endpoint is for documents.
#
#   Keeping this in the plugin means a Hermes upgrade only ever touches the
#   plugin — the app's upload contract stays put.

_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB — covers PDFs, docs, images
_UPLOAD_DIRNAME = "talaria_uploads"
_UPLOAD_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def _uploads_root() -> Path:
    """Return (and create) the per-host upload directory under HERMES_HOME."""
    root = _get_hermes_home() / _UPLOAD_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Collapses any directory components so ``../../etc/passwd`` becomes its
    leaf, drops control characters and leading dots, and caps the length.
    The result is only ever joined under a freshly-created upload dir.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch != "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        name = "attachment"
    return name[:200]


def _resolve_upload_dir(upload_id: str) -> Path:
    """Map a client-supplied id to its upload dir, rejecting traversal.

    The id is always a bare 32-char uuid hex we minted — anything with a path
    separator or wrong shape is rejected before touching the filesystem.
    """
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="invalid attachment id")
    root = _uploads_root().resolve()
    target = (root / upload_id).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid attachment id")
    return target


@router.post("/attachments")
async def upload_attachment(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
):
    """Store an uploaded file and return its absolute on-disk path.

    The blob lands under ``HERMES_HOME/talaria_uploads/<id>/<name>``. The app
    passes the returned ``stored_path`` to the agent in a chat turn so
    ``read_file`` / ``web_extract`` can read it.
    """
    safe_name = _safe_attachment_name(getattr(file, "filename", "") or "")
    upload_id = uuid.uuid4().hex
    dest_dir = _uploads_root() / upload_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    total = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ATTACHMENT_BYTES:
                    out.close()
                    shutil.rmtree(dest_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"attachment exceeds {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"failed to store attachment: {exc}")

    log.info(
        "Talaria: stored attachment %r (%d bytes) id=%s session=%s",
        safe_name, total, upload_id, session_id or "-",
    )
    # ``stored_path`` is this process's absolute view. When the plugin runs in a
    # container (HERMES_HOME=/opt/data) but the agent's tools run on the host
    # (HERMES_HOME=~/.hermes), that absolute path won't resolve for the agent.
    # ``relative_path`` is the stable handle under HERMES_HOME — the client
    # references it as ``~/.hermes/<relative_path>`` so the agent finds the file
    # regardless of the container/host mount mapping.
    relative_path = f"{_UPLOAD_DIRNAME}/{upload_id}/{safe_name}"
    return {
        "ok": True,
        "id": upload_id,
        "filename": safe_name,
        "stored_path": str(dest_path.resolve()),
        "relative_path": relative_path,
        "size": total,
        "content_type": getattr(file, "content_type", None),
        "session_id": session_id,
    }


@router.get("/attachments/{upload_id}")
def download_attachment(upload_id: str):
    """Serve a previously uploaded file (round-trip / verification)."""
    dest_dir = _resolve_upload_dir(upload_id)
    if not dest_dir.is_dir():
        raise HTTPException(status_code=404, detail="attachment not found")
    files = [p for p in dest_dir.iterdir() if p.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="attachment file missing on disk")
    blob = files[0]
    return FileResponse(path=str(blob), filename=blob.name)


@router.delete("/attachments/{upload_id}")
def delete_attachment(upload_id: str):
    """Delete an uploaded file. The app calls this after the turn is sent."""
    dest_dir = _resolve_upload_dir(upload_id)
    existed = dest_dir.is_dir()
    shutil.rmtree(dest_dir, ignore_errors=True)
    log.info("Talaria: deleted attachment id=%s existed=%s", upload_id, existed)
    return {"ok": True, "deleted": existed, "id": upload_id}


# ──────────────────────────────────────────────
# Admin facade — stable-path wrappers over the dashboard's own handlers
# ──────────────────────────────────────────────
#
# The app talks to these instead of the raw dashboard routes (/api/model/*,
# /api/config, /api/skills, /api/tools/toolsets). Each delegates to the *exact*
# function the dashboard route uses (the plugin runs in the same process), so
# responses are byte-identical and the app's decoders never change — but the
# path the app depends on lives here, under our control. If a Hermes upgrade
# renames a dashboard route, only this plugin changes; if it renames a handler,
# we adjust the lookup in `_ds_handler` once. Either way the app is insulated
# and never needs a release to track a Hermes change.


def _import_web_server():
    """Import the dashboard module, surfacing any failure as a clean 501.

    Catches BaseException (not just Exception) because a fresh import of
    web_server can raise SystemExit/argparse exits if it runs in a process that
    didn't already load it.
    """
    try:
        from hermes_cli import web_server
        return web_server
    except BaseException as exc:  # noqa: BLE001 — SystemExit et al. must not 500
        raise HTTPException(
            status_code=501,
            detail=f"dashboard module unavailable: {type(exc).__name__}: {exc}",
        )


def _ds_handler(name: str):
    """Resolve a dashboard handler function by name.

    Returns a clear 501 (rather than a 500) when this Hermes version doesn't
    expose the expected handler, so the app can detect a version mismatch and
    degrade gracefully instead of crashing.
    """
    web_server = _import_web_server()
    fn = getattr(web_server, name, None)
    if fn is None or not callable(fn):
        raise HTTPException(
            status_code=501,
            detail=f"dashboard handler '{name}' is not available in this Hermes version",
        )
    return fn


def _accepted_kwargs(fn, kwargs: dict) -> dict:
    """Filter kwargs to only those the target function actually accepts.

    Hermes handler signatures drift across versions (e.g. some `get_model_info`
    builds take `profile`, others don't). Passing an unsupported kwarg raises
    TypeError → a 500. Introspecting and passing only accepted names makes the
    facade resilient to those signature changes — the whole point of the facade.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


async def _delegate(name: str, *args, **kwargs):
    """Call a dashboard handler with only the kwargs it supports.

    Handles both sync and async handlers uniformly (older builds expose some of
    these as plain functions, newer ones as coroutines).
    """
    fn = _ds_handler(name)
    result = fn(*args, **_accepted_kwargs(fn, kwargs))
    if inspect.isawaitable(result):
        result = await result
    return result


@router.get("/admin/_diag")
async def admin_diag():
    """Diagnostic: report whether the dashboard handlers are reachable in-process
    and which signatures they expose. Useful when a Hermes upgrade shifts them."""
    import os
    import sys
    info: dict = {
        "pid": os.getpid(),
        "web_server_in_sys_modules": "hermes_cli.web_server" in sys.modules,
    }
    try:
        web_server = _import_web_server()
    except HTTPException as exc:
        info["import"] = "failed"
        info["detail"] = exc.detail
        return info
    info["import"] = "ok"
    wanted = [
        "get_model_info", "get_model_options", "set_model_assignment",
        "get_config", "get_skills", "get_toolsets", "ModelAssignment",
    ]
    sigs = {}
    for n in wanted:
        fn = getattr(web_server, n, None)
        try:
            sigs[n] = str(inspect.signature(fn)) if callable(fn) else None
        except (TypeError, ValueError):
            sigs[n] = "<no signature>"
    info["handlers"] = sigs
    try:
        mi = await _delegate("get_model_info")
        info["get_model_info_call"] = {"ok": True, "model": mi.get("model") if isinstance(mi, dict) else str(type(mi))}
    except BaseException as exc:  # noqa: BLE001
        info["get_model_info_call"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return info


@router.get("/admin/model/info")
async def admin_model_info(profile: Optional[str] = None):
    """Resolved current-model metadata — delegates to dashboard get_model_info."""
    return await _delegate("get_model_info", profile=profile)


@router.get("/admin/model/options")
async def admin_model_options(refresh: bool = False, profile: Optional[str] = None):
    """Provider/model picker catalog — delegates to dashboard get_model_options."""
    return await _delegate("get_model_options", refresh=refresh, profile=profile)


@router.post("/admin/model/set")
async def admin_model_set(payload: dict = Body(...), profile: Optional[str] = None):
    """Switch the model — delegates to dashboard set_model_assignment.

    Accepts the same body as /api/model/set: {scope, provider, model, task?,
    base_url?}. Persisted to config.yaml; applies to new sessions.
    """
    web_server = _import_web_server()
    model_assignment = getattr(web_server, "ModelAssignment", None)
    if model_assignment is None:
        raise HTTPException(status_code=501, detail="ModelAssignment unavailable in this Hermes version")
    try:
        assignment = model_assignment(**payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid model assignment: {exc}")
    return await _delegate("set_model_assignment", assignment, profile=profile)


@router.get("/admin/config")
async def admin_config(profile: Optional[str] = None):
    """Full runtime config — delegates to dashboard get_config."""
    return await _delegate("get_config", profile=profile)


@router.get("/admin/skills")
async def admin_skills(profile: Optional[str] = None):
    """Installed skills with enabled state — delegates to dashboard get_skills."""
    return await _delegate("get_skills", profile=profile)


@router.get("/admin/toolsets")
async def admin_toolsets(profile: Optional[str] = None):
    """Configurable toolsets with enabled/available state — delegates to get_toolsets."""
    return await _delegate("get_toolsets", profile=profile)
