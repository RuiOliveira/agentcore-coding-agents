#!/usr/bin/env bash
# Write coding-agents/infra.config in a fresh clone: the IDs of the network and the
# shared storage your agents' Runtimes attach to.
#
#   coding-agents/infra/setup.sh            # region: $AWS_REGION, then your AWS config
#
# The workshop template creates a VPC with two private subnets, a security group, an
# S3 bucket with an S3 Files file system and access point, and the role S3 Files uses,
# and publishes their IDs as SSM parameters under /workshop/agent/. This script reads
# them and records them in infra.config, the file setup.sh, deploy.py, fanout.py and
# progress.py read. It creates nothing and changes nothing in AWS: it only records
# what exists. The same template in your own account publishes the same parameters.
#
# Any value can be supplied instead (for your own VPC or storage), for example:
#   INFRA_SUBNET_1=subnet-... INFRA_S3FILES_AP_ARN=arn:aws:s3files:... coding-agents/infra/setup.sh
set -euo pipefail

REGION="${1:-${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [ -z "$REGION" ]; then
  echo "ERROR: no region. Pass one (coding-agents/infra/setup.sh us-west-2) or set AWS_REGION." >&2
  exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

param() {
  aws ssm get-parameter --name "$1" --region "$REGION" \
    --query Parameter.Value --output text 2>/dev/null || true
}

# The shared storage: one JSON parameter with the bucket, file system and access point.
storage="$(param /workshop/agent/shared-workspace)"
field() { [ -n "$storage" ] && printf '%s' "$storage" | jq -r --arg k "$1" '.[$k] // empty' || true; }
INFRA_BUCKET="${INFRA_BUCKET:-$(field bucket)}"
INFRA_S3FILES_FS_ID="${INFRA_S3FILES_FS_ID:-$(field file_system_id)}"
INFRA_S3FILES_AP_ID="${INFRA_S3FILES_AP_ID:-$(field access_point_id)}"
INFRA_S3FILES_AP_ARN="${INFRA_S3FILES_AP_ARN:-$(field access_point_arn)}"
# The network and the S3 Files role.
INFRA_VPC_ID="${INFRA_VPC_ID:-$(param /workshop/agent/vpc-id)}"
INFRA_SUBNET_1="${INFRA_SUBNET_1:-$(param /workshop/agent/private-subnet-1)}"
INFRA_SUBNET_2="${INFRA_SUBNET_2:-$(param /workshop/agent/private-subnet-2)}"
INFRA_SECURITY_GROUP="${INFRA_SECURITY_GROUP:-$(param /workshop/agent/security-group-id)}"
INFRA_S3FILES_ROLE_ARN="${INFRA_S3FILES_ROLE_ARN:-$(param /workshop/agent/s3files-role-arn)}"

missing=""
for var in INFRA_BUCKET INFRA_S3FILES_FS_ID INFRA_S3FILES_AP_ID INFRA_S3FILES_AP_ARN \
           INFRA_VPC_ID INFRA_SUBNET_1 INFRA_SUBNET_2 INFRA_SECURITY_GROUP INFRA_S3FILES_ROLE_ARN; do
  [ -n "${!var:-}" ] || missing="${missing} ${var}"
done
if [ -n "$missing" ]; then
  echo "ERROR: not found in $REGION:${missing}" >&2
  echo "  These come from the workshop stack's /workshop/agent/ parameters. Is the stack" >&2
  echo "  finished, and is this its region? (aws ssm get-parameters-by-path --path /workshop/agent)" >&2
  exit 1
fi
# A mount in one region and a Runtime in another fails later as "an agent that wrote
# nothing", so refuse it here, while the fix is still one argument.
ap_region_account="$(printf '%s' "$INFRA_S3FILES_AP_ARN" | cut -d: -f4-5)"
if [ "$ap_region_account" != "$REGION:$ACCOUNT_ID" ]; then
  echo "ERROR: the access point is in $ap_region_account, not $REGION:$ACCOUNT_ID" >&2
  exit 1
fi

# Same keys, same order as the workshop host writes them.
target="${SCRIPT_DIR}/../infra.config"
cat > "${target}.tmp" <<EOF
INFRA_REGION=${REGION}
INFRA_ACCOUNT_ID=${ACCOUNT_ID}
INFRA_BUCKET=${INFRA_BUCKET}
INFRA_S3FILES_FS_ID=${INFRA_S3FILES_FS_ID}
INFRA_S3FILES_AP_ID=${INFRA_S3FILES_AP_ID}
INFRA_S3FILES_AP_ARN=${INFRA_S3FILES_AP_ARN}
INFRA_VPC_ID=${INFRA_VPC_ID}
INFRA_SUBNET_1=${INFRA_SUBNET_1}
INFRA_SUBNET_2=${INFRA_SUBNET_2}
INFRA_SECURITY_GROUP=${INFRA_SECURITY_GROUP}
INFRA_S3FILES_ROLE_ARN=${INFRA_S3FILES_ROLE_ARN}
EOF
mv "${target}.tmp" "$target"

echo "Wrote coding-agents/infra.config for $REGION:"
echo "  bucket          $INFRA_BUCKET"
echo "  S3 Files        $INFRA_S3FILES_FS_ID, access point $INFRA_S3FILES_AP_ID"
echo "  VPC             $INFRA_VPC_ID, subnets $INFRA_SUBNET_1 $INFRA_SUBNET_2"
echo "  security group  $INFRA_SECURITY_GROUP"
