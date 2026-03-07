terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = [var.aci_principal_arn]
    }
    actions = ["sts:AssumeRole"]
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.external_id]
    }
  }
}

data "aws_iam_policy_document" "core_readonly" {
  statement {
    sid    = "CostExplorerRead"
    effect = "Allow"
    actions = [
      "ce:GetCostAndUsage",
      "ce:GetCostAndUsageWithResources",
      "ce:GetDimensionValues",
      "ce:GetTags",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudWatchRead"
    effect = "Allow"
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:StartQuery",
      "logs:GetQueryResults",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudTrailRead"
    effect = "Allow"
    actions = [
      "cloudtrail:LookupEvents",
      "cloudtrail:GetTrailStatus",
      "cloudtrail:DescribeTrails",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "BedrockRead"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:GetModelInvocationLoggingConfiguration",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ResourceInventoryRead"
    effect = "Allow"
    actions = [
      "ec2:DescribeRegions",
      "ec2:DescribeTags",
      "resourcegroupstaggingapi:GetResources",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "cur_readonly" {
  count = var.cur_bucket_name == "" ? 0 : 1

  statement {
    sid       = "CurBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.cur_bucket_name}"]
  }

  statement {
    sid    = "CurObjectRead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = ["arn:aws:s3:::${var.cur_bucket_name}/${var.cur_prefix}*"]
  }
}

resource "aws_iam_role" "aci_readonly" {
  name                 = var.role_name
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
}

resource "aws_iam_role_policy" "core_readonly" {
  name   = "${var.role_name}-core-readonly"
  role   = aws_iam_role.aci_readonly.id
  policy = data.aws_iam_policy_document.core_readonly.json
}

resource "aws_iam_role_policy" "cur_readonly" {
  count  = var.cur_bucket_name == "" ? 0 : 1
  name   = "${var.role_name}-cur-readonly"
  role   = aws_iam_role.aci_readonly.id
  policy = data.aws_iam_policy_document.cur_readonly[0].json
}
