# Apple credential handoff to SAG-Wiki

This bootstrap is prepared for review. Preparing or merging it does not run the
handoff. The security migration needs explicit user confirmation before a manual
run or any destination secret writes. It does not build an application, bump a
version, create a tag, or publish a release.

The source is `Zleap-AI/SAG`, environment `desktop-release`. GitHub does not return
stored secret plaintext through its API. The workflow therefore seals each of
the following existing values inside the source runner using the destination
environment's public key:

- `APPLE_CERTIFICATE_BASE64`
- `APPLE_CERTIFICATE_PASSWORD`
- `APPLE_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`
- `APPLE_TEAM_ID`

The reviewed target in `target.json` is `Zleap-AI/SAG-Wiki`, environment `release`,
key ID `3380204578043523366`. Its public key is public metadata. Both the manifest
and the CLI's approved target must change through review if GitHub rotates that
key. The CLI takes no destination, key, output, or other arguments and loads only
the adjacent manifest. There are no workflow inputs that could redirect secrets
to another public key.

## Execution boundaries

Pull requests changing this workflow or its bootstrap directory run tests with
disposable credentials and temporary key pairs. These tests do not select the
source environment or access Apple secrets.

The sealing job only runs on an explicit `workflow_dispatch` in `Zleap-AI/SAG`
from `refs/heads/main`, after the tests pass. The CLI independently validates the
event, repository, workflow path, ref, full commit SHA, and positive numeric run
ID and attempt. The `desktop-release` environment's existing protection policies
still apply. Although that environment also permits release tags, this workflow
rejects tags.

All Actions use the commit pins already used by SAG. The only npm dependency is
exactly pinned `libsodium-wrappers` and its transitive dependency is locked with
registry integrity hashes. Each job runs `npm ci --ignore-scripts`; the source
secrets are scoped exclusively to the subsequent `node encrypt.mjs` step.
Checkout credentials are not persisted. GitHub token permissions are limited to
`contents: read` and `actions: read`; uploading the artifact uses the Actions
artifact service. The workflow has no destination write credentials or API write
step.

Each value must be a nonempty string and is sealed separately using
`crypto_box_seal`. UTF-8 bytes, spaces, line endings, and base64 certificate text
are preserved. The certificate text is not decoded. No plaintext is written to
disk. Success logs list only the credential names and destination; failure logs
use a generic message without dependency details or values.

The only output file is `apple-secrets-for-sag-wiki.json`, created exclusively
with owner read/write permissions (`0600`). An existing file or symlink causes
failure. Only that sealed JSON file is uploaded as
`apple-secrets-for-sag-wiki-<run_id>-<run_attempt>`, with one-day retention.

Its JSON envelope is:

```text
{
  schema: 1,
  target: { repository, environment, key_id, public_key },
  source: { repository, workflow, commit, run_id, run_attempt, ref },
  credentials: { <each of the five names>: <base64 sealed value> }
}
```

## Approved migration procedure

After explicit user confirmation, review and merge this bootstrap, then manually
run the workflow on SAG `main`. Verify the actual GitHub run, reviewed source
commit, repository, workflow, ref, run ID, attempt, and artifact identity before
using the artifact. The source metadata in the JSON is a cross-check; sealed
boxes do not authenticate the sender, so the JSON alone is not provenance.

Independently re-fetch the destination environment's public key and require an
exact match with the reviewed target and artifact. If the key changed, stop and
review a new target manifest. An authorized operator can then submit each
`credentials[name]` unchanged as GitHub's `encrypted_value`, with the fixed
`key_id`, to that destination environment's secret API. This bootstrap deliberately
does not perform that write. Verify the five destination secret names through
metadata after applying, and delete the handoff artifact when it is no longer
needed. Do not print its ciphertext or secret values.

This process reuses SAG's current company signing identity. It does not establish
that the Apple certificate is current, that the five values form a working set,
or that a SAG-Wiki application has been signed or notarized. Those require a
separately authorized signing validation. Version PRs, version bumps, tags, and
releases still require user confirmation.

## Local verification with disposable fixtures

From this directory, run:

```sh
npm ci --ignore-scripts
npm test
```

Tests exercise the CLI in temporary directories with fixture values, decrypt
sealed values with disposable test keys, reject changed or malformed destinations
and source contexts, require all five values, verify no plaintext in the output
or logs, reject command-line overrides, preserve existing files, and verify the
output's `0600` mode. The in-memory sealing function accepts a disposable public
key for round-trip tests; the CLI always enforces the fixed approved manifest.
No test reads real source secrets or calls GitHub's secret-write API.

The sealing format follows [GitHub's secret encryption documentation](https://docs.github.com/en/rest/guides/encrypting-secrets-for-the-rest-api)
and [libsodium sealed boxes](https://doc.libsodium.org/public-key_cryptography/sealed_boxes).
