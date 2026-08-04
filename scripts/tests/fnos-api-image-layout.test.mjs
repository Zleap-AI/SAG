import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repoRoot = fileURLToPath(new URL("../..", import.meta.url));
const dockerfile = path.join(repoRoot, "apps/api/Dockerfile");

test("API production image keeps compiler toolchains out of the runtime stage", async () => {
  const source = await readFile(dockerfile, "utf8");
  const stages = source.split(/^FROM python:3\.12-slim AS runtime$/m);

  assert.equal(stages.length, 2, "Dockerfile must define a dedicated runtime stage");
  assert.match(stages[0], /build-essential/);
  assert.doesNotMatch(stages[1], /build-essential/);
  assert.match(stages[1], /COPY --from=builder \/install \/usr\/local/);
  assert.match(
    stages[0],
    /pip install --prefix=\/install --ignore-installed packaging -r \/tmp\/requirements\.txt/,
    "runtime-only dependency packaging must be installed into the copied prefix",
  );
  assert.match(
    stages[0],
    /ARG PIP_INDEX_URL=""/,
    "the build must allow a temporary package-index override without changing runtime configuration",
  );
});
