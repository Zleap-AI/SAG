from pathlib import Path

from sag_api.sag.octx_vector_manifest import VectorExportManifest


def test_vector_export_manifest_supports_batched_disk_backed_lookup(tmp_path: Path) -> None:
    path = tmp_path / "vectors.sqlite3"

    with VectorExportManifest(path) as manifest:
        manifest.add("entity.name", "entity-b", "octx-b", "b" * 64)
        manifest.add("entity.name", "entity-a", "octx-a", "a" * 64)
        manifest.add("event.title", "event-a", "octx-event", "c" * 64)

        assert manifest.count("entity.name") == 2
        assert manifest.lookup("entity.name", ["missing", "entity-a", "entity-b"]) == {
            "entity-a": ("octx-a", "a" * 64),
            "entity-b": ("octx-b", "b" * 64),
        }
        assert list(manifest.iter_batches("entity.name", batch_size=1)) == [
            [("entity-a", "octx-a", "a" * 64)],
            [("entity-b", "octx-b", "b" * 64)],
        ]

    assert path.exists()
