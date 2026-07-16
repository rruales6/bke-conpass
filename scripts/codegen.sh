#!/usr/bin/env bash
# Regenerate all contract-derived artifacts. Run after editing contracts/openapi.yaml.
# NEVER hand-edit generated files.
set -euo pipefail
cd "$(dirname "$0")/.."

SPEC="contracts/openapi.yaml"

echo "› validating OpenAPI spec"
python -c "from openapi_spec_validator import validate; import yaml; validate(yaml.safe_load(open('$SPEC'))); print('  spec valid')"

echo "› generating backend Pydantic models"
datamodel-codegen \
  --input "$SPEC" --input-file-type openapi \
  --output layers/common/python/conpass_common/models.py \
  --output-model-type pydantic_v2.BaseModel \
  --use-standard-collections --use-schema-description \
  --field-constraints --use-annotated --target-python-version 3.12 \
  --use-double-quotes
echo "  wrote layers/common/python/conpass_common/models.py"

# Frontend typed client (requires Node; run from repo root network-permitting):
#   npx openapi-typescript backend/contracts/openapi.yaml \
#     -o frontend/packages/api-client/src/schema.ts
echo "› (frontend) run: npx openapi-typescript $SPEC -o ../frontend/packages/api-client/src/schema.ts"
echo "done."
