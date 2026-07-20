# Contributing

Thank you for your interest in SAG.

## Environment

- Backend: Python ≥ 3.11 (`apps/api`)
- Frontend: Node ≥ 20 (`apps/web`, use `npm ci`)

## Checks

```bash
cd apps/api && ruff check sag_api/ sag_agent/
cd apps/web && npx tsc --noEmit && npx next build
```

## Workflow

1. Branch from `public` or `dev`.
2. Keep commits focused; explain what changed and why.
3. Describe how the change was verified, including loading, empty, and error states for UI changes.
4. Open a pull request when checks pass.

## Issues

Please include reproduction steps, expected behavior, actual behavior, and your environment (OS, Python, Node).
