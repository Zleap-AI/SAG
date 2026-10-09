import asyncio
import json
import sys

EVIDENCE_REF = "eyJjIjoiY2h1bmstMSIsIm4iOiJwcm9kdWN0LWRvY3MiLCJzIjoibWFudWFsIiwidiI6MX0"
CONTENT = "DW-2412P30 单文件上传上限为 100 MiB。"


async def main():
    tasks = {}
    write_lock = asyncio.Lock()

    async def respond(request_id, result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": request_id}
        payload["error" if error else "result"] = error or result
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    async def handle(request):
        request_id = request["id"]
        try:
            method = request["method"]
            if method == "initialize":
                await respond(request_id, {
                    "protocolVersion": "1.0",
                    "engineVersion": "0.14.0",
                    "health": "available",
                    "evidenceRead": True,
                    "namespaces": ["product-docs"],
                })
            elif method == "search":
                if request["params"]["query"] == "slow cancellation probe":
                    await asyncio.sleep(60)
                await respond(request_id, {
                    "query": request["params"]["query"],
                    "evidences": [{
                        "evidenceRef": EVIDENCE_REF,
                        "namespaceId": "product-docs",
                        "sourceId": "manual",
                        "title": "上传说明",
                        "excerpt": CONTENT,
                        "score": 0.98,
                    }],
                })
            elif method == "read":
                offset = request["params"]["offset"]
                maximum = request["params"]["maxChars"]
                page = CONTENT[offset:offset + maximum]
                result = {"title": "上传说明", "content": page, "offset": offset, "totalChars": len(CONTENT)}
                if offset + len(page) < len(CONTENT):
                    result["nextOffset"] = offset + len(page)
                await respond(request_id, result)
            elif method == "shutdown":
                await respond(request_id, {})
        except asyncio.CancelledError:
            await respond(request_id, error={
                "code": -32800,
                "message": "request cancelled",
                "data": {"code": "DSH_SAG_CANCELLED", "retryable": True},
            })
            raise
        finally:
            tasks.pop(request_id, None)

    while line := await asyncio.to_thread(sys.stdin.readline):
        request = json.loads(line)
        if request["method"] == "$/cancelRequest":
            task = tasks.get(request["params"]["id"])
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            continue
        task = asyncio.create_task(handle(request))
        tasks[request["id"]] = task
        if request["method"] == "shutdown":
            await task
            break
    if tasks:
        await asyncio.gather(*tasks.values(), return_exceptions=True)


asyncio.run(main())
