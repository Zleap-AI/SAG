#!/bin/sh
# Build a disposable web tree with the cloud overlay; never overwrite stock sources.
set -eu
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 EMPTY_BUILD_DIRECTORY" >&2
  exit 1
fi
cloud_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repository_dir=$(CDPATH= cd -- "$cloud_dir/.." && pwd)
build_dir=$1
if [ -e "$build_dir" ]; then
  echo "Build directory must not already exist." >&2
  exit 1
fi
mkdir -p "$build_dir"
tar -C "$repository_dir/apps/web" --exclude=node_modules --exclude=.next --exclude=.git --exclude=tsconfig.tsbuildinfo -cf - . | tar -C "$build_dir" -xf -
python3 "$cloud_dir/chatbot/web/apply_overlay.py" "$build_dir"
