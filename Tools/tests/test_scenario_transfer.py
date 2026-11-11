import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scenario_transfer as transfer


def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data +
            struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))


def png(width=1, height=1, color=b"\x00\x00\x00\xff"):
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"\x00" + color)) + chunk(b"IEND", b""))


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name) / "saved.png"
        self.url = "https://cdn.example.invalid/image.png?signature=secret"

    def response(self, payload=None):
        return io.BytesIO(png() if payload is None else payload)

    def test_valid_png_and_idempotent_save(self):
        first = transfer.save_png(png(), self.target)
        second = transfer.save_png(png(), self.target)
        self.assertEqual(first, second)
        self.assertEqual(self.target.read_bytes(), png())
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_short_header_truncated_chunk_and_missing_iend(self):
        for data in (b"\x89PNG\r\n\x1a\n", png()[:24], png()[:-1], png()[:-12]):
            with self.subTest(length=len(data)), self.assertRaises(transfer.TransferError):
                transfer.save_png(data, self.target)
        self.assertFalse(self.target.exists())

    def test_crc_for_header_data_and_ending(self):
        for position in (29, 42, len(png()) - 1):
            data = bytearray(png())
            data[position] ^= 1
            with self.subTest(position=position), self.assertRaises(transfer.TransferError):
                transfer.save_png(bytes(data), self.target)

    def test_png_dimension_limit(self):
        for width, height in ((8193, 1), (4097, 4097), (0, 1)):
            with self.subTest(width=width, height=height), self.assertRaises(transfer.TransferError):
                transfer.save_png(png(width, height), self.target)

    def test_existing_different_file_is_preserved(self):
        self.target.write_bytes(b"original")
        with self.assertRaises(transfer.TransferError):
            transfer.save_png(png(), self.target)
        self.assertEqual(self.target.read_bytes(), b"original")

    def test_concurrent_different_writer_is_not_overwritten(self):
        def competing_link(source, destination):
            Path(destination).write_bytes(b"other writer")
            raise FileExistsError

        with mock.patch.object(transfer.os, "link", side_effect=competing_link):
            with self.assertRaises(transfer.TransferError):
                transfer.save_png(png(), self.target)
        self.assertEqual(self.target.read_bytes(), b"other writer")
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_temp_cleanup_after_publish_failure(self):
        with mock.patch.object(transfer.os, "link", side_effect=OSError("secret path")):
            with self.assertRaisesRegex(transfer.TransferError, "Could not save PNG"):
                transfer.save_png(png(), self.target)
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_get_contains_no_authorization_and_handles_rate_limit(self):
        limited = urllib.error.HTTPError(self.url, 429, "private response", {"Retry-After": "2"}, None)
        opener = mock.Mock()
        opener.open.side_effect = [limited, self.response()]
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.time, "sleep") as sleep:
            result = transfer.download_png(self.url, self.target)
        self.assertEqual(result["width"], 1)
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(2)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertFalse(request.has_header("Authorization"))

    def test_retry_after_beyond_limit_does_not_sleep_or_retry(self):
        limited = urllib.error.HTTPError(self.url, 429, "secret", {"Retry-After": "300"}, None)
        opener = mock.Mock()
        opener.open.side_effect = limited
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.time, "sleep") as sleep:
            with self.assertRaises(transfer.TransferError) as caught:
                transfer.download_png(self.url, self.target)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()
        self.assertNotIn("secret", str(caught.exception))

    def test_timeouts_have_bounded_retries_and_safe_error(self):
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError(self.url)
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.time, "sleep"):
            with self.assertRaises(transfer.TransferError) as caught:
                transfer.download_png(self.url, self.target, max_attempts=2)
        self.assertEqual(opener.open.call_count, 2)
        self.assertNotIn("secret", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_server_failure_retries_but_corrupt_png_does_not(self):
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.HTTPError(self.url, 503, "secret", {}, None),
                                   self.response(png()[:-2])]
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.time, "sleep"):
            with self.assertRaises(transfer.TransferError):
                transfer.download_png(self.url, self.target)
        self.assertEqual(opener.open.call_count, 2)
        self.assertFalse(self.target.exists())

    def test_timeout_budget_prevents_another_request(self):
        opener = mock.Mock()
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(transfer.TransferError, "timed out"):
                transfer.download_png(self.url, self.target, timeout=1)
        opener.open.assert_not_called()

    def test_auth_failure_is_not_retried_or_exposed(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(self.url, 403, "secret", {}, None)
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(transfer.TransferError) as caught:
                transfer.download_png(self.url, self.target)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("secret", str(caught.exception))

    def test_initial_urls_and_redirects_require_https(self):
        for url in ("http://example.invalid/a", "https://user:secret@example.invalid/a", "https:///a"):
            with self.subTest(url=url), self.assertRaises(transfer.TransferError):
                transfer.download_png(url, self.target)
        redirect = transfer.HttpsRedirect()
        request = urllib.request.Request(self.url)
        with self.assertRaises(transfer.TransferError):
            redirect.redirect_request(request, None, 302, "", {}, "http://example.invalid/a")

    def test_redirect_removes_authentication_headers(self):
        request = urllib.request.Request(self.url, headers={"Authorization": "secret", "Cookie": "secret"})
        redirected = transfer.HttpsRedirect().redirect_request(
            request, None, 302, "", {}, "https://other.example.invalid/a")
        self.assertFalse(redirected.has_header("Authorization"))
        self.assertFalse(redirected.has_header("Cookie"))

    def test_cli_never_prints_signed_url_or_raw_exception(self):
        error = urllib.error.URLError(self.url)
        stderr = io.StringIO()
        stdin = io.StringIO(json.dumps({"url": self.url}) + "\n")
        with mock.patch.object(transfer, "download_png", side_effect=error), \
                mock.patch.object(transfer.sys, "stdin", stdin), \
                mock.patch.object(transfer.sys, "stderr", stderr):
            result = transfer.cli(["download", str(self.target)])
        self.assertEqual(result, 1)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertNotIn("https", stderr.getvalue())

    def test_existing_upload_cli_keeps_multipart_order(self):
        self.target.write_bytes(b"abcd")
        responses = [io.BytesIO(), io.BytesIO()]
        for response in responses:
            response.status = 201
        opener = mock.Mock()
        opener.open.side_effect = responses
        data = {"parts": [{"part_number": 2, "upload_url": self.url, "content_length": 2},
                          {"part_number": 1, "upload_url": self.url, "content_length": 2}]}
        output = io.StringIO()
        with mock.patch.object(transfer.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(transfer.sys, "stdin", io.StringIO(json.dumps(data))), \
                mock.patch.object(transfer.sys, "stdout", output):
            result = transfer.cli(["upload", str(self.target)])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0].data for call in opener.open.call_args_list], [b"ab", b"cd"])
        self.assertEqual(json.loads(output.getvalue())["bytes"], 4)
        self.assertNotIn("secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
