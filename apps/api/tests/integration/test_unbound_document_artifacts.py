"""SIGKILL leaves owned artifacts discoverable without a committed Job binding."""

import subprocess
import sys
from uuid import uuid4

import pytest

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.db.models import Job
from sag_api.jobs.tasks import _delete_document_task_unlocked
from sag_api.parsing.service import remove_parsed_sidecars
from tests.document_requests import deletion_receipt
from tests.test_document_public_deletion import deleting_document as deleting_document


async def test_delete_cleans_snapshot_killed_before_job_binding_and_keeps_peer(
    deleting_document, tmp_path, monkeypatch,
):
    source_id, document_id, job_id, _range_id, raw, _candidate = deleting_document
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(settings, "upload_dir", str(upload_root))
    peer_root = upload_root / source_id / ".document-candidates" / "peer-document"
    peer_root.mkdir(parents=True)
    peer_file = peer_root / "peer.md"
    peer_file.write_text("peer publication", encoding="utf-8")
    worker = """
import os, signal, sys
from types import SimpleNamespace
from sag_api.core.config import settings
from sag_api.services.document_service import _snapshot_document_input
settings.upload_dir = sys.argv[1]
document = SimpleNamespace(source_id=sys.argv[2], id=sys.argv[3], filename='published.md', storage_path=sys.argv[4])
_snapshot_document_input(document, sys.argv[5])
os.kill(os.getpid(), signal.SIGKILL)
"""
    killed = subprocess.run(
        [sys.executable, "-c", worker, str(upload_root), source_id, document_id, str(raw), uuid4().hex],
        capture_output=True, text=True, timeout=20,
    )
    assert killed.returncode == -9, killed.stderr
    owned_root = upload_root / source_id / ".document-candidates" / document_id
    assert list(owned_root.rglob("published.md")), "child never wrote the real unbound snapshot"

    class Engine:
        async def delete_data(self, request, *, source=None):
            return deletion_receipt(request)

    async with SessionLocal() as session:
        await _delete_document_task_unlocked(session, await session.get(Job, job_id), engine_manager=Engine())
    assert not owned_root.exists(), "unbound snapshot survived document cleanup"
    assert peer_file.read_text(encoding="utf-8") == "peer publication"


@pytest.mark.parametrize("artifact", ["markdown", "assets"])
def test_killed_parser_staging_belongs_to_its_original_file(tmp_path, artifact):
    raw = tmp_path / "a.pdf"
    peer = tmp_path / "b.pdf"
    raw.write_bytes(b"original A")
    peer.write_bytes(b"original B")
    cache = str(raw) + ".parsed.mineru.test.md"
    peer_cache = str(peer) + ".parsed.mineru.test.md"
    worker = """
import os, signal, sys
from sag_api.parsing.mineru_artifacts import write_assets
from sag_api.parsing.service import _write_markdown
_write_markdown(sys.argv[2], 'peer body')
write_assets(sys.argv[2], {'image.png': b'peer image'})
def kill_before_replace(*args):
    os.kill(os.getpid(), signal.SIGKILL)
os.replace = kill_before_replace
if sys.argv[3] == 'markdown':
    _write_markdown(sys.argv[1], 'candidate A body')
else:
    write_assets(sys.argv[1], {'image.png': b'candidate A image'})
"""
    killed = subprocess.run(
        [sys.executable, "-c", worker, cache, peer_cache, artifact],
        capture_output=True, text=True, timeout=20,
    )
    assert killed.returncode == -9, killed.stderr
    before = set(tmp_path.iterdir())
    peer_owned = {peer, tmp_path / (peer.name + ".parsed.mineru.test.md"),
                  tmp_path / (peer.name + ".parsed.mineru.test.assets")}
    # Actual helper writes one staging file/directory before os.replace.
    orphan = before - peer_owned - {raw}
    assert len(orphan) == 1, before
    remove_parsed_sidecars(str(raw))
    assert not any(path.exists() for path in orphan), "killed staging lost its document ownership"
    assert peer.read_bytes() == b"original B"
    assert (tmp_path / (peer.name + ".parsed.mineru.test.md")).read_text() == "peer body"
    assert (tmp_path / (peer.name + ".parsed.mineru.test.assets") / "image.png").read_bytes() == b"peer image"
