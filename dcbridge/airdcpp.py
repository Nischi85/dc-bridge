"""AirDC++ JSON-RPC client."""
from __future__ import annotations
import asyncio
import logging
from typing import Any, Optional
import httpx
from dcbridge.config import AirDCPPCfg
log = logging.getLogger("dc_bridge")
from dcbridge.helpers import _safe_json, _truncate  # noqa: E402


# ── AirDC++ client ───────────────────────────────────────────────────────────


class AirDCPP:
    """Thin async client for the AirDC++ webclient v1 API.

    NOTE: /api/v1/auto_search is NOT available in this AirDC++ build (404). We
    therefore implement an equivalent in this bridge: periodically POST to
    /api/v1/search to drive a manual search and queue any matching results via
    /api/v1/queue/bundles. Search and queue request shapes are validated live
    on first use; on a 4xx the bridge logs the raw response so we can iterate.
    """

    def __init__(self, cfg: AirDCPPCfg):
        self.cfg = cfg
        self.client = httpx.AsyncClient(base_url=cfg.url, timeout=30.0)
        self._token: Optional[str] = None
        self._session_id: Optional[int] = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _login(self) -> None:
        r = await self.client.post(
            "/api/v1/sessions/authorize",
            json={"username": self.cfg.username, "password": self.cfg.password},
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["auth_token"]
        self._session_id = d.get("session_id")
        self.client.headers["Authorization"] = f"Bearer {self._token}"
        log.info(
            "airdcpp: authenticated as %s (client %s)",
            d["user"]["username"],
            d.get("system_info", {}).get("client_version", "?"),
        )
        # AirDC++ mints a NEW session on every login and our token times out
        # between 15-min sweeps, so without cleanup these pile up until the
        # server's session cap is hit — after which authorize itself returns
        # 401. Drop our own leftover sessions (scoped to this bot account so a
        # human admin login is never touched), keeping the one we just made.
        await self._purge_my_other_sessions()

    async def _purge_my_other_sessions(self) -> None:
        try:
            r = await self.client.get("/api/v1/sessions")
            if r.status_code != 200:
                return
            for s in r.json():
                u = s.get("user") or {}
                uname = u.get("username") if isinstance(u, dict) else u
                sid = s.get("id")
                if uname == self.cfg.username and sid is not None and sid != self._session_id:
                    try:
                        await self.client.delete(f"/api/v1/sessions/{sid}")
                    except Exception:
                        log.debug("airdcpp: could not delete stale session %s", sid)
        except Exception:
            log.debug("airdcpp: session purge skipped (non-fatal)")

    async def ensure_auth(self) -> None:
        if self._token is None:
            async with self._lock:
                if self._token is None:
                    await self._login()

    async def _retry_on_401(self, method: str, path: str, **kw) -> httpx.Response:
        await self.ensure_auth()
        r = await self.client.request(method, path, **kw)
        if r.status_code == 401:
            token_before = self._token
            async with self._lock:
                # Only the first coroutine to see the stale token re-logs-in;
                # the rest reuse the new one. Prevents a burst of concurrent
                # 401s from spawning a login storm (and leaked sessions).
                if self._token == token_before:
                    log.info("airdcpp: token rejected, re-authenticating")
                    await self._login()
            r = await self.client.request(method, path, **kw)
        return r

    # AirDC++ v1 search is a 3-step flow:
    #   1) POST /api/v1/search                          -> create instance, returns id
    #   2) POST /api/v1/search/{id}/hub_search          -> seed query, fan out to hubs
    #   3) GET  /api/v1/search/{id}/results/{start}/{count}  -> read results back
    # Then optionally:
    #   4) POST /api/v1/search/{id}/results/{tth}/download   body: {"target_directory": "Z:\\..."}
    #   5) DELETE /api/v1/search/{id}                   -> cleanup
    #
    # Results stream in over a few seconds as each hub responds; the bridge
    # waits a fixed window then snapshots the result set. Live extension via
    # the WebSocket `search_result_added` event is possible but unnecessary here.

    async def create_search_instance(self) -> Optional[int]:
        r = await self._retry_on_401("POST", "/api/v1/search", json={})
        if r.status_code != 200:
            log.warning("airdcpp: create instance -> %s %s", r.status_code, _truncate(r.text))
            return None
        return r.json().get("id")

    async def hub_search(
        self,
        instance_id: int,
        pattern: str,
        extensions: Optional[list[str]] = None,
    ) -> bool:
        body: dict[str, Any] = {"query": {"pattern": pattern}}
        if extensions:
            body["query"]["extensions"] = list(extensions)
        if self.cfg.hub_urls:
            body["hub_urls"] = self.cfg.hub_urls
        r = await self._retry_on_401(
            "POST", f"/api/v1/search/{instance_id}/hub_search", json=body
        )
        if r.status_code != 200:
            log.warning("airdcpp: hub_search %r -> %s %s", pattern, r.status_code, _truncate(r.text))
            return False
        return True

    async def get_results(self, instance_id: int, start: int = 0, count: int = 100) -> list[dict]:
        r = await self._retry_on_401(
            "GET", f"/api/v1/search/{instance_id}/results/{start}/{count}"
        )
        if r.status_code != 200:
            log.warning(
                "airdcpp: get_results %s -> %s %s",
                instance_id,
                r.status_code,
                _truncate(r.text),
            )
            return []
        body = _safe_json(r)
        return body if isinstance(body, list) else []

    async def list_dir(self, smb_path: str) -> Optional[list[dict]]:
        """List a directory on the AirDC++ host (it can see where it downloads).
        Returns the item list ([] if the path is missing/invalid → 400), or None
        on any other error so callers can avoid acting on a transient failure."""
        r = await self._retry_on_401(
            "POST", "/api/v1/filesystem/list_items", json={"path": smb_path}
        )
        if r.status_code == 200:
            body = _safe_json(r)
            return body if isinstance(body, list) else []
        if r.status_code == 400:
            return []  # path does not exist
        log.debug("airdcpp: list_dir %r -> %s", smb_path, r.status_code)
        return None

    async def list_bundles(self, start: int = 0, count: int = 200) -> Optional[list[dict]]:
        """List queue bundles (downloads). None on error."""
        r = await self._retry_on_401("GET", f"/api/v1/queue/bundles/{start}/{count}")
        if r.status_code == 200:
            body = _safe_json(r)
            return body if isinstance(body, list) else []
        log.warning("airdcpp: list_bundles -> %s %s", r.status_code, _truncate(r.text))
        return None

    async def remove_bundle(self, bundle_id: int, remove_finished: bool = False) -> bool:
        """Remove a bundle from the queue. remove_finished=True also deletes the
        downloaded files (used to cancel a wrong grab)."""
        r = await self._retry_on_401(
            "POST", f"/api/v1/queue/bundles/{bundle_id}/remove",
            json={"remove_finished": remove_finished},
        )
        if r.status_code in (200, 204):
            return True
        log.warning("airdcpp: remove_bundle %s -> %s %s", bundle_id, r.status_code, _truncate(r.text))
        return False

    async def queue_result(
        self, instance_id: int, result_tth: str, target_smb_dir: str
    ) -> Optional[dict]:
        # target_smb_dir MUST end with a backslash, otherwise AirDC++ treats it
        # as a path prefix and concatenates the filename onto the trailing path
        # segment (leaking the file into the SMB share root). Enforce defensively.
        if not target_smb_dir.endswith("\\"):
            target_smb_dir = target_smb_dir + "\\"
        body = {"target_directory": target_smb_dir}
        r = await self._retry_on_401(
            "POST",
            f"/api/v1/search/{instance_id}/results/{result_tth}/download",
            json=body,
        )
        if r.status_code != 200:
            log.warning(
                "airdcpp: queue_result tth=%s dir=%r -> %s %s",
                result_tth,
                target_smb_dir,
                r.status_code,
                _truncate(r.text),
            )
            return None
        return _safe_json(r)

    async def delete_instance(self, instance_id: int) -> None:
        await self._retry_on_401("DELETE", f"/api/v1/search/{instance_id}")


