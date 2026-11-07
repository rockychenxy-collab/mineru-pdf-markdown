from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import requests

from .models import BatchSubmission, ParseOptions, SourceRecord, TaskResult


TaskProgressCallback = Callable[[list[TaskResult]], None]


def progress_percent(value: Any) -> float | None:
    """Best-effort conversion of MinerU's varying progress payloads to 0-100."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if 0 <= number <= 100:
            return number
        return None
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            number = float(text)
        except ValueError:
            return None
        return number if 0 <= number <= 100 else None
    if not isinstance(value, dict):
        return None

    for key in ("progress", "extract_progress", "progress_percent", "percent", "percentage"):
        if key in value:
            parsed = progress_percent(value[key])
            if parsed is not None:
                return parsed
    for completed_key, total_key in (
        ("completed_pages", "total_pages"),
        ("extracted_pages", "total_pages"),
        ("current", "total"),
    ):
        completed = value.get(completed_key)
        total = value.get(total_key)
        if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
            return max(0.0, min(100.0, float(completed) / float(total) * 100))
    for nested in value.values():
        parsed = progress_percent(nested)
        if parsed is not None:
            return parsed
    return None


class MineruAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class MineruClient:
    """Small, secret-safe client for MinerU's hosted v4 API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://mineru.net",
        request_timeout: float = 60.0,
        retry_attempts: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key cannot be empty")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.retry_attempts = max(1, retry_attempts)
        self.sleep_fn = sleep_fn
        self.session = session or requests.Session()

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.request_timeout)
        headers = dict(kwargs.pop("headers", {}))
        headers = {**self._headers(json_body="json" in kwargs), **headers}

        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < self.retry_attempts:
                        self.sleep_fn(min(2**attempt, 10))
                        continue
                if response.status_code >= 400:
                    raise MineruAPIError(
                        f"MinerU API returned HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise MineruAPIError("MinerU API returned invalid JSON", response.status_code) from exc
                if not isinstance(body, dict):
                    raise MineruAPIError("MinerU API returned an unexpected JSON shape", response.status_code)
                if body.get("code", 0) not in (0, None):
                    raise MineruAPIError(
                        str(body.get("msg") or "MinerU API returned an error"),
                        status_code=response.status_code,
                        code=body.get("code"),
                    )
                return body
            except (requests.RequestException, MineruAPIError) as exc:
                last_error = exc
                if isinstance(exc, MineruAPIError) and exc.status_code not in {429} and (
                    exc.status_code is None or exc.status_code < 500
                ):
                    raise
                if attempt + 1 < self.retry_attempts:
                    self.sleep_fn(min(2**attempt, 10))
        if isinstance(last_error, MineruAPIError):
            raise last_error
        raise MineruAPIError("MinerU API request failed after retries") from last_error

    @staticmethod
    def _valid_records(records: list[SourceRecord]) -> list[SourceRecord]:
        valid = [record for record in records if record.valid]
        if not valid:
            raise ValueError("no valid PDF records to submit")
        if len(valid) > 50:
            raise ValueError("MinerU signed-upload requests support at most 50 files")
        return valid

    def submit_batch(
        self,
        records: list[SourceRecord],
        options: ParseOptions | None = None,
        upload_concurrency: int = 2,
    ) -> BatchSubmission:
        options = options or ParseOptions()
        valid = self._valid_records(records)
        payload: dict[str, Any] = {
            "files": [{"name": record.path.name, "data_id": record.data_id} for record in valid],
            **options.to_payload(),
        }
        body = self._request_json("POST", "/api/v4/file-urls/batch", json=payload)
        data = body.get("data") or {}
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls") or []
        if not batch_id or len(upload_urls) != len(valid):
            raise MineruAPIError("MinerU returned an incomplete upload-url response")

        failures: list[Exception] = []
        max_workers = max(1, min(upload_concurrency, len(valid)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._upload_file, record.path, url)
                for record, url in zip(valid, upload_urls, strict=True)
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover - exact network errors vary
                    failures.append(exc)
        if failures:
            raise MineruAPIError(f"{len(failures)} file upload(s) failed") from failures[0]
        return BatchSubmission(batch_id=batch_id, records=valid, upload_urls=list(upload_urls))

    def submit_file(
        self,
        record: SourceRecord,
        options: ParseOptions | None = None,
    ) -> BatchSubmission:
        return self.submit_batch([record], options=options, upload_concurrency=1)

    def _upload_file(self, path: Path, signed_url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                with path.open("rb") as handle:
                    # The signed URL authenticates the upload itself. Do not add
                    # the MinerU Bearer token to this request.
                    response = self.session.put(signed_url, data=handle, timeout=self.request_timeout)
                if 200 <= response.status_code < 300:
                    return
                if response.status_code not in {408, 409, 425, 429} and response.status_code < 500:
                    raise MineruAPIError(f"signed upload returned HTTP {response.status_code}", response.status_code)
                last_error = MineruAPIError(f"signed upload returned HTTP {response.status_code}", response.status_code)
            except (OSError, requests.RequestException, MineruAPIError) as exc:
                last_error = exc
            if attempt + 1 < self.retry_attempts:
                self.sleep_fn(min(2**attempt, 10))
        raise MineruAPIError("signed upload failed after retries") from last_error

    @staticmethod
    def _extract_results(body: dict[str, Any]) -> list[dict[str, Any]]:
        data = body.get("data") or {}
        results = data.get("extract_result", [])
        if isinstance(results, dict):
            return [results]
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    def poll_batch(
        self,
        batch_id: str,
        timeout_seconds: float = 1800,
        interval_seconds: float = 3,
        max_interval_seconds: float = 30,
        progress_callback: TaskProgressCallback | None = None,
    ) -> list[TaskResult]:
        started = time.monotonic()
        interval = max(0.1, interval_seconds)
        terminal = {"done", "failed"}
        latest: list[TaskResult] = []
        while True:
            body = self._request_json("GET", f"/api/v4/extract-results/batch/{batch_id}")
            latest = [
                TaskResult(
                    file_name=str(item.get("file_name") or ""),
                    state=str(item.get("state") or "unknown"),
                    full_zip_url=item.get("full_zip_url"),
                    data_id=item.get("data_id"),
                    task_id=item.get("task_id"),
                    error=item.get("err_msg"),
                    progress=item.get("extract_progress"),
                )
                for item in self._extract_results(body)
            ]
            if progress_callback:
                progress_callback(latest)
            if latest and all(item.state in terminal for item in latest):
                return latest
            if time.monotonic() - started >= timeout_seconds:
                raise MineruAPIError(f"timed out while waiting for batch {batch_id}")
            self.sleep_fn(interval)
            interval = min(max_interval_seconds, interval * 1.5)

    def submit_url(self, url: str, options: ParseOptions | None = None) -> str:
        options = options or ParseOptions()
        payload = {"url": url, **options.to_payload()}
        body = self._request_json("POST", "/api/v4/extract/task", json=payload)
        task_id = (body.get("data") or {}).get("task_id")
        if not task_id:
            raise MineruAPIError("MinerU did not return a task_id for URL submission")
        return str(task_id)

    def poll_task(
        self,
        task_id: str,
        timeout_seconds: float = 1800,
        interval_seconds: float = 3,
        max_interval_seconds: float = 30,
        progress_callback: TaskProgressCallback | None = None,
    ) -> TaskResult:
        started = time.monotonic()
        interval = max(0.1, interval_seconds)
        while True:
            body = self._request_json("GET", f"/api/v4/extract/task/{task_id}")
            data = body.get("data") or {}
            result = TaskResult(
                file_name=str(data.get("file_name") or ""),
                state=str(data.get("state") or "unknown"),
                full_zip_url=data.get("full_zip_url"),
                task_id=task_id,
                error=data.get("err_msg"),
                progress=data.get("extract_progress"),
            )
            if progress_callback:
                progress_callback([result])
            if result.state in {"done", "failed"}:
                return result
            if time.monotonic() - started >= timeout_seconds:
                raise MineruAPIError(f"timed out while waiting for task {task_id}")
            self.sleep_fn(interval)
            interval = min(max_interval_seconds, interval * 1.5)

    def wait(
        self,
        *,
        batch_id: str | None = None,
        task_id: str | None = None,
        timeout_seconds: float = 1800,
        interval_seconds: float = 3,
        max_interval_seconds: float = 30,
        progress_callback: TaskProgressCallback | None = None,
    ) -> list[TaskResult] | TaskResult:
        """Wait for either a batch upload job or a URL task to finish.

        Keeping this small dispatcher on the public client makes the normal
        workflow uniform while retaining the explicit ``poll_batch`` and
        ``poll_task`` methods for callers that need them.
        """

        if bool(batch_id) == bool(task_id):
            raise ValueError("provide exactly one of batch_id or task_id")
        if batch_id:
            return self.poll_batch(
                batch_id,
                timeout_seconds=timeout_seconds,
                interval_seconds=interval_seconds,
                max_interval_seconds=max_interval_seconds,
                progress_callback=progress_callback,
            )
        return self.poll_task(
            str(task_id),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            max_interval_seconds=max_interval_seconds,
            progress_callback=progress_callback,
        )

    def download_zip(self, url: str, destination: Path) -> Path:
        """Download a result ZIP without logging its signed URL."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                # A full signed URL is sufficient for the download; do not
                # send the API token to the object-storage host.
                response = self.session.get(url, stream=True, timeout=self.request_timeout)
                if response.status_code >= 400:
                    raise MineruAPIError(f"result download returned HTTP {response.status_code}", response.status_code)
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                temporary.replace(destination)
                return destination
            except (OSError, requests.RequestException, MineruAPIError) as exc:
                last_error = exc
            if attempt + 1 < self.retry_attempts:
                self.sleep_fn(min(2**attempt, 10))
        raise MineruAPIError("result ZIP download failed after retries") from last_error

    def download_result(self, result_or_url: TaskResult | str, destination: Path) -> Path:
        """Download a completed result without exposing its signed URL."""

        url = result_or_url if isinstance(result_or_url, str) else result_or_url.full_zip_url
        if not url:
            raise MineruAPIError("MinerU result has no full_zip_url")
        return self.download_zip(url, destination)

    @staticmethod
    def normalize_bundle(
        zip_path: Path,
        output_dir: Path,
        *,
        markdown_name: str = "full.md",
        assets_name: str = "assets",
        retain_raw: bool = True,
    ) -> dict[str, object]:
        """Normalize a downloaded MinerU ZIP into Markdown and local assets."""

        from .bundle import normalize_bundle

        return normalize_bundle(
            zip_path,
            output_dir,
            markdown_name=markdown_name,
            assets_name=assets_name,
            retain_raw=retain_raw,
        )

    @staticmethod
    def import_to_obsidian(
        result_dir: Path,
        vault: Path,
        target_dir: str | Path,
        *,
        note_name: str | None = None,
        apply: bool = False,
        overwrite: bool = False,
        backup_root: Path = Path("backups"),
    ) -> object:
        """Create an Obsidian import plan and optionally execute it.

        ``apply=False`` is deliberately the default so callers must make the
        destination and conflict review explicit before writing the vault.
        """

        from .obsidian import create_import_plan, execute_import

        plan = create_import_plan(result_dir, vault, target_dir, note_name=note_name)
        if apply:
            execute_import(plan, overwrite=overwrite, backup_root=backup_root)
        return plan
