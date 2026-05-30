"""Thin Cekura REST client for the eval pipeline.

Standalone (no Claude/MCP dependency) so the pipeline can run from a terminal or
cron. Auth is an org-scoped API key in CEKURA_API_KEY (create one at
dashboard.cekura.ai → Settings → API Keys). Using a key also avoids the OAuth
token expiry we hit repeatedly with the MCP.

Endpoints are per the Cekura test_framework v1 API. The pipecat-v2 run endpoint
and the runs-list shape mirror the MCP tools used during setup; if the hosted API
differs, adjust the two flagged spots below.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import requests

DEFAULT_BASE_URL = "https://api.cekura.ai"
TERMINAL_RESULT_STATUSES = {"completed", "failed"}


class CekuraError(RuntimeError):
    """Raised when the Cekura API returns an error or times out."""


class CekuraClient:
    """Minimal REST wrapper for triggering runs and collecting results."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        key = api_key or os.getenv("CEKURA_API_KEY")
        if not key:
            raise CekuraError(
                "CEKURA_API_KEY not set. Create an org-scoped key at "
                "dashboard.cekura.ai → Settings → API Keys and export it."
            )
        self._base = (base_url or os.getenv("CEKURA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {"X-CEKURA-API-KEY": key, "Content-Type": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._session.request(method, self._url(path), timeout=120, **kwargs)
        if not resp.ok:
            raise CekuraError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    # --- triggering -------------------------------------------------------

    def trigger_pipecat_v2(
        self, scenario_ids: list[int], frequency: int = 1, name: str | None = None
    ) -> dict:
        """Start a Pipecat-Cloud (Daily/WebRTC) run for the given scenarios.

        Returns the result payload, including the new ``id`` (result_id) and the
        per-scenario ``runs``. NOTE: pass no more scenarios than the Pipecat agent's
        max_agents, or the overflow sessions fail with
        ``pipecat-agent-concurrency-limit-reached``. The pipeline batches for you.
        """
        body: dict[str, Any] = {
            "scenarios": [{"scenario": sid} for sid in scenario_ids],
            "frequency": frequency,
        }
        if name:
            body["name"] = name
        # FLAG: verify this path against your tenant if it 404s.
        return self._request(
            "POST", "/test_framework/v1/scenarios/run_scenarios_pipecat_v2/", json=body
        )

    # --- collecting -------------------------------------------------------

    def get_result(self, result_id: int) -> dict:
        """Full result detail, including per-run ``evaluation.metrics`` and scores."""
        return self._request("GET", f"/test_framework/v1/results/{result_id}/")

    def get_runs(self, result_id: int, run_ids: list[int]) -> list[dict]:
        """Per-run detail including transcripts (``transcript_object``) and audio URLs."""
        params = {"result_id": result_id, "run_ids": ",".join(str(r) for r in run_ids)}
        # FLAG: verify this path/params against your tenant if it 404s.
        return self._request("GET", "/test_framework/v1/runs/", params=params)

    def wait_for_result(
        self,
        result_id: int,
        poll_interval_secs: int = 20,
        timeout_secs: int = 1800,
        on_tick: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll until the result is terminal or the timeout elapses.

        Returns the latest result payload regardless (a timeout returns the partial
        result rather than raising, so the caller can still report what finished).
        """
        deadline = time.time() + timeout_secs
        while True:
            result = self.get_result(result_id)
            if result.get("status") in TERMINAL_RESULT_STATUSES:
                return result
            if on_tick:
                on_tick(result)
            if time.time() >= deadline:
                return result
            time.sleep(poll_interval_secs)

    # --- prompt optimization (Phase 4b) -----------------------------------

    def improve_prompt(
        self,
        prompt: str,
        run_ids: list[int],
        category_ids: list[int] | None = None,
        workflow_metric_ids: list[int] | None = None,
    ) -> dict:
        """Ask Cekura to suggest prompt improvements from failing runs.

        Mirrors the ``runs_improve_prompt_create`` MCP tool: pass the CURRENT system
        prompt and up to 3 failing run IDs; Cekura analyzes the transcripts + metric
        failures and returns improvement suggestions (often async — poll progress).
        """
        body: dict[str, Any] = {"prompt": prompt, "run_ids": run_ids[:3]}
        if category_ids:
            body["category_ids"] = category_ids
        if workflow_metric_ids:
            body["workflow_metric_ids"] = workflow_metric_ids
        # FLAG: verify path against your tenant; mirrors the MCP improve-prompt tool.
        return self._request("POST", "/test_framework/v1/runs/improve-prompt/", json=body)

    def get_improve_prompt_progress(self) -> dict:
        """Poll the latest improve-prompt job's progress / result."""
        # FLAG: verify path against your tenant.
        return self._request("GET", "/test_framework/v1/runs/improve-prompt-progress/")
