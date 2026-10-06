"""Resume interrupted HTTP audio bodies without splicing different byte ranges."""

from __future__ import annotations

import re
from http.client import IncompleteRead
from typing import Callable, Iterator, Any


def byte_window(response: Any) -> tuple[int, int | None, int | None]:
    content_range = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
    if match:
        start, end, total = map(int, match.groups())
        if response.status != 206 or not 0 <= start <= end < total:
            raise OSError("invalid audio Content-Range")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != end - start + 1:
            raise OSError("inconsistent audio response length")
        return start, end, total
    if response.status == 206:
        raise OSError("audio range response has no valid Content-Range")
    length = response.headers.get("Content-Length")
    total = int(length) if length is not None else None
    return 0, total - 1 if total else None, total


def audio_chunks(response: Any, reopen: Callable[[int, int | None], Any],
                 max_retries: int = 2) -> Iterator[bytes]:
    """Retry a broken body at the exact next byte; reject changed representations."""
    start, end, total = byte_window(response)
    expected = end - start + 1 if end is not None else None
    delivered = 0
    retries = 0
    etag = response.headers.get("ETag")
    upstream = response
    try:
        while expected is None or delivered < expected:
            failure = None
            try:
                read = getattr(upstream, "read1", upstream.read)
                block = read(min(65536, expected - delivered) if expected is not None else 65536)
                if not block:
                    if expected is None or delivered == expected:
                        return
                    failure = OSError("audio body ended before Content-Length")
            except IncompleteRead as exc:
                block = exc.partial
                failure = exc
            except (OSError, TimeoutError) as exc:
                block = b""
                failure = exc
            if block:
                delivered += len(block)
                if expected is not None and delivered > expected:
                    raise OSError("audio body exceeds Content-Length")
                yield block
            if not failure:
                continue
            upstream.close()
            while True:
                if retries >= max_retries:
                    raise OSError("audio transfer retries exhausted") from failure
                retries += 1
                try:
                    upstream = reopen(start + delivered, end)
                    resumed_start, resumed_end, resumed_total = byte_window(upstream)
                    if resumed_start != start + delivered or resumed_total != total or resumed_end != end:
                        raise OSError("audio server changed byte range during recovery")
                    new_etag = upstream.headers.get("ETag")
                    if etag and new_etag and etag != new_etag:
                        raise OSError("audio server changed representation during recovery")
                    break
                except (OSError, TimeoutError) as exc:
                    upstream.close()
                    failure = exc
    finally:
        upstream.close()
