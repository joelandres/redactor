#!/usr/bin/env bash
# Provision Azure resources for the Document Redactor (local-dev permissions for the signed-in user).
#
#   az login
#   ./infra/setup.sh                       # uses defaults below
#   RG=my-rg LOC=westus2 ./infra/setup.sh  # override
#
# Creates: resource group, Language resource, Document Intelligence resource, and your role
#          assignments. No storage: documents are only held in the app's memory.
set -euo pipefail

RG="${RG:-rg-redactor}"
LOC="${LOC:-eastus2}"
SUFFIX="${SUFFIX:-$(openssl rand -hex 3)}"
LANG_RES="${LANG_RES:-lang-redactor-${SUFFIX}}"
DI="${DI:-di-redactor-${SUFFIX}}"

ME=$(az ad signed-in-user show --query id -o tsv)

echo "==> Resource group $RG ($LOC)"
az group create -n "$RG" -l "$LOC" -o none

echo "==> Language resource $LANG_RES"
az cognitiveservices account create -n "$LANG_RES" -g "$RG" -l "$LOC" \
  --kind TextAnalytics --sku S --custom-domain "$LANG_RES" --yes -o none
LANG_ID=$(az cognitiveservices account show -n "$LANG_RES" -g "$RG" --query id -o tsv)

echo "==> Document Intelligence resource $DI"
az cognitiveservices account create -n "$DI" -g "$RG" -l "$LOC" \
  --kind FormRecognizer --sku S0 --custom-domain "$DI" --yes -o none
DI_ID=$(az cognitiveservices account show -n "$DI" -g "$RG" --query id -o tsv)

echo "==> Role assignments"
assign() { az role assignment create --assignee-object-id "$1" --assignee-principal-type "$2" \
             --role "$3" --scope "$4" -o none 2>/dev/null || echo "   (exists or failed: $3)"; }
# You (local development): Language, Document Intelligence
assign "$ME" User "Cognitive Services Language Owner" "$LANG_ID"
assign "$ME" User "Cognitive Services User" "$DI_ID"

echo "==> Waiting for role assignments to propagate (60s)"
sleep 60

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  cp "$ENV_FILE" "$ENV_FILE.bak"
  echo "   (existing .env backed up to .env.bak)"
fi
cat > "$ENV_FILE" <<EOF
LANGUAGE_ENDPOINT=https://$LANG_RES.cognitiveservices.azure.com
DOCINTEL_ENDPOINT=https://$DI.cognitiveservices.azure.com
EOF

echo
echo "Done. Wrote $ENV_FILE:"
cat "$ENV_FILE"
