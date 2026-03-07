# GitHub App Onboarding (Read-Only)

This guide defines the minimum GitHub App permissions for ACI source-control and CI/CD metadata ingestion.

## Objective

Grant ACI read-only access to:

- repository metadata and collaborators
- organization teams/memberships
- pull request and workflow/deployment metadata

Do not grant code content write or repository administration scopes.

## Required App Permissions

Repository permissions:

- `Metadata`: `Read-only`
- `Pull requests`: `Read-only`
- `Actions`: `Read-only`
- `Deployments`: `Read-only`
- `Administration`: `Read-only` (only if required for environment metadata)
- `Contents`: `No access` (preferred). If your org requires CODEOWNERS file reads, use `Read-only`.

Organization permissions:

- `Members`: `Read-only`
- `Administration`: `Read-only` (optional, only if team-level metadata requires it)

Account permissions:

- None required.

## Required Webhook Events

- `push`
- `pull_request`
- `workflow_job`
- `deployment_status`

## Security Controls

- Restrict app installation to approved org(s)/repo(s) only.
- Use short-lived installation tokens only. Do not store personal access tokens.
- Rotate private key material per customer policy.
- Store private key and webhook secret in customer-managed secret storage.

## Mapping to PRD

- FR-102: GitHub SCM metadata ingestion.
- FR-103a: GitHub Actions deployment metadata ingestion.
- FR-112: onboarding automation with scoped credentials.
