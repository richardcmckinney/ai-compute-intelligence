# Operations and Deployment

## Environments
- `staging`: integration and pre-production checks.
- `production`: release gate for production readiness.

## Workflows
- CI: lint, typecheck, unit, integration, glass-jaw, docker smoke.
- CodeQL: static security analysis on push/PR + weekly schedule.
- Dependency Review: PR-time dependency risk gate.
- Deployment Gate: manual workflow with environment selection and smoke tests.

## Release Guidance
1. Merge to `main` only after CI and CodeQL pass.
2. Run Deployment Gate for `staging`.
3. Run Deployment Gate for `production`.
4. Tag release commit and publish release notes.

## Operational SLO Suggestions
- API availability: >= 99.9%
- P99 interceptor overhead: <= 15ms target
- Failed-open request ratio: continuously monitored

