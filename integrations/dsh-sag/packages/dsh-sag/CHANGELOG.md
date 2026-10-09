# Changelog

## 0.2.0

- Move the complete plugin project into SAG under `integrations/dsh-sag/`, preserving independent npm delivery.
- Adapt settings and CLI persistence to DeepSeek Harness 0.2.0-rc.2 profiles. Older hosts should keep plugin 0.1.1.
- Upgrade the embedded engine to zleap-sag 0.14.0 and document its new embedding dimension configuration.
- Add independent CI/release tags and source-tarball installation instructions.

## 0.1.1 - 2026-09-11

- Keep settings namespace branding type-only so dsh host helper-export changes cannot prevent plugin startup.
- Validate the packaged bundle against the dsh host selected by `DSH_BIN` instead of a fixed historical version.

## 0.1.0 - 2026-08-29

- Validate the published bundle against the npm `latest` DeepSeek Harness `0.1.1-rc.2` release.
- Publish dsh-sag as an installable DeepSeek Harness profile bundle.
- Discover and configure a running local SAG through `setup` and `doctor`.
- Expose search, reading, knowledge-source, upload, ingestion, and document-management tools.
- Store connection metadata and credentials through dsh host services.
- Keep the Python sidecar available as an explicit advanced embedded mode.
