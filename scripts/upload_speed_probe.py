"""Measure browser-to-eyeserver tus throughput with real files.

Pass one or more files; --concurrency is the number of simultaneous files. Run on each
network (wired, Wi-Fi, VPN) with chunk sizes 8/32/64 MiB and concurrency 1/2/4.
UPLOAD_COOKIE may contain the current browser session cookie; it is never printed.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(url: str, method: str, data: bytes | None, headers: dict) -> urllib.response.addinfourl:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {url}: HTTP {exc.code}: {exc.read(1000)!r}") from exc


def metadata(**fields: str) -> str:
    return ",".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in fields.items())


def upload(path: Path, base: str, connection: str, chunk: int, cookie: str) -> dict:
    start = time.monotonic()
    common = {"Tus-Resumable": "1.0.0"}
    if cookie:
        common["Cookie"] = cookie
    headers = {**common, "Upload-Length": str(path.stat().st_size),
               "Upload-Metadata": metadata(connection_id=connection, filename=path.name)}
    with request(base + "/api/tus", "POST", b"", headers) as response:
        location = response.headers["Location"]
    url = location if location.startswith("http") else base + location
    sent = 0
    with path.open("rb") as source:
        while block := source.read(chunk):
            checksum = base64.b64encode(hashlib.sha256(block).digest()).decode()
            with request(url, "PATCH", block, {**common, "Content-Type": "application/offset+octet-stream",
                                                "Upload-Offset": str(sent),
                                                "Upload-Checksum": "sha256 " + checksum}) as response:
                sent = int(response.headers["Upload-Offset"])
    elapsed = time.monotonic() - start
    return {"file": path.name, "bytes": sent, "seconds": round(elapsed, 3),
            "mib_per_second": round(sent / 1024**2 / elapsed, 3), "upload_url": url}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--url", required=True, help="Local gateway base URL, without trailing slash")
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--network", required=True, help="Label: wired, wifi, or vpn")
    parser.add_argument("--chunk-mb", type=int, choices=(8, 32, 64), default=8)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=1)
    args = parser.parse_args()
    for path in args.files:
        if not path.is_file():
            parser.error(f"file not found: {path}")
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda p: upload(p, args.url.rstrip("/"), args.connection_id,
                                                 args.chunk_mb * 1024**2, os.environ.get("UPLOAD_COOKIE", "")),
                                args.files))
    elapsed = time.monotonic() - start
    total = sum(item["bytes"] for item in results)
    print(json.dumps({"network": args.network, "chunk_mb": args.chunk_mb,
                      "concurrency": args.concurrency, "bytes": total,
                      "seconds": round(elapsed, 3),
                      "aggregate_mib_per_second": round(total / 1024**2 / elapsed, 3),
                      "files": results}))


if __name__ == "__main__":
    main()
