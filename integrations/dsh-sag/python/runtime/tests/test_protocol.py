import json

import pytest
from pydantic import ValidationError

from dsh_sag_runtime.protocol import (
    MAX_FRAME_BYTES,
    CancelRequest,
    InitializeRequest,
    ReadRequest,
    SearchRequest,
    decode_request,
    encode_response,
    success_response,
)


def test_decode_request_discriminates_supported_methods() -> None:
    initialize = decode_request(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}')
    search = decode_request('{"jsonrpc":"2.0","id":2,"method":"search","params":{"query":"上传限制","namespaces":["product-docs"],"mode":"fast","limit":8}}'.encode())
    read = decode_request(b'{"jsonrpc":"2.0","id":3,"method":"read","params":{"evidenceRef":"abc","offset":0,"maxChars":1000,"includeEvents":false}}')
    cancel = decode_request(b'{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":3}}')

    assert isinstance(initialize, InitializeRequest)
    assert isinstance(search, SearchRequest)
    assert isinstance(read, ReadRequest)
    assert isinstance(cancel, CancelRequest)
    assert search.params.namespaces == ("product-docs",)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2.0"}}',
        b'{"jsonrpc":"2.0","id":2,"method":"search","params":{"query":"q","namespaces":[],"mode":"fast","limit":0}}',
        b'{"jsonrpc":"2.0","id":3,"method":"read","params":{"evidenceRef":"abc","offset":-1,"maxChars":1,"includeEvents":false}}',
        b'{"jsonrpc":"2.0","id":4,"method":"shutdown","params":{},"unknown":true}',
    ],
)
def test_decode_request_rejects_invalid_or_unknown_fields(payload: bytes) -> None:
    with pytest.raises((ValidationError, ValueError)):
        decode_request(payload)


def test_frame_budget_is_checked_before_json_parsing() -> None:
    with pytest.raises(ValueError, match="8 MiB"):
        decode_request(b"x" * (MAX_FRAME_BYTES + 1))


def test_response_serialization_uses_wire_aliases_and_one_line() -> None:
    frame = encode_response(success_response(9, {"nextOffset": 20}))
    assert frame.endswith(b"\n")
    assert json.loads(frame) == {"jsonrpc": "2.0", "id": 9, "result": {"nextOffset": 20}}
