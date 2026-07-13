# ADR-002: Replace Streamlit with a React SPA

Status: Accepted

## Context

The current UI is a single Streamlit file (`src/msa_apps/ui_streamlit/ui.py`, 900+ lines)
running on port 8501 as a separate process. It is functional but has significant limitations:

- Full page reloads on every filter change or pagination action.
- No real-time updates — indexer progress requires polling workarounds.
- No persistent client-side state across tab switches.
- Face labeling requires navigating between tabs; no inline assignment.
- Requires a separate process on a separate port (8501), complicating launchers and firewall rules.
- Not extensible toward a native desktop shell (Tauri/Electron) if ever needed.
- OpenWebUI-style single-landing-page UX (one place for config, search, and management)
  is not achievable in Streamlit without significant hacks.

## Decision

Replace Streamlit with a **React 18 + TypeScript + Vite SPA** served as static files
directly by FastAPI at `/`. This eliminates port 8501 and the Streamlit process entirely.

Tech stack:
- **React 18 + TypeScript + Vite** — fast builds, strong ecosystem, standard for SPAs
- **Tailwind CSS + shadcn/ui** (Radix primitives) — accessible, easily themed, used by
  OpenWebUI and similar tools
- **TanStack Query** — server state management, caching, pagination; results update
  without page reloads
- **WebSocket** (`GET /ws/indexer`) — real-time indexer progress streamed from FastAPI
- **FastAPI static serving** — `app.mount("/", StaticFiles(directory="ui/dist"))`;
  single port 8000 for both API and UI

## Rationale

- Serving from FastAPI at port 8000 eliminates the Streamlit process, port 8501,
  and all associated firewall/launcher complexity.
- TanStack Query handles cache invalidation cleanly — search results, face lists,
  and people update reactively without page reloads.
- WebSocket enables real-time indexer progress with zero polling overhead.
- shadcn/ui + sidebar navigation enables the OpenWebUI-style layout the user wants:
  one landing page with Settings, Search, Browse, and Face workspaces.
- The React build output is static HTML/JS/CSS — portable to any static host
  if a future server deployment mode is needed.
- Removing Streamlit reduces `requirements-api.txt` by one large dependency.

## Migration Strategy

Streamlit runs in parallel throughout the React migration. It is only removed
once all React workspaces are validated. This allows incremental development
without breaking the working UI during the transition.

The earlier Setup & Index tab in Streamlit is explicitly temporary — it
provides self-service indexer control in the interim and is replaced by the
React workspace.

## Consequences

- `npm run build` must be integrated into `setup.sh` and `install.sh` as part
  of the install step. Node.js is a build-time dependency (not runtime — the output
  is static files).
- The FastAPI `app.py` gains a static file mount; the existing API routes are unchanged.
- The Streamlit indexer tab is knowingly temporary technical work.
- Once the migration completes: `start.sh` no longer launches Streamlit; port 8501 is fully retired.
