# Retained-data fallback

The uninstall wizard defaults to retaining `${TRIM_PKGVAR}/data`. Final fnOS
cleanup behavior for application-private runtime directories still requires
verification on the target x86 device.

Until that device gate is complete, use this deterministic fallback before
uninstalling:

1. Check the timestamp of the newest full archive in
   `${TRIM_PKGVAR}/backup/` against the most recent SAG write. If no archive
   exists, or the archive predates any write, do not reuse it.
2. Stop SAG and create a fresh cold full-data archive with the packaged
   lifecycle backup procedure (`cmd/upgrade_init`) in the fnOS-provided TRIM
   callback environment. A successful run leaves the old application stopped
   and creates a mode-0600 `sag-data-*.tar.gz` atomically. Do not continue if
   the callback reports an error.
3. Verify the fresh archive with `tar -tzf <archive>` and confirm it contains
   the complete top-level `data/` tree, then record its SHA-256.
4. Copy that verified fresh archive to a user-created shared folder outside the
   application package tree and verify the copied file's SHA-256 matches.
5. Leave **Retain data** selected in the uninstall wizard.
6. Do not delete the external archive until SAG has been reinstalled and its
   complete `/data` tree has been restored and verified.

Selecting **Permanently delete active data** is the only path that authorizes
`cmd/uninstall_callback` to remove `${TRIM_PKGVAR}/data`.
