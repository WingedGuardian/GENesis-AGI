"""Proxmox VE provisioning adapter (stdlib urllib, never-raise).

Talks to the PVE API (``/api2/json``) with two privilege-separated tokens:
an AUDIT token for all reads (get_capacity, connectivity, verify re-reads) and
a PROVISION token — scoped to VM.Config.Disk/Memory on this one VM only — for
the two mutating PUTs. Code never parses the token's user/realm; the whole
token string goes into the ``PVEAPIToken=`` header verbatim.

Response shapes were captured live from PVE 9.1.4 (2026-07-06):
- ``/nodes/N/status`` → ``.data.memory{total,free,used,available}``, ``.data.cpuinfo.cpus``
- ``/nodes/N/storage`` → ``.data[]{storage,total,avail,used}``
- ``/nodes/N/qemu/V/config`` → ``.data{name,cores,memory:"21500",scsi1:"...,size=32G"}``
- writes: ``PUT /nodes/N/qemu/V/resize`` (disk,size=+NG) and
  ``PUT /nodes/N/qemu/V/config`` (memory=N); form-encoded bodies (PVE wants form).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from genesis.guardian.config import ProvisioningConfig
from genesis.guardian.provisioning.base import (
    HostCapacity,
    ProvisioningAdapter,
    ProvisionResult,
)

logger = logging.getLogger(__name__)

_GIB = 1024**3
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGTP]?)$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": _GIB, "T": 1024**4, "P": 1024**5}


def _parse_size_to_bytes(size_str: str) -> int | None:
    """Parse a PVE size token ('32G', '32768M', '512K', plain bytes) to bytes."""
    m = _SIZE_RE.match(size_str.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])


def _human(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "unknown"
    if num_bytes >= _GIB:
        return f"{num_bytes / _GIB:.1f}G"
    return f"{num_bytes / 1024**2:.0f}M"


class ProxmoxAdapter(ProvisioningAdapter):
    """PVE provisioning over urllib. Every public method is never-raise."""

    def __init__(
        self,
        config: ProvisioningConfig,
        audit_token: str,
        provision_token: str,
        request_timeout: float = 30.0,
    ) -> None:
        self._config = config
        self._audit = audit_token
        self._provision = provision_token
        self._timeout = request_timeout
        self._base = (
            f"https://{config.api_host}:{config.api_port}/api2/json"
        )
        if config.verify_tls:
            self._ctx: ssl.SSLContext | None = None  # default verification
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ctx = ctx

    # ── transport ────────────────────────────────────────────────────────
    @staticmethod
    def _auth_header(token: str) -> str:
        token = token.strip()
        if token.startswith("PVEAPIToken="):
            return token
        return f"PVEAPIToken={token}"

    def _request_sync(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        token: str = "",
    ) -> tuple[int, Any, str]:
        """Blocking HTTP. Returns (status, envelope['data'], error).

        status -1 = transport error (never raised). On non-2xx, data is None
        and error carries the code + body prefix.
        """
        url = f"{self._base}{path}"
        headers = {"Authorization": self._auth_header(token)}
        body: bytes | None = None
        if method in ("PUT", "POST") and params:
            body = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif method == "GET" and params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(
                req, timeout=self._timeout, context=self._ctx,
            ) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                envelope = json.loads(raw) if raw.strip() else {}
                data = envelope.get("data") if isinstance(envelope, dict) else None
                return resp.status, data, ""
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            return exc.code, None, f"HTTP {exc.code}: {err_body[:200]}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return -1, None, str(exc)
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            logger.warning("Proxmox request unexpected error: %s", exc, exc_info=True)
            return -1, None, f"unexpected: {exc}"

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        token: str = "",
    ) -> tuple[int, Any, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._request_sync, method, path, params, token,
        )

    async def _await_task(
        self, upid: str, timeout: float = 120.0, interval: float = 2.0,
    ) -> tuple[bool, str]:
        """Poll a PVE task UPID to completion. Returns (ok, exitstatus).

        A resize (and many PVE mutations) return HTTP 200 with a ``UPID:`` task
        string in ``data``; the actual work runs as a background worker that can
        FAIL *after* the 200 (e.g. a Datastore.AllocateSpace permission error
        surfaces only in the worker). We poll
        ``GET /nodes/N/tasks/{upid}/status`` (audit token — it has Sys.Audit)
        until ``status == "stopped"``, then map ``exitstatus == "OK"`` → ok.

        Never raises. On poll timeout or a persistent read failure we return
        ``(False, <reason>)`` so the caller treats the mutation as UNVERIFIED
        (and never auto-retries) rather than as a silent success. The ~120s
        bound (poll count × interval) is a raw external poll with no other
        watchdog: a resize completes in seconds, so it only guards a
        pathologically slow/hung task worker or status endpoint from blocking
        the (already bounded) provisioning flow.
        """
        cfg = self._config
        encoded = urllib.parse.quote(upid, safe="")
        path = f"/nodes/{cfg.node}/tasks/{encoded}/status"
        max_polls = max(1, int(timeout / interval))
        last_err = "task did not reach 'stopped'"
        for _ in range(max_polls):
            st, data, err = await self._request("GET", path, token=self._audit)
            if st == 200 and isinstance(data, dict):
                if data.get("status") == "stopped":
                    exitstatus = str(data.get("exitstatus") or "").strip()
                    return exitstatus == "OK", exitstatus or "no exitstatus reported"
                # still running — keep polling
            else:
                last_err = err or f"task status read failed: {st}"
            await asyncio.sleep(interval)
        return False, f"task poll timed out after ~{timeout:.0f}s ({last_err})"

    # ── parsing helpers ──────────────────────────────────────────────────
    @staticmethod
    def _disk_entries(cfg: dict[str, Any]) -> dict[str, int]:
        """Map disk-key → size bytes, skipping cdrom media and unparseable sizes."""
        out: dict[str, int] = {}
        for key, val in cfg.items():
            if not isinstance(key, str) or not _DISK_KEY.match(key):
                continue
            if not isinstance(val, str) or "media=cdrom" in val:
                continue
            for part in val.split(","):
                part = part.strip()
                if part.startswith("size="):
                    size = _parse_size_to_bytes(part[len("size="):])
                    if size is not None:
                        out[key] = size
                    break
        return out

    # ── capacity (audit token, read-only) ────────────────────────────────
    async def get_capacity(self) -> HostCapacity:
        cfg = self._config
        if not (cfg.api_host and cfg.node and cfg.vmid):
            return HostCapacity(
                detected=False, detail="provisioning not fully configured",
            )
        st1, node_status, e1 = await self._request(
            "GET", f"/nodes/{cfg.node}/status", token=self._audit,
        )
        st2, storages, e2 = await self._request(
            "GET", f"/nodes/{cfg.node}/storage", token=self._audit,
        )
        st3, vm_cfg, e3 = await self._request(
            "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config", token=self._audit,
        )
        # The VM config is the anchor; require all three (conservative — never
        # assume headroom from a partial read).
        if st3 != 200 or not isinstance(vm_cfg, dict):
            return HostCapacity(detected=False, detail=f"vm config read failed: {e3 or st3}")
        if st1 != 200 or not isinstance(node_status, dict):
            return HostCapacity(detected=False, detail=f"node status read failed: {e1 or st1}")
        if st2 != 200 or not isinstance(storages, list):
            return HostCapacity(detected=False, detail=f"storage read failed: {e2 or st2}")

        mem = node_status.get("memory") if isinstance(node_status.get("memory"), dict) else {}
        node_mem_total = mem.get("total")
        # DD finding: use available (free + reclaimable), NOT raw free.
        node_mem_available = mem.get("available")

        storage_free = storage_total = None
        for s in storages:
            if isinstance(s, dict) and s.get("storage") == cfg.storage:
                storage_free = s.get("avail")
                storage_total = s.get("total")
                break

        vm_mem_mib: int | None = None
        raw_mem = vm_cfg.get("memory")
        if raw_mem is not None:
            try:
                vm_mem_mib = int(raw_mem)
            except (TypeError, ValueError):
                vm_mem_mib = None
        cores = vm_cfg.get("cores") if isinstance(vm_cfg.get("cores"), int) else None

        return HostCapacity(
            detected=True,
            vm_memory_mib=vm_mem_mib,
            cores=cores,
            disks=self._disk_entries(vm_cfg),
            storage_free_bytes=storage_free,
            storage_total_bytes=storage_total,
            node_mem_total_bytes=node_mem_total,
            node_mem_available_bytes=node_mem_available,
            detail="ok",
        )

    async def test_connectivity(self) -> bool:
        cfg = self._config
        if not (cfg.api_host and cfg.node):
            return False
        st, data, _ = await self._request(
            "GET", f"/nodes/{cfg.node}/status", token=self._audit,
        )
        return st == 200 and isinstance(data, dict)

    # ── grow disk (provision token, verify by re-read) ───────────────────
    async def grow_vm_disk(self, disk: str, add_gib: int) -> ProvisionResult:
        action = "grow_vm_disk"
        requested = f"{disk} +{add_gib}G"
        cfg = self._config
        if add_gib <= 0:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                error="add_gib must be positive (grow-only)",
            )
        st, vm_cfg, e = await self._request(
            "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config", token=self._audit,
        )
        if st != 200 or not isinstance(vm_cfg, dict):
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                error=f"pre-read failed: {e or st}",
            )
        before = self._disk_entries(vm_cfg).get(disk)
        if before is None:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                error=f"disk {disk} not found on VM {cfg.vmid}",
            )
        expected = before + add_gib * _GIB
        # The one mutating PUT — provision token.
        stp, put_data, ep = await self._request(
            "PUT", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/resize",
            params={"disk": disk, "size": f"+{add_gib}G"}, token=self._provision,
        )
        if stp != 200:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                before=_human(before), target_bytes=expected,
                error=f"resize PUT failed: {ep or stp}",
            )
        # The resize runs as an async PVE task: the 200 above only means
        # "accepted". When data is a UPID string, await the task so a resize
        # that FAILS in the worker (e.g. a storage permission error) is
        # reported as a failure — not misread as a slow-but-pending success by
        # the config re-read below.
        if isinstance(put_data, str) and put_data.startswith("UPID:"):
            task_ok, exitstatus = await self._await_task(put_data)
            if not task_ok:
                return ProvisionResult(
                    ok=False, action=action, requested=requested,
                    before=_human(before), target_bytes=expected, verified=False,
                    error=f"resize task failed: {exitstatus}",
                )
        # Verify by re-read ONLY — never re-issue the PUT.
        after: int | None = None
        for _ in range(3):
            await asyncio.sleep(3)
            stc, vm_cfg2, _ec = await self._request(
                "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config", token=self._audit,
            )
            if stc == 200 and isinstance(vm_cfg2, dict):
                after = self._disk_entries(vm_cfg2).get(disk)
                if after is not None and after >= expected:
                    break
        verified = after is not None and after >= expected
        return ProvisionResult(
            ok=verified, action=action, requested=requested,
            before=_human(before), after=_human(after),
            verified=verified, target_bytes=expected,
            error="" if verified else "resize issued but re-read did not confirm the new size",
        )

    # ── grow memory (provision token, grow-only, may need reboot) ─────────
    async def grow_vm_memory(self, new_mib: int) -> ProvisionResult:
        action = "grow_vm_memory"
        requested = f"{new_mib}MiB"
        cfg = self._config
        st, vm_cfg, e = await self._request(
            "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config", token=self._audit,
        )
        if st != 200 or not isinstance(vm_cfg, dict):
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                error=f"pre-read failed: {e or st}",
            )
        current: int | None = None
        raw_mem = vm_cfg.get("memory")
        if raw_mem is not None:
            try:
                current = int(raw_mem)
            except (TypeError, ValueError):
                current = None
        if current is None:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                error="cannot read current VM memory",
            )
        if new_mib <= current:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                before=f"{current}MiB",
                error=f"grow-only: requested {new_mib} <= current {current} MiB",
            )
        stp, put_data, ep = await self._request(
            "PUT", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config",
            params={"memory": new_mib}, token=self._provision,
        )
        if stp != 200:
            return ProvisionResult(
                ok=False, action=action, requested=requested,
                before=f"{current}MiB",
                error=f"config PUT failed: {ep or stp}",
            )
        # A config PUT is normally synchronous (data=null), but await defensively
        # if PVE ever returns a task UPID so a failed worker isn't misread as ok.
        if isinstance(put_data, str) and put_data.startswith("UPID:"):
            task_ok, exitstatus = await self._await_task(put_data)
            if not task_ok:
                return ProvisionResult(
                    ok=False, action=action, requested=requested,
                    before=f"{current}MiB", verified=False,
                    error=f"config task failed: {exitstatus}",
                )
        # Confirm the config accepted the new value; the running VM needs a
        # reboot to actually use it (hotplug is off on this install → default
        # requires_reboot True unless /pending proves it took effect live).
        requires_reboot = True
        stq, pending, _eq = await self._request(
            "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/pending", token=self._audit,
        )
        if stq == 200 and isinstance(pending, list):
            for item in pending:
                if isinstance(item, dict) and item.get("key") == "memory":
                    # A 'pending' field differing from active 'value' = reboot needed.
                    requires_reboot = "pending" in item and str(
                        item.get("pending"),
                    ) != str(item.get("value"))
        # Verify the config now reads the new value (grow accepted).
        verified = False
        stc, vm_cfg2, _ec = await self._request(
            "GET", f"/nodes/{cfg.node}/qemu/{cfg.vmid}/config", token=self._audit,
        )
        if stc == 200 and isinstance(vm_cfg2, dict):
            try:
                verified = int(vm_cfg2.get("memory")) == new_mib
            except (TypeError, ValueError):
                verified = False
        return ProvisionResult(
            ok=verified, action=action, requested=requested,
            before=f"{current}MiB", after=f"{new_mib}MiB",
            verified=verified, requires_reboot=requires_reboot,
            error="" if verified else "memory PUT issued but re-read did not confirm",
        )
