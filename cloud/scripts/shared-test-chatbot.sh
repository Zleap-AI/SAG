#!/bin/sh
# Scope: Standalone mounted image verification, isolated database and fake transports.
set -eu
cloud_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repository_dir=$(CDPATH= cd -- "$cloud_dir/.." && pwd)
image_name=${SAG_CHATBOT_API_IMAGE:-sag-api:latest}
test_cache="$cloud_dir/chatbot/.test-deps"
mkdir -p "$test_cache"
docker run --rm --entrypoint python \
  -v "$cloud_dir/chatbot:/extension:ro" -v "$test_cache:/dependencies" \
  "$image_name" -m pip install --quiet --upgrade --target /dependencies -r /extension/requirements-test.txt
docker run --rm --entrypoint python --workdir /upstream \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e LITELLM_LOCAL_MODEL_COST_MAP=True \
  -e SAG_TEST_STOCK_WEB=/web-stock \
  -e PYTHONPATH=/dependencies:/extension:/upstream \
  -v "$cloud_dir/chatbot:/extension:ro" \
  -v "$repository_dir/apps/web:/web-stock:ro" \
  -v "$test_cache:/dependencies:ro" -v "$repository_dir/apps/api:/upstream:ro" \
  "$image_name" -m pytest -p no:cacheprovider /extension/tests "$@"
