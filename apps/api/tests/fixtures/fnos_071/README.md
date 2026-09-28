# Native 0.7.1 migration fixture

`native-071.tar.gz` was generated with SAG `8ad465446d89067337b7406bff09fe44438f2059`
and its locked `zleap-sag==0.7.1` environment. It contains two synthetic tenants
(numeric UID and UID/name/hash), old metadata SQLite, populated engine SQLite and
LanceDB, private Markdown/XLSX originals, missing originals, queued jobs, settings,
an agent, and a conversation with internal/external citations. It contains no user
data or usable credentials.

Only embedding and model responses are deterministic: old ingestion, parsing,
event/entity persistence and LanceDB writes run through 0.7.1. These are generated
old-version stores, not samples collected from a physical fnOS device.
`provenance.json` records the source version and every uncompressed file hash.
Tests relocate only the private root stored in document paths. The old engine is
never opened by 0.13.0, and its hashes must remain unchanged.

To regenerate, export that pinned commit's `apps/api` to a temporary directory,
run `uv sync --extra dev` there, then run this directory's `generate.py` using the
old environment's Python with `PYTHONPATH` set to the old `apps/api`:

```sh
<old-python> generate.py --root <new-empty-directory> --output <this-directory>
```

The ordinary regression suite consumes the saved fixture; it does not install
0.7.1, call external model services, or require a fnOS device.
