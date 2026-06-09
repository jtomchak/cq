# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**cq** is an open standard for shared agent learning — a knowledge commons that lets AI
agents query structured "knowledge units" (KUs) before acting and propose new learnings
after, so agents avoid repeating each other's mistakes. (Published as `mozilla-ai/cq`;
the local clone directory is `grand-central`.)

A **knowledge unit** (`ku_<32-hex>`) carries an `insight` (summary / detail / action),
`context` (languages / frameworks / pattern), `domains` tags, and `evidence` (confidence
0.0–1.0, starts at 0.5, ±0.1 per confirmation/flag). KUs live in three tiers:
**local** (machine-only SQLite) → **private** (team remote) → **public** (global commons),
graduating via human review.

## Monorepo layout — one contract, many languages

Everything is orchestrated from the root `Makefile`. Components:

| Path             | Lang       | Role |
|------------------|------------|------|
| `cli/`           | Go         | Cobra CLI + MCP server (`mcp-go`), spawned over stdio by the agent. Owns local store `~/.local/share/cq/local.db`. Entry: `cli/main.go`, tools in `cli/mcpserver/`. |
| `sdk/go/`        | Go         | Public Go SDK. Entry: `sdk/go/client.go`. |
| `sdk/python/`    | Python     | Public Python SDK (`cq-sdk` on PyPI). Entry: `sdk/python/src/cq/client.py`. |
| `server/backend/`| Python     | Remote API (FastAPI + SQLite, Alembic migrations). Entry: `server/backend/src/cq_server/app.py`. |
| `server/frontend/`| TS/React  | Admin UI (Vite + pnpm). |
| `server-cf/`     | TypeScript | Cloudflare Workers + D1 deployment — a **1:1, wire-compatible port** of `server/backend`. |
| `schema/`        | JSON+Go+Py | JSON Schemas are the source of truth (`schema/knowledge_unit.json`, `scoring.json`, …); published as `cq-schema`. |
| `plugins/cq/`    | MD+Python  | Claude Code plugin: `SKILL.md` (agent behavior), `/cq:status` and `/cq:reflect` commands, hooks. |
| `scripts/install/`| Python    | Multi-host installer (Claude Code, Cursor, OpenCode, Windsurf). |

### The five MCP tools (the whole surface area)
`query` (search before acting), `propose` (submit a KU), `confirm` (endorse), `flag`
(mark wrong/stale), `status` (store stats).

### Three runtime boundaries
1. **Agent process** — loads `plugins/cq/skills/cq/SKILL.md`; no cq code runs here.
2. **Local MCP server** — the Go CLI over stdio; owns the local SQLite store.
3. **Remote API** (optional) — FastAPI (`server/backend`) or the Cloudflare port
   (`server-cf`). SDK is local-first: if the remote is unreachable it stores locally and
   **drains** to the remote on next connection.

## Critical sync invariants (CI enforces these)

These are the easiest things to break — `make lint` runs all of these checks:

- **Prompts** (`SKILL.md`, reflect, etc.) live canonically in `plugins/cq/` and are
  **copied** into both SDKs. After editing a prompt, run `make sync-prompts`.
  `make check-prompts-sync` fails CI if copies drift.
- **Schema**: JSON schemas in `schema/` are copied into the Python schema package.
  Run `make sync-schema` after editing them; `make validate-schema` checks fixtures.
- **`server-cf` mirrors `server/backend`**: it is a deliberate 1:1 port (repositories,
  services, routes). Any change to the Python server's behavior or wire format must be
  reflected in the TypeScript port to keep them wire-compatible.

## Common commands

All from the repo root. Each `*-<component>` target delegates to the component dir.

```bash
make setup     # install deps for every component
make lint      # lint everything + verify prompt/schema sync
make test      # test everything
```

Per-component (examples — see `make help` for the full matrix):

```bash
make test-cli            # cd cli && go test ./... -v
make test-sdk-python     # cd sdk/python && uv run pytest
make test-server-backend # cd server/backend && uv run pytest (runs validate-schema first)
make lint-sdk-go         # checks prompt sync, then golangci-lint
```

Run a **single test** by working in the component directory:

```bash
cd cli && go test ./mcpserver/ -run TestQueryTool -v        # Go
cd sdk/python && uv run pytest tests/test_client.py::test_x # Python (uv)
cd server/frontend && pnpm test -- <pattern>                # frontend (vitest)
```

### Local server / end-to-end

```bash
make compose-up                          # build + start server (creates .env from example)
make seed-users USER=demo PASS=demo123   # create a user
make seed-all   USER=demo PASS=demo123   # user + sample KUs
make dev-api                             # backend only on :8742
make dev-ui                              # frontend dev server
make compose-reset                       # stop + wipe DB
```

## Conventions

- **Python**: managed with `uv` (Python 3.11+); lint/format via Ruff + pre-commit
  (`.pre-commit-config.yaml`). Go 1.26.1+; `golangci-lint run --fix`. Frontend uses pnpm.
- **Wire format is `snake_case` JSON** across all languages.
- `created_by` on a KU is set by the server from the authenticated API key — never
  supplied by the client.
- Relevance scoring: `0.55*jaccard(domains) + 0.15*language + 0.15*framework +
  0.15*pattern`; results ranked by `relevance * confidence`. Canonical weights live in
  `schema/scoring.values.json` and are read by both Go and Python — change them there.

## Status

This is a `0.x` project — expect breaking changes. See `DEVELOPMENT.md` for repo structure
and `docs/architecture.md` for detailed diagrams (knowledge flow, tier graduation, schema).
