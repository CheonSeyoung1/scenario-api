"""Scenario의 임시 전송 URL을 명령 인자나 파일에 남기지 않고 전달한다."""
import argparse
import datetime
import email.utils
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_RETRY_WAIT = 30


class TransferError(Exception):
    """호출자에게 전달해도 URL이나 인증 정보가 노출되지 않는 전송 오류."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def require_https(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None):
            raise ValueError
        parsed.port
    except (TypeError, ValueError):
        raise TransferError("Expected an HTTPS URL without embedded credentials") from None


def read_private_line():
    if os.name == "nt" and sys.stdin.isatty():
        import msvcrt
        chars = []
        print("Ready for transfer metadata on stdin.", flush=True)
        while True:
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                return "".join(chars)
            if char == "\x03":
                raise KeyboardInterrupt
            chars.append(char)
    return sys.stdin.readline()


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


def validate_png(payload):
    """PNG 컨테이너·청크 CRC와 크기를 검사한다. 픽셀 재인코딩은 하지 않는다."""
    if len(payload) > MAX_PNG_BYTES or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise TransferError("Expected a PNG of at most 32 MiB")
    position = 8
    dimensions = None
    image_bytes = 0
    while position < len(payload):
        if position + 12 > len(payload):
            raise TransferError("Incomplete PNG chunk")
        length = struct.unpack_from(">I", payload, position)[0]
        chunk_type = payload[position + 4:position + 8]
        chunk_end = position + 12 + length
        if chunk_end > len(payload):
            raise TransferError("Incomplete PNG chunk")
        content = payload[position + 8:position + 8 + length]
        crc = struct.unpack_from(">I", payload, position + 8 + length)[0]
        if zlib.crc32(chunk_type + content) & 0xffffffff != crc:
            raise TransferError("PNG checksum mismatch")
        if dimensions is None:
            if chunk_type != b"IHDR" or length != 13:
                raise TransferError("Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", content)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                      4: (8, 16), 6: (8, 16)}
            if (depth not in depths.get(color, ()) or compression != 0 or
                    filtering != 0 or interlace not in (0, 1)):
                raise TransferError("Invalid PNG header")
            if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16777216):
                raise TransferError("PNG dimensions out of bounds")
            dimensions = (width, height)
        elif chunk_type == b"IHDR":
            raise TransferError("Duplicate PNG header")
        if chunk_type == b"IDAT":
            image_bytes += length
        if chunk_type == b"IEND":
            if length != 0 or image_bytes == 0 or chunk_end != len(payload):
                raise TransferError("Invalid PNG ending")
            return dimensions
        position = chunk_end
    raise TransferError("Incomplete PNG ending")


def save_png(payload, path):
    """검사한 PNG를 배타적으로 발행한다. 같은 파일의 재시도만 허용한다."""
    width, height = validate_png(payload)
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise TransferError("Target must not be a symbolic link")
        if path.exists():
            if path.read_bytes() != payload:
                raise TransferError("Target already contains a different file")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="." + path.name + ".", suffix=".partial", dir=path.parent)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            try:
                # replace/rename의 덮어쓰기 경쟁을 피한다. NTFS와 Linux 파일시스템에서
                # 같은 디렉터리의 완성된 파일을 원자적으로 공개하며, 기존 대상은 건드리지 않는다.
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != payload:
                    raise TransferError("Target already contains a different file") from None
    except OSError:
        raise TransferError("Could not save PNG without overwriting the target") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise TransferError("Could not remove temporary PNG") from None
    return {"saved": str(path), "width": width, "height": height,
            "sha256": hashlib.sha256(payload).hexdigest()}


def _retry_delay(headers, attempt):
    value = headers.get("Retry-After") if headers is not None else None
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                target = email.utils.parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=datetime.timezone.utc)
                delay = (target - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None
        if delay is not None and math.isfinite(delay):
            return max(0, delay)
    return min(2 ** attempt, 8)


def download_png(url, path, timeout=120, max_attempts=3):
    """인증 헤더 없이 GET하고 저장한다. timeout은 요청·재시도 대기의 시간 예산이다.

    urllib의 timeout은 소켓 I/O 단위로 적용되므로 느린 응답의 마지막 I/O가
    이 예산을 조금 초과할 수 있다. 생성 POST나 업로드는 이 재시도를 사용하지 않는다.
    """
    require_https(url)
    if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0 or
            not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5):
        raise TransferError("Invalid download timeout or attempt count")
    opener = urllib.request.build_opener(HttpsRedirect())
    deadline = time.monotonic() + timeout
    for attempt in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError("Download timed out")
        status = None
        retry_headers = None
        try:
            request = urllib.request.Request(url, method="GET")
            with opener.open(request, timeout=min(remaining, 30)) as response:
                parts = []
                count = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    part = response.read(min(65536, MAX_PNG_BYTES + 1 - count))
                    if not part:
                        break
                    parts.append(part)
                    count += len(part)
                    if count > MAX_PNG_BYTES:
                        raise TransferError("Expected a PNG of at most 32 MiB")
                payload = b"".join(parts)
            break
        except urllib.error.HTTPError as error:
            status = error.code
            retry_headers = error.headers
            error.close()
            if status != 429 and not 500 <= status < 600:
                raise TransferError("Download rejected", status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            pass
        if attempt + 1 == max_attempts:
            raise TransferError("Download failed after bounded retries", status=status) from None
        delay = _retry_delay(retry_headers, attempt)
        if delay > MAX_RETRY_WAIT or delay >= deadline - time.monotonic():
            raise TransferError("Download retry deferred; try again later", status=status) from None
        time.sleep(delay)
    return save_png(payload, path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("upload", "download"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    data = json.loads(read_private_line())
    if args.action == "upload":
        opener = urllib.request.build_opener(HttpsRedirect())
        with args.path.open("rb") as source:
            for part in sorted(data["parts"], key=lambda item: item["part_number"]):
                url = part["upload_url"]
                require_https(url)
                payload = source.read(part["content_length"])
                if len(payload) != part["content_length"]:
                    raise ValueError("Unexpected upload length")
                request = urllib.request.Request(url, data=payload, method="PUT")
                with opener.open(request, timeout=120) as response:
                    if response.status not in (200, 201, 204):
                        raise ValueError("Upload rejected")
            if source.read(1):
                raise ValueError("Upload parts do not cover source")
        print(json.dumps({"uploaded": str(args.path), "bytes": args.path.stat().st_size}), flush=True)
    else:
        print(json.dumps(download_png(data["url"], args.path)), flush=True)


def cli(argv=None):
    try:
        main(argv)
        return 0
    except Exception as error:
        # urllib exceptions can contain the signed URL; report the class only.
        print("Transfer failed: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
