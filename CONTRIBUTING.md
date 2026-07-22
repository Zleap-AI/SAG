# Contributing

Thank you for your interest in SAG.

## Environment

- Backend: Python ≥ 3.11 (`apps/api`)
- Frontend: Node ≥ 20 (`apps/web`, use `npm ci`)

## Checks

```bash
cd apps/api && ruff check sag_api/ sag_agent/ tests/ && python -m pytest -q
cd apps/web && npm run typecheck && npm run lint && npm run test:unit && npm run build
```

## Workflow

1. Branch from public `main`.
2. Keep commits focused and explain what changed and why.
3. Include tests and describe loading, empty, and error states for UI changes.
4. Open a pull request to `main` after all checks pass.

## Issues

Please include reproduction steps, expected behavior, actual behavior, and your environment (OS, Python, and Node versions).
