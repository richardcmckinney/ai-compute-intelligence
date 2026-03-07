# Okta API Token Scoping (Read-Only)

This guide defines the minimum Okta access model for ACI identity ingestion.

## Objective

Grant ACI read-only access to:

- user directory metadata
- group directory metadata
- group membership edges
- System Log events used for identity change tracking

Do not grant administrative write privileges.

## Recommended Access Model

1. Create a dedicated Okta service account for ACI.
2. Assign the least-privilege custom admin role containing only read scopes.
3. Issue a scoped API token tied to that service account.
4. Restrict token usage by network zone/IP allowlist where possible.

## Required Read Scopes

- `okta.users.read`
- `okta.groups.read`
- `okta.logs.read`

Optional (only when needed):

- `okta.apps.read`

Explicitly disallow:

- any `*.manage` scope
- lifecycle, credential, factor, or policy write scopes

## Data Handling Notes

- Persist only stable identifiers and membership edges required for attribution.
- Apply email pseudonymization before graph persistence.
- Keep raw PII in segregated, access-controlled storage.

## Mapping to PRD

- FR-101a: Okta System Log + SCIM identity event ingestion.
- FR-112: onboarding automation with scoped identity credentials.
