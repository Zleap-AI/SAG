# @zleap-ai/dsh-sag

dsh-sag lets DeepSeek Harness use your SAG knowledge base for search, reading, upload, text ingestion, and document management.

## Compatibility

- This source package: `0.2.0`; DeepSeek Harness: `0.2.0-rc.2`.
- Node.js: `^22.19.0` or `>=24.0.0`; Node.js 24 is recommended. Both `dsh` and `pnpm` must be on PATH.
- SAG must include the Connect dsh setting and local connector API.
- Embedded engine: `zleap-sag==0.14.0`. See [advanced configuration](docs/embedded.md).

The previously published `0.1.1` plugin is for older dsh hosts and cannot be installed on dsh 0.2. Other host versions have not been verified.

## Quick start

Start SAG. For a fresh dsh installation, run `npm install --global @deepseek-ai/dsh@0.2.0-rc.2`.

### Install from npm

Install the plugin from npm. The following commands apply after `0.2.0` is published to npm.

```sh
dsh plugin --profile web add @zleap-ai/dsh-sag@0.2.0
dsh plugin --profile web exec dsh-sag doctor
dsh --profile web
```

When `doctor` reports that SAG is connected, start or restart Web. Local discovery usually connects automatically, without Python or manual credentials.

## Connect SAG

If discovery fails, save a connection using one of these routes:

```sh
# Discover a running local SAG automatically
dsh plugin --profile web exec dsh-sag setup

# Use a connection file exported from SAG's Connect dsh settings
dsh plugin --profile web exec dsh-sag setup ./sag-dsh.json

# Specify the local SAG address
dsh plugin --profile web exec dsh-sag setup --url http://127.0.0.1:8000
```

For the exported-file option, run the command in that file's directory or replace `./sag-dsh.json` with its actual path. Run `dsh plugin --profile web exec dsh-sag doctor`, then restart dsh. Use the same profile and `DSH_HOME` for every command. Replace `web` consistently for a custom profile.

## Ask dsh naturally

- “Search SAG for the upload limit and cite the original text.”
- “Upload `/Users/me/Documents/product-manual.pdf` to SAG, then summarize it after processing finishes.”
- “Save the following meeting decisions as a note in SAG: …”

SAG advertises its available operations; missing capabilities are not called. Document deletion requires user approval.

## Source installation for development

To develop or debug plugin changes, build a local package from the root of your SAG checkout.

```sh
cd integrations/dsh-sag
pnpm install --frozen-lockfile
pnpm run pack
dsh plugin --profile web add ./artifacts/zleap-ai-dsh-sag-0.2.0.tgz
```

Then run `dsh plugin --profile web exec dsh-sag doctor` and start or restart that profile.

## Upgrade and recovery

After upgrading the host, explicitly install the matching plugin. An unversioned `update` can retain an exact old version. After installing `0.2.0`, run `setup` and `doctor` again; credentials remain in dsh Credentials.

If a plugin prevents Web startup, remove it first:

```sh
dsh plugin --profile web remove @zleap-ai/dsh-sag
dsh --profile web
```

Removing the plugin does not delete SAG documents. Preserve the rest of your dsh configuration. If you manually added Cordis entries, remove only the plugin's own entries.

Source and independent releases live in the [SAG repository](https://github.com/Zleap-AI/SAG/tree/main/integrations/dsh-sag). To manage the Python engine directly, see [embedded mode](docs/embedded.md).
