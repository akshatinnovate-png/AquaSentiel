#!/usr/bin/env bash
# Provision one AquaSentinel node in AWS IoT Core.
#
# Creates a thing, issues it its own X.509 certificate, attaches the device
# policy the stack created, and writes a .env.iot you can source before running
# the simulator against the real cloud path.
#
# Per-device certificates rather than a shared key: if one node is lost or
# tampered with you revoke that certificate, and no other node is affected.
#
#   ./scripts/provision_device.sh aquasentinel-node-01
set -euo pipefail

THING_NAME="${1:-aquasentinel-node-01}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"
POLICY_NAME="${IOT_POLICY_NAME:-aquasentinel-device-${REGION}}"
CERT_DIR="${CERT_DIR:-./certs/${THING_NAME}}"

command -v aws >/dev/null || { echo "the AWS CLI is required" >&2; exit 1; }

echo "==> region=${REGION} thing=${THING_NAME} policy=${POLICY_NAME}"
mkdir -p "${CERT_DIR}"

if ! aws iot describe-thing --thing-name "${THING_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  aws iot create-thing --thing-name "${THING_NAME}" --region "${REGION}" >/dev/null
  echo "==> created thing"
else
  echo "==> thing already exists"
fi

if [ -f "${CERT_DIR}/certificate.pem" ]; then
  echo "==> certificate already present in ${CERT_DIR}; not re-issuing"
  CERT_ARN="$(cat "${CERT_DIR}/certificate.arn")"
else
  CERT_ARN="$(aws iot create-keys-and-certificate \
    --set-as-active --region "${REGION}" \
    --certificate-pem-outfile "${CERT_DIR}/certificate.pem" \
    --private-key-outfile "${CERT_DIR}/private.key" \
    --public-key-outfile "${CERT_DIR}/public.key" \
    --query certificateArn --output text)"
  echo "${CERT_ARN}" > "${CERT_DIR}/certificate.arn"
  chmod 600 "${CERT_DIR}/private.key"
  echo "==> issued certificate ${CERT_ARN}"
fi

aws iot attach-policy --policy-name "${POLICY_NAME}" --target "${CERT_ARN}" --region "${REGION}" \
  || echo "!! could not attach ${POLICY_NAME} -- deploy the stack first (cdk deploy)" >&2
aws iot attach-thing-principal --thing-name "${THING_NAME}" --principal "${CERT_ARN}" \
  --region "${REGION}" 2>/dev/null || true

# Amazon's root CA, needed by the device to verify the broker.
if [ ! -f "${CERT_DIR}/AmazonRootCA1.pem" ]; then
  curl -fsSL -o "${CERT_DIR}/AmazonRootCA1.pem" https://www.amazontrust.com/repository/AmazonRootCA1.pem
  echo "==> downloaded Amazon root CA"
fi

ENDPOINT="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "${REGION}" \
  --query endpointAddress --output text)"

cat > .env.iot <<ENVEOF
# Source this, then: python -m sim.run --transport iot --scenario demo --speed 60
export AWS_IOT_ENDPOINT="${ENDPOINT}"
export AWS_IOT_CERT="${CERT_DIR}/certificate.pem"
export AWS_IOT_KEY="${CERT_DIR}/private.key"
export AWS_IOT_CA="${CERT_DIR}/AmazonRootCA1.pem"
export AWS_IOT_THING="${THING_NAME}"
ENVEOF

echo
echo "==> done. Next:"
echo "    pip install awsiotsdk"
echo "    source .env.iot"
echo "    python -m sim.run --transport iot --scenario demo --speed 60"
echo
echo "    Watch it arrive:  AWS console -> IoT Core -> MQTT test client"
echo "                      subscribe to  aquasentinel/telemetry/#"
