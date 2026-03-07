# AWS Onboarding Role (Terraform)

## Usage

```hcl
module "aci_onboarding_role" {
  source = "./infra/onboarding/aws/terraform"

  aci_principal_arn = "arn:aws:iam::123456789012:role/aci-assumer"
  external_id       = "replace-with-long-random-external-id"

  # Optional CUR access:
  cur_bucket_name = "my-cur-bucket"
  cur_prefix      = "cur/"
}
```

## Apply

```bash
terraform init
terraform plan
terraform apply
```

Export `module.aci_onboarding_role.role_arn` to ACI onboarding configuration.
