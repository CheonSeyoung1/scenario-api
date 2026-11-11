import base64
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scenario_api as api
import scenario_transfer as transfer


KEY = "unit-test-key"
SECRET = "unit-test-secret"
MODEL = "model_test"
JOB = "job_test"
ASSET = "asset_test"
SIGNED_URL = "https://cdn.example.invalid/image.png?signature=never-log-this"


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def model_response():
    return response({"model": {"id": MODEL, "name": "Test model",
                               "inputs": [{"name": "prompt", "type": "string", "required": {"always": True}}]}})


def job_response(status, asset_ids=None):
    return response({"job": {"jobId": JOB, "status": status, "progress": 1 if status == "success" else 0,
                             "metadata": {"assetIds": asset_ids or []}}})


def http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(SIGNED_URL, status, SECRET, headers,
                                  io.BytesIO((KEY + SECRET + SIGNED_URL).encode()))


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ScenarioApiTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"SCENARIO_API_KEY": KEY, "SCENARIO_API_SECRET": SECRET}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.receipt = self.root / "request.json"
        self.clock = Clock()
        self.opener = mock.Mock()

    def client(self, **kwargs):
        return api.ScenarioClient(opener=self.opener, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def args(self, *values):
        return api.build_parser().parse_args(values)

    def seed_receipt(self, **changes):
        value = {"version": 1, "operation": "generate", "state": "submitted",
                 "projectId": "proj_test", "jobId": JOB, "modelId": MODEL}
        value.update(changes)
        api.write_receipt(self.receipt, value, create=True)
        return value

    def requests(self):
        return [call.args[0] for call in self.opener.open.call_args_list]

    def test_missing_credentials_fail_before_network(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(api.ScenarioError) as caught:
                self.client()
        self.assertEqual(caught.exception.code, "missing_credentials")
        self.opener.open.assert_not_called()

    def test_basic_auth_and_model_pagination_query(self):
        self.opener.open.return_value = response({"models": [], "nextPaginationToken": "page-2"})
        result = self.client(project_id="proj_test").models(page_size=2, pagination_token="page+1")
        request = self.requests()[0]
        expected = "Basic " + base64.b64encode((KEY + ":" + SECRET).encode()).decode()
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc, "api.cloud.scenario.com")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query, {"privacy": ["public"], "pageSize": ["2"],
                                 "paginationToken": ["page+1"], "projectId": ["proj_test"]})
        self.assertEqual(result["nextPaginationToken"], "page-2")

    def test_connectivity_check_uses_only_model_gets(self):
        self.opener.open.side_effect = [response({"models": [{"id": MODEL}]}), model_response()]
        result = api.execute(self.args("check"), self.client())
        self.assertTrue(result["connected"])
        self.assertTrue(result["readOnly"])
        self.assertEqual(result["generationRequests"], 0)
        self.assertEqual([r.get_method() for r in self.requests()], ["GET", "GET"])
        self.assertEqual([urllib.parse.urlsplit(r.full_url).path for r in self.requests()],
                         ["/v1/models", "/v1/models/" + MODEL])
        first_query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.requests()[0].full_url).query)
        self.assertEqual(first_query["privacy"], ["private"])

    def test_model_command_preserves_official_input_schema(self):
        self.opener.open.return_value = model_response()
        result = api.execute(self.args("model", MODEL), self.client())
        self.assertEqual(result["inputs"], [{"name": "prompt", "type": "string", "required": {"always": True}}])

    def test_asset_command_reads_name_from_metadata(self):
        self.opener.open.return_value = response({"asset": {"id": ASSET, "mimeType": "image/png",
                                                            "url": SIGNED_URL,
                                                            "metadata": {"name": "Portrait preview"}}})
        result = api.execute(self.args("asset", ASSET), self.client())
        self.assertEqual(result["name"], "Portrait preview")
        self.assertEqual(result["id"], ASSET)
        self.assertNotIn(SIGNED_URL, json.dumps(result))

    def test_check_uses_public_fallback_only_after_authenticated_empty_private_list(self):
        self.opener.open.side_effect = [response({"models": []}), response({"models": [{"id": MODEL}]}),
                                       model_response()]
        result = api.execute(self.args("check"), self.client())
        self.assertEqual(result["modelChecked"], MODEL)
        self.assertEqual([r.get_method() for r in self.requests()], ["GET", "GET", "GET"])
        privacy = [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)["privacy"][0]
                   for r in self.requests()[:2]]
        self.assertEqual(privacy, ["private", "public"])

    def test_get_retries_rate_limit_and_server_error(self):
        self.opener.open.side_effect = [http_error(429, "2"), http_error(503), model_response()]
        self.assertEqual(self.client().model(MODEL)["id"], MODEL)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.clock.waits, [2, 2])

    def test_get_timeout_retry_is_bounded(self):
        self.opener.open.side_effect = TimeoutError(SECRET + SIGNED_URL)
        with self.assertRaises(api.ScenarioError) as caught:
            self.client(max_attempts=2).model(MODEL)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SIGNED_URL, str(caught.exception))

    def test_retry_after_outside_deadline_is_not_shortened(self):
        self.opener.open.side_effect = http_error(429, "300")
        with self.assertRaises(api.ScenarioError) as caught:
            self.client(timeout=1).models()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.clock.waits, [])

    def test_paid_post_failures_are_never_retried_and_remain_unknown(self):
        for label, failure in (("timeout", TimeoutError(SECRET)), ("server", http_error(503)),
                               ("rate", http_error(429, "1"))):
            with self.subTest(label=label):
                self.opener.reset_mock()
                self.opener.open.side_effect = [model_response(), failure]
                path = self.root / (label + ".json")
                with self.assertRaises(api.ScenarioError):
                    api.generate(self.client(), MODEL, {"prompt": SECRET}, path, allow_paid=True)
                self.assertEqual([r.get_method() for r in self.requests()], ["GET", "POST"])
                receipt = api.read_json(path)
                self.assertEqual(receipt["state"], "submission_unknown")
                self.assertNotIn("jobId", receipt)
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_existing_receipt_prevents_a_second_paid_request(self):
        self.opener.open.side_effect = [model_response(), job_response("pending"), model_response()]
        client = self.client()
        api.generate(client, MODEL, {"prompt": "test"}, self.receipt, allow_paid=True)
        with self.assertRaises(api.ScenarioError) as caught:
            api.generate(client, MODEL, {"prompt": "changed"}, self.receipt, allow_paid=True)
        self.assertEqual(caught.exception.code, "receipt_exists")
        self.assertEqual([r.get_method() for r in self.requests()].count("POST"), 1)
        self.assertEqual(api.read_json(self.receipt)["jobId"], JOB)

    def test_generate_without_paid_flag_never_opens_network_or_reads_input(self):
        output = io.StringIO()
        with mock.patch.object(api.urllib.request, "build_opener") as build, \
                contextlib.redirect_stderr(output):
            result = api.main(["generate", MODEL, "--input", str(self.root / "missing.json"),
                               "--receipt", str(self.receipt)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "paid_not_enabled")
        build.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_successful_submission_persists_job_without_prompt_or_auth(self):
        self.opener.open.side_effect = [model_response(), job_response("queued")]
        result = api.generate(self.client(project_id="proj_test"), MODEL,
                              {"prompt": SECRET, "referenceImages": [SIGNED_URL]}, self.receipt, allow_paid=True)
        receipt = api.read_json(self.receipt)
        self.assertEqual(result["jobId"], JOB)
        self.assertEqual(receipt["state"], "submitted")
        self.assertEqual(receipt["projectId"], "proj_test")
        self.assertEqual(len(receipt["requestSha256"]), 64)
        text = self.receipt.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertNotIn(SIGNED_URL, text)
        self.assertNotIn("Authorization", text)

    def test_success_response_with_receipt_disk_failure_retains_recovery_id(self):
        self.opener.open.side_effect = [model_response(), job_response("pending")]
        real_write = api.write_receipt

        def fail_update(path, value, create=False):
            if create:
                return real_write(path, value, create=True)
            raise OSError(SECRET)

        with mock.patch.object(api, "write_receipt", side_effect=fail_update):
            with self.assertRaises(api.ScenarioError) as caught:
                api.generate(self.client(), MODEL, {"prompt": "test"}, self.receipt, allow_paid=True)
        self.assertEqual(caught.exception.code, "receipt_save_failed")
        self.assertIn(JOB, str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(api.read_json(self.receipt)["state"], "submission_unknown")
        self.assertEqual([r.get_method() for r in self.requests()].count("POST"), 1)

    def test_unknown_submission_receipt_requires_manual_recovery(self):
        self.seed_receipt(state="submission_unknown", jobId=None)
        with self.assertRaises(api.ScenarioError) as caught:
            api.execute(self.args("wait", "--receipt", str(self.receipt)), self.client())
        self.assertEqual(caught.exception.code, "unknown_submission")
        self.opener.open.assert_not_called()

    def test_wait_restores_receipt_scope_and_records_completion(self):
        self.seed_receipt()
        self.opener.open.side_effect = [job_response("in-progress"), job_response("success", [ASSET])]
        result = api.execute(self.args("wait", "--receipt", str(self.receipt), "--poll-interval", "1"), self.client())
        self.assertEqual(result["assetIds"], [ASSET])
        self.assertEqual(self.clock.waits, [1])
        self.assertEqual(api.read_json(self.receipt)["state"], "finished")
        for request in self.requests():
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["projectId"], ["proj_test"])

    def test_failure_and_cancellation_are_terminal_and_recorded(self):
        for status in ("failure", "canceled"):
            with self.subTest(status=status):
                path = self.root / (status + ".json")
                api.write_receipt(path, {"version": 1, "operation": "generate", "state": "submitted",
                                        "jobId": JOB, "projectId": None}, create=True)
                self.opener.open.side_effect = [job_response(status)]
                with self.assertRaises(api.ScenarioError) as caught:
                    api.execute(self.args("wait", "--receipt", str(path)), self.client())
                self.assertEqual(caught.exception.code, "job_failed")
                self.assertEqual(api.read_json(path)["status"], status)
                self.assertEqual(api.read_json(path)["state"], "finished")

    def test_wait_timeout_preserves_receipt_and_can_resume_without_generation(self):
        before = self.seed_receipt()
        self.opener.open.side_effect = [job_response("pending"), job_response("finalizing"),
                                       job_response("success", [ASSET])]
        args = self.args("wait", "--receipt", str(self.receipt), "--wait-timeout", "2", "--poll-interval", "1")
        client = self.client()
        with self.assertRaises(api.ScenarioError) as caught:
            api.execute(args, client)
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertEqual(api.read_json(self.receipt), before)
        self.assertEqual(api.execute(args, client)["status"], "success")
        self.assertTrue(all(request.get_method() == "GET" for request in self.requests()))

    def test_receipt_scope_mismatch_is_rejected_before_network(self):
        self.seed_receipt()
        with self.assertRaises(api.ScenarioError) as caught:
            api.execute(self.args("wait", "--receipt", str(self.receipt)), self.client(project_id="proj_other"))
        self.assertEqual(caught.exception.code, "scope_mismatch")
        self.opener.open.assert_not_called()

    def test_mismatched_resource_ids_and_unrecognized_job_status_fail(self):
        cases = (("model", MODEL, {"model": {"id": "model_other"}}),
                 ("asset", ASSET, {"asset": {"id": "asset_other"}}),
                 ("job", JOB, {"job": {"jobId": "job_other", "status": "success"}}),
                 ("job", JOB, {"job": {"jobId": JOB, "status": "unexpected"}}))
        for method, identifier, payload in cases:
            with self.subTest(method=method, payload=payload):
                self.opener.open.side_effect = None
                self.opener.open.return_value = response(payload)
                with self.assertRaises(api.ScenarioError) as caught:
                    getattr(self.client(), method)(identifier)
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_401_body_and_signed_url_never_reach_cli_output(self):
        self.opener.open.side_effect = http_error(401)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(api, "ScenarioClient", return_value=self.client()), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = api.main(["model", MODEL])
        self.assertEqual(result, 1)
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["httpStatus"], 401)
        self.assertEqual(self.opener.open.call_count, 1)
        for value in (KEY, SECRET, SIGNED_URL, "never-log-this"):
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())

    def test_safe_output_redacts_nested_values_keys_and_auth(self):
        client = self.client()
        raw = {SECRET: "value", "Authorization": client._authorization,
               "records": [{"message": "embedded " + SIGNED_URL + " " + SECRET,
                             "api_key": KEY}], "safe": 4}
        result = json.dumps(api.safe_output(raw, client.redactions))
        for value in (KEY, SECRET, SIGNED_URL, "never-log-this", client._authorization):
            self.assertNotIn(value, result)

    def test_api_redirect_handler_never_forwards_authenticated_request(self):
        request = urllib.request.Request(api.API_ROOT + "/models", headers={"Authorization": "Basic test"})
        redirected = api.NoRedirect().redirect_request(request, None, 302, "", {}, SIGNED_URL)
        self.assertIsNone(redirected)

    def test_download_rejects_paths_outside_resource_directories_before_get(self):
        with mock.patch.object(api, "PROJECT_ROOT", self.root):
            for path in ("Logs", "ContentAuthoring/Art", "InterviewGame/Assets/Game/Art",
                         "generated/../../outside", str(self.root.parent / "outside")):
                with self.subTest(path=path), self.assertRaises(api.ScenarioError) as caught:
                    api.download_asset(self.client(), ASSET, path)
                self.assertEqual(caught.exception.code, "invalid_output")
        self.opener.open.assert_not_called()

    def test_output_directory_accepts_generated_and_rejects_symlink_escape(self):
        with mock.patch.object(api, "PROJECT_ROOT", self.root):
            self.assertEqual(api.output_directory("generated"), self.root / "generated")
            self.assertEqual(api.output_directory("generated/images"), self.root / "generated/images")
            (self.root / "generated").mkdir()
            (self.root / "generated/escape").symlink_to(self.root.parent, target_is_directory=True)
            with self.assertRaises(api.ScenarioError) as caught:
                api.output_directory("generated/escape/outside")
            self.assertEqual(caught.exception.code, "invalid_output")

    def test_download_uses_separate_unauthenticated_cdn_request(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

        image = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) +
                 chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b""))
        self.opener.open.return_value = response({"asset": {"id": ASSET, "mimeType": "image/png", "url": SIGNED_URL}})
        cdn = mock.Mock()
        cdn.open.return_value = io.BytesIO(image)
        client = self.client()
        with mock.patch.object(api, "PROJECT_ROOT", self.root), \
                mock.patch.object(transfer.urllib.request, "build_opener", return_value=cdn):
            result = api.download_asset(client, ASSET, "generated")
        self.assertEqual(Path(result["saved"]).read_bytes(), image)
        self.assertEqual(self.requests()[0].get_header("Authorization"), client._authorization)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.requests()[0].full_url).query)
        self.assertEqual(query["originalAssets"], ["true"])
        request = cdn.open.call_args.args[0]
        self.assertEqual(request.full_url, SIGNED_URL)
        self.assertFalse(request.has_header("Authorization"))
        self.assertFalse(request.has_header("Cookie"))
        self.assertNotIn(SIGNED_URL, json.dumps(result))

    def test_malformed_response_reports_no_remote_content(self):
        for payload in (SECRET.encode(), b"[]", b'{"models":null}'):
            with self.subTest(payload=payload):
                self.opener.open.side_effect = None
                self.opener.open.return_value = io.BytesIO(payload)
                with self.assertRaises(api.ScenarioError) as caught:
                    self.client().models()
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertNotIn(SECRET, str(caught.exception))

    def test_untrusted_progress_cannot_be_written_to_generation_receipt(self):
        self.opener.open.side_effect = [model_response(), response({"job": {"jobId": JOB, "status": "pending",
                                                                         "progress": SECRET + SIGNED_URL}})]
        with self.assertRaises(api.ScenarioError) as caught:
            api.generate(self.client(), MODEL, {"prompt": "test"}, self.receipt, allow_paid=True)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertNotIn(SECRET, self.receipt.read_text(encoding="utf-8"))
        self.assertNotIn(SIGNED_URL, self.receipt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
