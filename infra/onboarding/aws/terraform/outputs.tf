output "role_arn" {
  description = "ARN of the ACI read-only integration role"
  value       = aws_iam_role.aci_readonly.arn
}
