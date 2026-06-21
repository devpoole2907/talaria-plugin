# Talaria — Hermes Agent Admin API Plugin

> Backend plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that exposes programmatic control endpoints for external clients (iOS, desktop, web). Built to power the **Talaria** iOS app, but usable by any Hermes API consumer.

---

## Why

The Hermes API Server (`POST /v1/chat/completions`, `/v1/runs`, etc.) is a **chat surface** — it runs agent turns. It does not expose admin operations like switching models, toggling toolsets, or listing skills.

The Telegram and Discord adapters have full admin control because they live **inside** the Hermes gateway process and route through the slash-command dispatcher. External clients cannot.

Talaria bridges this gap with a **dashboard backend plugin** — a FastAPI router mounted at `/api/plugins/talaria/` on the Hermes web dashboard (port 9119). It reads and writes `config.yaml` directly, giving any authenticated client programmatic access to:

- Switch models
- Toggle toolsets per platform
- List skills
- Read memory config
- Signal session resets
- Read safe config subsets

---

## Endpoints

All endpoints live under `/api/plugins/talaria/` on the dashboard port (default `9119`).

| Method | Path | Description |
|---|---|---|
| `GET` | `/status` | Health check + current model, provider, terminal backend, max turns, memory state |
| `GET` | `/model` | Read current model (`default`, `provider`, `base_url`, `context_length`) |
| `POST` | `/model` | Switch model. Body: `{"model": "anthropic/claude-sonnet-4", "provider": "anthropic"}` |
| `GET` | `/tools` | List all platform toolsets with enabled/disabled arrays |
| `POST` | `/tools` | Enable or disable a toolset. Body: `{"toolset": "web", "action": "enable", "platform": "api_server"}` |
| `GET` | `/skills` | List installed skills (name + path) |
| `GET` | `/memory` | Memory config (`enabled`, `user_profile_enabled`, `provider`, `storage_path`) |
| `GET` | `/config` | Safe config subset (model, agent, terminal, memory, compression, display — secrets redacted) |
| `POST` | `/session/reset` | Get instructions for creating a fresh session (hint for API clients) |
| `POST` | `/attachments` | Upload a file (`multipart/form-data`, field `file`; optional `session_id`). Returns `{id, filename, stored_path, relative_path, size, content_type}` |
| `GET` | `/attachments/{id}` | Download a previously uploaded file |
| `DELETE` | `/attachments/{id}` | Delete an uploaded file (the app calls this after the turn is sent) |

---

## File attachments

The Hermes API server (`:8642`) only accepts inline `image_url` parts on
`/api/sessions/{id}/chat` — uploaded files and document content parts are
rejected (`unsupported_content_type`). So the app can't attach a PDF or text
doc through the chat endpoint directly.

This plugin bridges that: the app `POST`s the file to `/attachments`, the
plugin streams it to `HERMES_HOME/talaria_uploads/<id>/<name>`, and returns both
`stored_path` (this process's absolute view) and `relative_path`
(`talaria_uploads/<id>/<name>`, relative to `HERMES_HOME`). The app references
`~/.hermes/<relative_path>` in a normal chat turn, and the agent's server-side
`read_file` / `web_extract` tools read the file (`web_extract` handles PDFs).
Images keep using the app's inline `image_url` path — this is for documents.

> **Why `relative_path`:** when the plugin runs in a container
> (`HERMES_HOME=/opt/data`) but the agent's tools run on the host
> (`HERMES_HOME=~/.hermes`), the same file has two absolute paths. The
> container-view `stored_path` won't resolve for the agent, so the client uses
> the mount-independent `~/.hermes/<relative_path>` instead.

Keeping this in the plugin means a Hermes upgrade only ever touches the plugin,
never the app. Uploads are capped at 25 MB; filenames are sanitised to a safe
basename; ids are server-minted UUIDs validated against path traversal.

```shell
# Upload (cookie-authed, like the other endpoints)
curl -b cookies.txt -F "file=@report.pdf" -F "session_id=api_123" \
  http://localhost:9119/api/plugins/talaria/attachments
# → {"ok":true,"id":"…","filename":"report.pdf","stored_path":"/…/report.pdf","size":12345,"content_type":"application/pdf"}
```

---

## Installation

### Quick install

```shell
mkdir -p ~/.hermes/plugins
git clone https://github.com/devpoole2907/talaria-plugin.git ~/.hermes/plugins/talaria
```

Then restart the dashboard (Python plugins are mounted at startup):

```shell
hermes gateway restart
```

The endpoints will be available at `http://<host>:9119/api/plugins/talaria/status`.

### Verify

```shell
# Login to get a session cookie
curl -c cookies.txt -X POST http://localhost:9119/auth/password-login \
  -H "Content-Type: application/json" \
  -d '{"provider":"basic","username":"<user>","password":"<pass>"}'

# Hit an endpoint
curl -b cookies.txt http://localhost:9119/api/plugins/talaria/status
```

### Docker (Hermes in a container)

If Hermes runs in Docker with `~/.hermes:/opt/data`, the plugin path inside the container is `/opt/data/plugins/talaria/`. Clone to the host path and it's immediately visible:

```shell
git clone https://github.com/devpoole2907/talaria-plugin.git ~/.hermes/plugins/talaria
docker restart hermes
```

---

## Authentication

The dashboard uses **session-cookie auth**, not Bearer tokens.

1. `POST /auth/password-login` with `{"provider": "basic", "username": "...", "password": "..."}`
2. Store the returned `hermes_session_rt` cookie
3. Include it on all subsequent requests

For the Talaria iOS app, the flow is:

```
Port 8642 → Hermes API Server (chat, sessions, runs) — Bearer token auth
Port 9119 → Talaria plugin (admin commands)       — Session cookie auth
```

---

## Architecture

```
┌─────────────────────────────────────────┐
│  Talaria iOS App                        │
│                                         │
│  Chat ────► :8642/v1/runs (Bearer)     │
│  Admin ───► :9119/api/plugins/talaria/  │
│              (Session cookie)           │
└────────────┬────────────────────────────┘
             │
    ┌────────▼────────┐
    │  Hermes Gateway │
    │  (Docker)       │
    │                 │
    │  Dashboard ─────┤
    │  Plugin Router  │
    │       │         │
    │  Talaria API ───┤  reads/writes config.yaml
    │  (FastAPI)      │  reads skills/ dir
    └─────────────────┘
```

---

## File Structure

```
talaria-plugin/
├── dashboard/
│   ├── manifest.json       # Plugin metadata (name, version, icon, api entry)
│   └── plugin_api.py       # FastAPI router with all endpoints
├── LICENSE
└── README.md
```

No JavaScript frontend — this is a **pure API plugin**. No build step, no npm, no dependencies beyond what Hermes ships (FastAPI, pydantic, `hermes_cli.config`).

---

## Requirements

- Hermes Agent with web dashboard enabled (`HERMES_DASHBOARD=1` or `hermes dashboard`)
- Python 3.11+ (comes with Hermes)
- Dashboard auth configured (`HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`)
- No additional pip packages — uses `fastapi`, `pydantic`, and `hermes_cli.config` from Hermes core

---

## Security

- All endpoints require dashboard authentication (session cookie)
- `/config` endpoint redacts fields containing `key`, `secret`, `password`, or `token`
- Plugin reads secrets only indirectly via `hermes_cli.config.load_config()` — it never touches `.env` directly
- No secrets are hardcoded anywhere in the plugin source
- Model switches and toolset toggles are logged via the Python `logging` module

---

## Companion Projects

- **[Talaria iOS](https://github.com/devpoole2907/talaria)** — Native iOS 26.2 SwiftUI client for Hermes Agent
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — The self-improving AI agent by Nous Research

---

## License

MIT — see [LICENSE](LICENSE)
