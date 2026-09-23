"""Deadline/redirect/disconnect probes in the isolated client container."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from checks import require

API = "http://93.184.216.3:8001"
FIXTURE = "http://93.184.216.2:8000"
TOKEN = "isolated-benchmark-token-not-a-production-secret"  # nosec B105


def request(path: str, budget: int, index: int) -> dict:
    started = time.monotonic()
    request = urllib.request.Request(
        API + "/v1/extract",
        data=json.dumps({"url": FIXTURE + path + f"?fault-{index}", "render": "never"}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Scholight-Internal-Token": TOKEN,
            "X-Scholight-Budget-Ms": str(budget),
            "X-Scholight-Request-Id": f"fault-{index}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:  # nosec B310
            status, result = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        status, result = error.code, json.load(error)
    return {"path": path, "status": status, "result": result, "seconds": time.monotonic() - started}


def run() -> None:
    evidence = []
    for index, path in enumerate(("/fault/slow", "/fault/slow-redirect/20", "/fault/redirect/20")):
        row = request(path, 2500, index)
        require(row["status"] == (502 if path == "/fault/redirect/20" else 504), str(row))
        require(row["seconds"] < 3.2, str(row))
        evidence.append(row)
        require(
            request("/article-0", 5000, 100 + index)["status"] == 200,
            'request("/article-0", 5000, 100 + index)["status"] == 200',
        )
    for index in range(3):
        connection = http.client.HTTPConnection("93.184.216.3", 8001, timeout=6)
        connection.request(
            "POST",
            "/v1/extract",
            body=json.dumps(
                {"url": FIXTURE + f"/fault/slow?disconnect-{index}", "render": "never"}
            ),
            headers={
                "Content-Type": "application/json",
                "X-Scholight-Internal-Token": TOKEN,
                "X-Scholight-Request-Id": f"fault-disconnect-{index}",
            },
        )
        time.sleep(0.1)
        connection.close()
        time.sleep(0.25)
        row = request("/article-0", 5000, 200 + index)
        require(row["status"] == 200, str(row))
        evidence.append({"fault": "client_disconnect", "recovery": row})
    Path("/results/faults.json").write_text(
        json.dumps({"passed": True, "evidence": evidence}, indent=2)
    )


if __name__ == "__main__":
    run()
