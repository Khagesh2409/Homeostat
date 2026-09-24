# AWS Setup Guide — Step 0.1

## What We're Creating

```
Your AWS Account
├── You (admin user) — manages everything
├── homeostat-agent (IAM role) — the AI agent uses this
│   ├── CAN: manage pods, use Bedrock, read/write S3 & DynamoDB
│   └── CANNOT: touch the watchdog, change its own permissions
├── homeostat-watchdog (IAM role) — the safety watchdog uses this
│   ├── CAN: shut down the agent, read costs, alert you
│   └── CANNOT: do anything else
└── AWS Budget — hard spending limit
```

## Prerequisites

- An AWS account (create at https://aws.amazon.com if you don't have one)
- AWS CLI installed (https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html)

---

# Part 1 — Manual Setup (AWS Console)

Do everything below from your browser at https://console.aws.amazon.com.
Make sure you're in the **us-east-1** (N. Virginia) region — check the dropdown in the top-right corner.

---

## Step 1: Create an Admin User for Yourself

> You don't want to use the root account for daily work. Create a proper admin user.

1. Go to **IAM** (search "IAM" in the top search bar)
2. In the left sidebar, click **Users**
3. Click **Create user**
4. User name: `homeostat-admin`
5. Check ✅ **Provide user access to the AWS Management Console**
6. Choose **I want to create an IAM user** (not Identity Center)
7. Set a password → click **Next**
8. On the permissions page, click **Attach policies directly**
9. Search for `AdministratorAccess` and check ✅ it
10. Click **Next** → **Create user**
11. **Save the sign-in URL, username, and password somewhere safe**

### Create Access Keys (for CLI)

1. Click on the user `homeostat-admin` you just created
2. Go to the **Security credentials** tab
3. Scroll down to **Access keys** → click **Create access key**
4. Choose **Command Line Interface (CLI)**
5. Check the acknowledgment box → click **Next** → **Create access key**
6. **Copy both the Access Key ID and Secret Access Key** — you won't see the secret again!
7. Open your terminal and run:
   ```
   aws configure
   ```
   Paste the access key, secret key, enter `us-east-1` for region, `json` for output.

---

## Step 2: Create the Agent IAM Role

> This is the role the AI agent will use. It can manage servers and call Bedrock, but it CANNOT change IAM permissions.

### 2a. Create the Permission Boundary first

The permission boundary is the "fence" — it hard-blocks dangerous actions.

1. Go to **IAM → Policies** (left sidebar)
2. Click **Create policy**
3. Click the **JSON** tab (top right of the editor)
4. Delete whatever is there and paste this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAgentWorkload",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:CreateTags",
        "ec2:DeleteTags",
        "ec2:AssociateAddress",
        "ec2:DisassociateAddress",
        "ec2:AllocateAddress",
        "ec2:ReleaseAddress",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms",
        "budgets:ViewBudget",
        "budgets:DescribeBudget",
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath",
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "sts:GetCallerIdentity",
        "sts:AssumeRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyAllIAM",
      "Effect": "Deny",
      "Action": "iam:*",
      "Resource": "*"
    },
    {
      "Sid": "DenyAccountManagement",
      "Effect": "Deny",
      "Action": [
        "organizations:*",
        "account:*"
      ],
      "Resource": "*"
    }
  ]
}
```

5. Click **Next**
6. Policy name: `homeostat-agent-boundary`
7. Description: `Permission boundary for Homeostat agent — blocks self-modification`
8. Click **Create policy**

### 2b. Create the Agent Role

1. Go to **IAM → Roles** (left sidebar)
2. Click **Create role**
3. Trusted entity type: **AWS service**
4. Use case: **EC2** → click **Next**
5. On the permissions page, **don't attach any policies yet** → click **Next**
6. Role name: `homeostat-agent`
7. **Expand the "Step 2" section** → under **Permissions boundary**, click **Set permissions boundary**
8. Search for `homeostat-agent-boundary` and select it ✅
9. Click **Create role**

### 2c. Add the Agent's Permissions

1. Click on the role `homeostat-agent` you just created
2. In the **Permissions** tab, click **Add permissions → Create inline policy**
3. Click the **JSON** tab and paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EC2Tagged",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:CreateTags",
        "ec2:DeleteTags"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/Project": "homeostat"
        }
      }
    },
    {
      "Sid": "EC2Describe",
      "Effect": "Allow",
      "Action": "ec2:Describe*",
      "Resource": "*"
    },
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::homeostat-*",
        "arn:aws:s3:::homeostat-*/*"
      ]
    },
    {
      "Sid": "DynamoDBAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:*:table/homeostat-*"
    },
    {
      "Sid": "BedrockAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MonitoringReadOnly",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms",
        "budgets:ViewBudget",
        "budgets:DescribeBudget"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SSMReadOnly",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Resource": "arn:aws:ssm:us-east-1:*:parameter/homeostat/*"
    },
    {
      "Sid": "ECRPull",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability"
      ],
      "Resource": "*"
    }
  ]
}
```

4. Policy name: `homeostat-agent-permissions`
5. Click **Create policy**

### 2d. Create an Instance Profile for the Agent

> An instance profile is how you attach an IAM role to an EC2 server. The console creates one automatically when you create a role with EC2 trust — so the profile `homeostat-agent` should already exist. Verify:

1. Still on the `homeostat-agent` role page
2. Look at the **Summary** section at the top
3. You should see **Instance profile ARN** — if it's there, you're good

---

## Step 3: Create the Watchdog IAM Role

> The watchdog is the safety supervisor. It has very few permissions, but the critical one: it can shut down the agent.

1. Go to **IAM → Roles**
2. Click **Create role**
3. Trusted entity type: **AWS service**
4. Use case: **EC2** → click **Next**
5. **Don't attach any policies** → click **Next**
6. Role name: `homeostat-watchdog`
7. Click **Create role**
8. Click on the role `homeostat-watchdog`
9. Click **Add permissions → Create inline policy**
10. Click **JSON** tab, paste:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "KillSwitch",
      "Effect": "Allow",
      "Action": [
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy"
      ],
      "Resource": "arn:aws:iam::*:role/homeostat-agent"
    },
    {
      "Sid": "ReadCosts",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "budgets:ViewBudget",
        "budgets:DescribeBudget"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadMetrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "cloudwatch:DescribeAlarms",
        "logs:GetLogEvents",
        "logs:FilterLogEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SendAlerts",
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:us-east-1:*:homeostat-alerts"
    }
  ]
}
```

11. Policy name: `homeostat-watchdog-permissions`
12. Click **Create policy**

> **Note:** The `KillSwitch` statement lets the watchdog attach a "deny everything" policy to the agent role. That's how it shuts the agent down — it doesn't delete the role, it just adds a blanket deny on top.

---

## Step 4: Create the SNS Topic (Email Alerts)

1. Go to **SNS** (search "SNS" in the top bar)
2. Click **Topics** in the left sidebar → **Create topic**
3. Type: **Standard**
4. Name: `homeostat-alerts`
5. Click **Create topic**
6. On the topic page, click **Create subscription**
7. Protocol: **Email**
8. Endpoint: **your email address**
9. Click **Create subscription**
10. **Check your email** — click the confirmation link AWS sends you

---

## Step 5: Create the Budget

1. Go to **AWS Budgets** (search "Budgets" in the top bar)
2. Click **Create a budget**
3. Choose **Customize (advanced)**
4. Budget type: **Cost budget** → click **Next**
5. Budget name: `homeostat-monthly`
6. Period: **Monthly**, Budget amount: **Fixed**, Amount: **100** (USD)
7. Click **Next**
8. Add alert thresholds:
   - **Alert 1:** Actual cost > **50%** → notify your email
   - **Alert 2:** Actual cost > **80%** → notify your email
   - **Alert 3:** Actual cost > **100%** → notify your email
9. Click **Next** → **Create budget**

---

## Step 6: Enable Bedrock Model Access

1. Go to **Amazon Bedrock** (search "Bedrock" in the top bar)
2. Make sure you're in **us-east-1** (top-right dropdown)
3. In the left sidebar, scroll down to **Model access**
4. Click **Manage model access**
5. Find and enable:
   - ✅ **Claude 3 Haiku** (by Anthropic) — cheap, fast, used for both reasoning and quick checks
6. Click **Save changes**
7. Wait for status to show **Access granted** (usually instant)

---

## Step 7: Verify Everything

Run these in your terminal after setting up AWS CLI:

```bash
# Check you're logged in
aws sts get-caller-identity

# Check agent role exists
aws iam get-role --role-name homeostat-agent --query "Role.RoleName"

# Check watchdog role exists
aws iam get-role --role-name homeostat-watchdog --query "Role.RoleName"

# Check agent has the permission boundary
aws iam get-role --role-name homeostat-agent --query "Role.PermissionsBoundary.PermissionsBoundaryArn"

# Check SNS topic exists
aws sns list-topics --query "Topics[?contains(TopicArn, 'homeostat-alerts')]"

# Check budget exists
aws budgets describe-budgets --account-id $(aws sts get-caller-identity --query Account --output text) --query "Budgets[?BudgetName=='homeostat-monthly'].BudgetName"

# Test that the agent CANNOT modify IAM (should show "implicitDeny")
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/homeostat-agent \
  --action-names iam:PutRolePolicy \
  --query "EvaluationResults[0].EvalDecision"
```

All checks should return valid values. The last one should say `implicitDeny`. ✅

---

# Part 2 — Terraform Alternative

> If you prefer infrastructure-as-code, or you want to tear down and recreate everything reliably, use the Terraform files instead of the manual steps above. **Don't do both** — pick one.

### Prerequisites

- Install Terraform: https://developer.hashicorp.com/terraform/install
- AWS CLI configured (Step 1 above — you still need the admin user)

### Create the Terraform Backend First

Terraform needs somewhere to store its state. Run these once:

```bash
# Get your account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Create S3 bucket for state
aws s3 mb s3://homeostat-terraform-state-$ACCOUNT_ID --region us-east-1

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket homeostat-terraform-state-$ACCOUNT_ID \
  --versioning-configuration Status=Enabled

# Create DynamoDB table for state locking
aws dynamodb create-table \
  --table-name homeostat-terraform-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

### Then edit `terraform/00-bootstrap/main.tf`

Uncomment the backend block and replace `YOUR_ACCOUNT_ID` with your actual account ID.

### Run Terraform

```bash
cd terraform/00-bootstrap
terraform init
terraform plan       # review what it will create
terraform apply      # type "yes" to create everything
```

Terraform will ask for your email address (for alerts). It creates exactly the same resources as the manual steps above.

### Terraform Files

| File | What it does |
|---|---|
| `main.tf` | Creates both IAM roles, permission boundary, budget, SNS topic |
| `variables.tf` | Config: region, email, budget amount |
| `outputs.tf` | Prints role ARNs after creation |

---

# Summary: What You Have Now

After completing either Part 1 or Part 2:

| Resource | Name | Purpose |
|---|---|---|
| Admin user | `homeostat-admin` | You use this to manage everything |
| Agent role | `homeostat-agent` | The AI agent's identity — can manage infra, can't touch IAM |
| Permission boundary | `homeostat-agent-boundary` | Hard block — agent can never escape its limits |
| Watchdog role | `homeostat-watchdog` | Safety supervisor — can kill the agent |
| SNS topic | `homeostat-alerts` | Sends you email when things go wrong |
| Budget | `homeostat-monthly` | Alerts at 50%, 80%, 100% of $100/mo |
| Bedrock access | Claude 3 Haiku | The LLM the agent will use |

**Next step:** Step 0.2 — Repository & Dev Tooling Setup
