# Onboarding Automation

This directory provides infrastructure-as-code assets for provisioning
read-only integration access during design partner onboarding.

## Scope

- AWS read-only IAM role for ACI ingestion and reconciliation metadata.
- Okta read-only token scoping guidance for identity ingestion.
- GitHub App read-only permission guidance for SCM/CI-CD ingestion.
- Two provisioning options:
  - CloudFormation template
  - Terraform module

## AWS Permissions Included

- Cost Explorer (`ce:GetCostAndUsage`, related reads)
- CloudWatch metrics/logs read
- CloudTrail lookup read
- Bedrock model invocation logging metadata read
- Optional CUR S3 object read (parameterized bucket/prefix)

## Paths

- CloudFormation: `infra/onboarding/aws/cloudformation/aci-readonly-role.yaml`
- Terraform: `infra/onboarding/aws/terraform/`
- Okta token scoping: `infra/onboarding/okta/README.md`
- GitHub App setup: `infra/onboarding/github/README.md`

## Security Notes

- Trust policy enforces explicit `ExternalId`.
- Role uses least-privilege read actions only.
- No write/delete permissions are granted.
