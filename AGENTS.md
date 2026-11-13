# Repository conventions

Read this before changing anything. It is the contract for humans and agents working in this
repository.

## What this repository is

A generic, publishable implementation of a git-backed shared context hub for multi-bot agent
fleets, plus a web console. The **public repository never contains live data** — no hub
directory contents, no deployment configs, no credentials, no internal hostnames, no chat ids.
Live deployments keep their config and data outside the repository.

## Public-safety rules (hard requirements)

1. Never commit `config.toml`, `data/`, `hub/`, `run/`, logs, or anything under `.ctxbridge/`.
   `.gitignore` covers them; do not weaken it.
2. Never commit credentials, tokens, cookies, SSH keys or `.env` files.
3. Never put internal company information in code, docs, tests or sample data: no corporate
   domains (see the blocklist in `scripts/check_public_safety.py`), no private IPs, no internal hostnames, no real Feishu
   chat/message/user ids, no internal repository URLs. Use placeholders like `example.com`,
   `oc_EXAMPLE`, `/srv/context-hub`.
4. `scripts/check_public_safety.py` scans tracked files in CI. A failure blocks the PR; fix the
   leak, never the rule.
5. Live data stays redacted at write time (`redact.py`). When adding a collector field, redact it
   before storing it.

## Code conventions

- Python 3.11+, **standard library only** at runtime (pytest is the only dev dependency).
- Type hints on public functions; docstrings explain *why*, not *what*.
- Small modules with one owner (see the task board). Do not edit another owner's files without
  coordinating; shared contracts are `model.py`, `config.py`, `API.md`, `DESIGN.md`.
- No network access in tests. Integration tests that need a real host are marked
  `@pytest.mark.integration` and skipped by default.
- Web console: plain HTML/CSS/JS, no build step, no node_modules, no external CDN (it must work
  on an offline host).
- Keep the API contract in `API.md` in sync with `server.py`; the console is built against it.

## Testing

```bash
python3 -m pip install -e .[dev]
python3 -m pytest                      # unit tests, no network
python3 scripts/check_public_safety.py # public-repo leak scan
```

New behaviour needs a test. Bug fixes should include the regression case.

## Ownership map (updated per task)

| Area | Files | Owner |
| --- | --- | --- |
| Contracts | `model.py`, `config.py`, `DESIGN.md`, `API.md`, `AGENTS.md` | Lead |
| Data layer | `redact.py`, `collect/`, `digest.py`, `sync.py`, `tests/test_redact.py`, `tests/test_collect.py` | data-layer teammate |
| Hub + CLI | `hub.py`, `index.py`, `board.py`, `dispatch.py`, `cli.py`, `tests/test_hub.py`, `tests/test_cli.py` | hub teammate |
| API server | `server.py`, `tests/test_server.py` | API teammate |
| Web console | `web/` | frontend teammate |
| Deployment / docs | `scripts/`, `integrations/`, `README.md`, `examples/`, `.github/workflows/` | ops teammate |

The Lead commits and opens the PR. Commit messages follow
`<scope>: <what changed> (<why>)`.
