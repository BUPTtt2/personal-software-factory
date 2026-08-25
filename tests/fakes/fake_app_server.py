import json
import sys
import time


mode = sys.argv[1]
if mode == "exit":
    raise SystemExit(2)
if mode == "hang":
    time.sleep(60)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        continue
    request_id = request.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {"userAgent": "fake"}}), flush=True)
    elif method == "thread/list":
        print(json.dumps({"method": "thread/status/changed", "params": {"threadId": "noise"}}), flush=True)
        print(json.dumps({"id": request_id, "result": {"data": [{"id": "thread-1", "cwd": "/repo", "status": {"type": mode}}], "nextCursor": None}}), flush=True)
    elif method == "thread/read":
        print(json.dumps({"id": request_id, "result": {"thread": {"id": request["params"]["threadId"], "cwd": "/repo", "status": {"type": mode}}}}), flush=True)
    else:
        print(json.dumps({"id": request_id, "error": {"code": -1, "message": "forbidden method"}}), flush=True)
