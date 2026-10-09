# Independent dsh-sag releases

`@zleap-ai/dsh-sag` is maintained in SAG but keeps its own npm version. Plugin `0.2.0` targets dsh `0.2.0-rc.2` and embeds zleap-sag `0.14.0`; SAG's Web/API/Desktop version is independent.

## Verify and pack

Use Node.js 24, pnpm 11.7.0, uv and an exact dsh 0.2.0-rc.2 installation. From `integrations/dsh-sag/`:

```sh
pnpm install --frozen-lockfile
pnpm run lint
pnpm run typecheck
pnpm run test
pnpm run test:python
pnpm run test:release
pnpm run check:release
pnpm run check:pack
pnpm run pack
```

`pack` builds the standalone CLI, regenerates notices, synchronizes the Python runtime and writes `artifacts/zleap-ai-dsh-sag-0.2.0.tgz`. `check:pack` installs the actual tarball into a clean profile and verifies CLI behavior, Web startup/shutdown, invalid configuration rejection and shared Cordis identity. Set `DSH_BIN` to the exact host executable being tested. These checks do not by themselves validate a real SAG deployment or external model quality.

The integration CI also runs the real SAG API and published engine with temporary storage, dynamic loopback ports and deterministic local model responses:

```sh
uv sync --project ../../apps/api --frozen --extra dev
pnpm run build
node scripts/run-live-sag.mjs
```

This checks all 11 registered tools, upload/processing/search/read, profile persistence and API restart. The printed evidence directory contains the result and process logs. It does not assess external model quality.

For later releases, update the independent package version in `packages/dsh-sag/package.json`, the integration workspace version/lockfile and user-facing versioned examples. Add package release notes. Do not change SAG application versions merely to publish the plugin.

Before publishing, confirm the target npm version is unused:

```sh
npm view @zleap-ai/dsh-sag@0.2.0 version
```

A not-found response means that version is available; an authentication or network failure does not. npm package versions are immutable.

## Release from GitHub Actions

The independent workflow is `.github/workflows/dsh-sag-release.yml`. It runs the plugin checks, requires a matching plugin tag on SAG main, packs the package and publishes with npm OIDC provenance.

Configure a trusted publisher for `@zleap-ai/dsh-sag` on npm before using this workflow:

- Organization: `Zleap-AI`
- Repository: `SAG`
- Workflow filename: `dsh-sag-release.yml`
- Environment: `dsh-sag-npm`
- Allow direct `npm publish`.

Trusted publishing requires npm CLI 11.5.1+ and Node.js 22.14.0+; the workflow uses Node.js 24. See [npm's trusted publishing instructions](https://docs.npmjs.com/trusted-publishers/). This local change does not configure npm account permissions.

After the migration is merged and its checks pass, create the plugin's tag:

```sh
git tag -a dsh-sag-v0.2.0 -m 'dsh-sag 0.2.0'
git push origin dsh-sag-v0.2.0
```

Use `dsh-sag-v<version>`, never `v<version>` for this plugin. SAG's `v*.*.*` tags trigger desktop releases. The plugin workflow currently publishes stable npm versions only; it rejects prerelease package versions instead of assigning them to `latest` accidentally.

## Manual publication

If publishing locally instead, authenticate with npm and publish the verified archive:

```sh
npm whoami
npm publish ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz --access public
```

Then verify the registry package from a clean profile using the npm installation commands in the user guide. Record the published package version, exact dsh version and business-path verification separately.

The original dsh-sag repository remains untouched during this migration. Its later deprecation is a separate step once the SAG integration is adopted.
