variable "aci_principal_arn" {
  description = "IAM principal ARN allowed to assume this role"
  type        = string
}

variable "external_id" {
  description = "External ID shared with ACI for secure role assumption"
  type        = string
}

variable "role_name" {
  description = "Name of the IAM role created for ACI read-only access"
  type        = string
  default     = "aci-readonly-integration"
}

variable "cur_bucket_name" {
  description = "Optional CUR S3 bucket name for billing export reads"
  type        = string
  default     = ""
}

variable "cur_prefix" {
  description = "Optional CUR object prefix"
  type        = string
  default     = ""
}
