"""Scenario REST authoring CLI. No generation is performed by check or imports."""
import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .scenario_transfer import download_png, TransferError
except ImportError:
    from scenario_transfer import download_png, TransferError


API_ROOT = "https://api.cloud.scenario.com/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "generated"
MAX_JSON_BYTES = 8 * 1024 * 1024
PENDING = {"pending", "queued", "warming-up", "in-progress", "finalizing"}
TERMINAL = {"success", "failure", "canceled"}
RETRY_CODES = {408, 429, 500, 502, 503, 504}
URL_PATTERN = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)


class ScenarioError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Authentication must never follow a Location supplied by a different server.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ScenarioError("invalid_id", "Expected an ID containing letters, digits, underscore or hyphen.")
    return value


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Expected a positive finite duration")
    return number


def safe_output(value, secrets=()):
    """Redact URLs even when embedded in remote text, not just named URL fields."""
    if isinstance(value, dict):
        hidden = {"authorization", "apikey", "apisecret", "password", "secret",
                  "accesstoken", "refreshtoken", "headers", "cookie"}
        return {safe_output(str(k), secrets): "[redacted]" if re.sub(r"[^a-z]", "", str(k).lower()) in hidden
                else safe_output(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_output(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return URL_PATTERN.sub("[url omitted]", value)
    return value


def retry_delay(header, attempt):
    if header:
        try:
            delay = float(header)
            if math.isfinite(delay):
                return max(0.0, delay)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(header)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return min(2 ** attempt, 16)


class ScenarioClient:
    def __init__(self, *, project_id=None, timeout=30, max_attempts=3,
                 opener=None, clock=time.monotonic, sleep=time.sleep):
        key = os.environ.get("SCENARIO_API_KEY", "")
        secret = os.environ.get("SCENARIO_API_SECRET", "")
        if not key or not secret:
            raise ScenarioError("missing_credentials",
                                "Set SCENARIO_API_KEY and SCENARIO_API_SECRET in the execution environment.")
        if ":" in key or any(c in key + secret for c in "\r\n\x00"):
            raise ScenarioError("invalid_credentials", "Invalid credential format.")
        encoded = base64.b64encode((key + ":" + secret).encode("utf-8")).decode("ascii")
        self._authorization = "Basic " + encoded
        self.redactions = (key, secret, key + ":" + secret, encoded, self._authorization)
        self.project_id = identifier(project_id) if project_id else None
        self.timeout = positive_seconds(timeout)
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise ScenarioError("invalid_attempts", "max_attempts must be between 1 and 5.")
        self.max_attempts = max_attempts
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock, self.sleep = clock, sleep

    def request(self, method, path, *, query=None, body=None, deadline=None):
        if method not in {"GET", "POST"} or not path.startswith("/") or "?" in path or "#" in path:
            raise ScenarioError("invalid_request", "Invalid REST request.")
        params = dict(query or {})
        if self.project_id:
            params["projectId"] = self.project_id
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        payload = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method=method, headers={
            "Authorization": self._authorization, "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "FantasyInterview-Scenario/1"})
        attempts = self.max_attempts if method == "GET" else 1
        deadline = deadline if deadline is not None else self.clock() + self.timeout * attempts
        for attempt in range(attempts):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ScenarioError("timeout", "REST request deadline expired.")
            retry_after = None
            try:
                with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                    content = response.read(MAX_JSON_BYTES + 1)
                if len(content) > MAX_JSON_BYTES:
                    raise ScenarioError("invalid_response", "REST response exceeded the size limit.")
                try:
                    result = json.loads(content)
                except (ValueError, UnicodeError):
                    raise ScenarioError("invalid_response", "REST response was not valid JSON.") from None
                if not isinstance(result, dict):
                    raise ScenarioError("invalid_response", "Expected a JSON object.")
                return result
            except urllib.error.HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                failure = ScenarioError("rate_limited" if status == 429 else "http_error",
                                        "Scenario returned HTTP " + str(status) + ".", status)
                retryable = status in RETRY_CODES
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                failure = ScenarioError("transport_error", "Scenario connection failed or timed out.")
                retryable = True
            delay = retry_delay(retry_after, attempt)
            if not retryable or attempt + 1 >= attempts or delay >= deadline - self.clock():
                raise failure from None
            self.sleep(delay)

    def models(self, *, privacy="public", page_size=20, pagination_token=None):
        if privacy not in {"public", "private"} or not 1 <= page_size <= 500:
            raise ScenarioError("invalid_query", "Invalid model privacy or page size.")
        result = self.request("GET", "/models", query={"privacy": privacy, "pageSize": page_size,
                                                       "paginationToken": pagination_token})
        if not isinstance(result.get("models"), list):
            raise ScenarioError("invalid_response", "Model list was missing.")
        return result

    def model(self, model_id):
        model_id = identifier(model_id)
        result = self.entity("/models/" + model_id, "model")
        if result.get("id") != model_id:
            raise ScenarioError("invalid_response", "Unexpected model identity.")
        return result

    def entity(self, path, key, *, deadline=None, query=None):
        result = self.request("GET", path, deadline=deadline, query=query)
        if not isinstance(result.get(key), dict):
            raise ScenarioError("invalid_response", "Expected resource was missing.")
        return result[key]

    def job(self, job_id, *, deadline=None):
        job_id = identifier(job_id)
        result = self.entity("/jobs/" + job_id, "job", deadline=deadline)
        if result.get("jobId") != job_id or result.get("status") not in PENDING | TERMINAL:
            raise ScenarioError("invalid_response", "Unexpected job identity or status.")
        return result

    def asset(self, asset_id):
        asset_id = identifier(asset_id)
        result = self.entity("/assets/" + asset_id, "asset", query={"originalAssets": "true"})
        if result.get("id") != asset_id:
            raise ScenarioError("invalid_response", "Unexpected asset identity.")
        return result

    def wait(self, job_id, *, wait_timeout=600, poll_interval=3):
        deadline = self.clock() + positive_seconds(wait_timeout)
        interval = positive_seconds(poll_interval)
        while self.clock() < deadline:
            job = self.job(job_id, deadline=deadline)
            if job["status"] in TERMINAL:
                return job
            self.sleep(min(interval, max(0, deadline - self.clock())))
        raise ScenarioError("wait_timeout", "Job is still pending. Resume wait with the same job ID or receipt.")


def job_summary(job):
    metadata = job.get("metadata") or {}
    asset_ids = metadata.get("assetIds") or []
    if not isinstance(asset_ids, list):
        raise ScenarioError("invalid_response", "Invalid job asset list.")
    progress = job.get("progress")
    if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (int, float))
                                 or not math.isfinite(progress) or not 0 <= progress <= 1):
        raise ScenarioError("invalid_response", "Invalid job progress.")
    return {"jobId": identifier(job["jobId"]), "status": job["status"],
            "assetIds": [identifier(value) for value in asset_ids],
            "progress": progress}


def read_json(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ScenarioError("invalid_input", "JSON file exceeded the size limit.")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
        json.dumps(value, allow_nan=False)
    except (ValueError, UnicodeError):
        raise ScenarioError("invalid_input", "Invalid JSON file.") from None
    if not isinstance(value, dict):
        raise ScenarioError("invalid_input", "Expected a JSON object in the file.")
    return value


def write_receipt(path, value, *, create=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if create:
        # Reserve the request before sending: concurrent/repeated commands cannot submit twice.
        try:
            with path.open("xb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError:
            raise ScenarioError("receipt_exists", "Receipt already exists. Inspect it and resume the recorded job; do not resubmit.") from None
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".scenario-", delete=False) as destination:
            temporary = Path(destination.name)
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def generate(client, model_id, inputs, receipt_path, *, allow_paid=False):
    if not allow_paid:
        raise ScenarioError("paid_not_enabled", "Generation requires explicit authorization and --allow-paid.")
    model_id = identifier(model_id)
    if not isinstance(inputs, dict) or not inputs:
        raise ScenarioError("invalid_input", "Provide a nonempty model input object.")
    payload = json.dumps(inputs, sort_keys=True, allow_nan=False).encode("utf-8")
    receipt = {"version": 1, "operation": "generate", "state": "submission_unknown",
               "modelId": model_id, "projectId": client.project_id,
               "requestSha256": hashlib.sha256(payload).hexdigest(),
               "createdAt": datetime.now(timezone.utc).isoformat()}
    # Query the actual model before submission. Its inputs are a Scenario schema, not JSON Schema.
    client.model(model_id)
    write_receipt(receipt_path, receipt, create=True)
    try:
        result = client.request("POST", "/generate/custom/" + model_id, body=inputs)
        job = result.get("job")
        if not isinstance(job, dict) or job.get("status") not in PENDING | TERMINAL:
            raise ScenarioError("invalid_response", "Generation response did not contain a recognized job.")
        summary = job_summary(job)
    except ScenarioError as error:
        receipt["errorCode"] = error.code
        if error.status is not None:
            receipt["httpStatus"] = error.status
        # Even a 5xx may follow acceptance. Keep this record instead of repeating a paid POST.
        try:
            write_receipt(receipt_path, receipt)
        except OSError:
            pass  # The pre-submit receipt still blocks a duplicate paid request.
        raise
    receipt.update(summary)
    receipt["state"] = "submitted"
    try:
        write_receipt(receipt_path, receipt)
    except OSError:
        raise ScenarioError("receipt_save_failed", "Job " + summary["jobId"] +
                            " was created but its receipt could not be updated. Resume this job; do not generate again.") from None
    return {"receipt": str(receipt_path), **summary}


def output_directory(value):
    path = Path(value)
    path = (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    roots = [PROJECT_ROOT / "generated"]
    if not any(path == root or root in path.parents for root in roots):
        raise ScenarioError("invalid_output", "Output must be within the repository generated directory.")
    return path


def download_asset(client, asset_id, output_dir):
    asset_id = identifier(asset_id)
    target = output_directory(output_dir) / (asset_id + ".png")
    # Resolve each URL again on retry/resume rather than persisting an expiring signed URL.
    asset = client.asset(asset_id)
    mime = asset.get("mimeType", "")
    if not isinstance(mime, str) or not mime.startswith("image/"):
        raise ScenarioError("unsupported_asset", "Only image assets can be saved by this tool.")
    if mime == "image/png":
        url = asset.get("url")
    else:
        result = client.request("POST", "/assets/" + asset_id + "/download", body={"targetFormat": "png"})
        url = result.get("url")
    if not isinstance(url, str) or not url:
        raise ScenarioError("invalid_response", "Asset download URL was missing.")
    return {"assetId": asset_id, **download_png(url, target, timeout=client.timeout,
                                               max_attempts=client.max_attempts)}


def build_parser():
    parser = argparse.ArgumentParser(description="Scenario REST authoring tools (Python standard library).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-id", default=os.environ.get("SCENARIO_PROJECT_ID"))
    common.add_argument("--request-timeout", type=positive_seconds, default=30)
    common.add_argument("--max-attempts", type=int, choices=range(1, 6), default=3,
                        help="Maximum attempts for GET requests; POST is always sent once.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", parents=[common], help="Read-only authentication/model connectivity check.")
    models = commands.add_parser("models", parents=[common])
    models.add_argument("--privacy", choices=["public", "private"], default="public")
    models.add_argument("--page-size", type=int, default=20)
    models.add_argument("--pagination-token")
    model = commands.add_parser("model", parents=[common])
    model.add_argument("model_id")
    create = commands.add_parser("generate", parents=[common])
    create.add_argument("model_id")
    create.add_argument("--input", required=True, help="JSON containing the model's input fields directly.")
    create.add_argument("--receipt", required=True, help="New local request receipt; must not already exist.")
    create.add_argument("--allow-paid", action="store_true")
    job = commands.add_parser("job", parents=[common])
    job.add_argument("job_id")
    wait = commands.add_parser("wait", parents=[common])
    source = wait.add_mutually_exclusive_group(required=True)
    source.add_argument("--job-id")
    source.add_argument("--receipt")
    wait.add_argument("--wait-timeout", type=positive_seconds, default=600)
    wait.add_argument("--poll-interval", type=positive_seconds, default=3)
    asset = commands.add_parser("asset", parents=[common])
    asset.add_argument("asset_id")
    download = commands.add_parser("download", parents=[common])
    download.add_argument("asset_ids", nargs="+")
    download.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    return parser


def execute(args, client):
    if args.command == "check":
        result = client.models(privacy="private", page_size=1)
        models = result["models"]
        if not models:
            models = client.models(privacy="public", page_size=1)["models"]
        checked_id = None
        if models:
            checked_id = identifier(models[0]["id"])
            client.model(checked_id)
        return {"connected": True, "readOnly": True, "modelChecked": checked_id,
                "generationRequests": 0}
    if args.command == "models":
        result = client.models(privacy=args.privacy, page_size=args.page_size,
                               pagination_token=args.pagination_token)
        return {"models": [{key: model.get(key) for key in ("id", "name", "type")}
                           for model in result["models"]],
                "nextPaginationToken": result.get("nextPaginationToken")}
    if args.command == "model":
        model = client.model(args.model_id)
        return {key: model.get(key) for key in ("id", "name", "type", "inputs")}
    if args.command == "generate":
        return generate(client, args.model_id, read_json(args.input), args.receipt, allow_paid=args.allow_paid)
    if args.command == "job":
        return job_summary(client.job(args.job_id))
    if args.command == "wait":
        receipt = read_json(args.receipt) if args.receipt else None
        if receipt is not None:
            if receipt.get("operation") != "generate" or receipt.get("version") != 1:
                raise ScenarioError("invalid_receipt", "Unsupported generation receipt.")
            if not receipt.get("jobId"):
                raise ScenarioError("unknown_submission", "Receipt has no job ID. Check Scenario job history before any new generation.")
            if client.project_id and client.project_id != receipt.get("projectId"):
                raise ScenarioError("scope_mismatch", "Project does not match the generation receipt.")
            client.project_id = identifier(receipt["projectId"]) if receipt.get("projectId") else None
        job = client.wait(identifier(receipt["jobId"] if receipt is not None else args.job_id),
                          wait_timeout=args.wait_timeout, poll_interval=args.poll_interval)
        result = job_summary(job)
        if receipt is not None:
            receipt.update(result)
            receipt["state"] = "finished"
            write_receipt(args.receipt, receipt)
        if result["status"] != "success":
            raise ScenarioError("job_failed", "Scenario job finished with status " + result["status"] + ".")
        return result
    if args.command == "asset":
        asset = client.asset(args.asset_id)
        result = {key: asset.get(key) for key in ("id", "kind", "mimeType", "status", "properties")}
        metadata = asset.get("metadata") or {}
        result["name"] = metadata.get("name") if isinstance(metadata, dict) else None
        return result
    if args.command == "download":
        return {"files": [download_asset(client, value, args.output_dir) for value in args.asset_ids]}
    raise ScenarioError("unknown_command", "Unknown command.")


def main(argv=None):
    client = None
    try:
        args = build_parser().parse_args(argv)
        if args.command == "generate" and not args.allow_paid:
            raise ScenarioError("paid_not_enabled", "Generation requires explicit authorization and --allow-paid.")
        client = ScenarioClient(project_id=args.project_id, timeout=args.request_timeout,
                                max_attempts=args.max_attempts)
        result = execute(args, client)
        print(json.dumps(safe_output(result, client.redactions), ensure_ascii=False, allow_nan=False))
        return 0
    except ScenarioError as error:
        result = {"error": error.code, "message": str(error)}
        if error.status is not None:
            result["httpStatus"] = error.status
    except TransferError:
        result = {"error": "transfer_error", "message": "Image transfer or PNG validation failed; existing files were preserved."}
    except KeyboardInterrupt:
        result = {"error": "interrupted", "message": "Stopped. Resume existing job; do not repeat a generation request."}
    except Exception:
        # File paths, HTTP exception strings and server error bodies may contain credentials or URLs.
        result = {"error": "local_or_response_error", "message": "Invalid local input, file operation or API response. No automatic generation retry."}
    print(json.dumps(safe_output(result, client.redactions if client else ()), ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
