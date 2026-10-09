import pytest

from dsh_sag_runtime.evidence import EvidenceLocator, EvidenceRefCodec


def test_evidence_ref_round_trips_unicode_across_codec_instances() -> None:
    locator = EvidenceLocator(namespace_id="产品文档", source_id="手册/一", chunk_id="片段-1")
    encoded = EvidenceRefCodec({"产品文档"}).encode(locator)

    assert EvidenceRefCodec({"产品文档"}).decode(encoded) == locator


@pytest.mark.parametrize("value", ["!", "e30", "eyJ2IjoyfQ", "eyJ2IjoxLCJuIjoieCJ9"])
def test_evidence_ref_rejects_malformed_or_incomplete_payload(value: str) -> None:
    with pytest.raises(ValueError):
        EvidenceRefCodec({"x"}).decode(value)


def test_evidence_ref_checks_namespace_allowlist_on_every_decode() -> None:
    value = EvidenceRefCodec({"private"}).encode(EvidenceLocator("private", "source", "chunk"))

    with pytest.raises(ValueError, match="not configured"):
        EvidenceRefCodec({"public"}).decode(value)
