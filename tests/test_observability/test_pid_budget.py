"""Tests for the PID-budget reader — walk-from-self across the cgroup chain.

A fork fails when ANY ancestor cgroup hits its ``pids.max`` (kernel cgroup-v2:
nested limits only restrict further, enforced across the subtree). So reading
only the user slice is a blind spot — an uncapped slice with a binding ancestor,
a bound service-scope BELOW the slice, or a busy shared container root can each
precede ``Cannot fork`` while the slice reads green. ``_collect_pid_budget``
walks from this process's own cgroup (``/proc/self/cgroup``) up to the cgroup
mount root and reports the highest-utilization finite level + a ``scope`` label.
"""

from __future__ import annotations

import importlib

from genesis.observability.snapshots.infrastructure import (
    _cgroup_path_from_proc,
    _collect_pid_budget,
    _pid_status,
)

# The `snapshots` package re-exports the `infrastructure` FUNCTION, shadowing the
# submodule for attribute access — so grab the real module object for monkeypatching.
infra = importlib.import_module("genesis.observability.snapshots.infrastructure")


def _mk(d, current: str, maximum: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pids.current").write_text(current)
    (d / "pids.max").write_text(maximum)


class TestPidStatus:
    def test_none_is_healthy(self):
        assert _pid_status(None) == "healthy"

    def test_thresholds(self):
        assert _pid_status(0.0) == "healthy"
        assert _pid_status(41.7) == "healthy"
        assert _pid_status(79.9) == "healthy"
        assert _pid_status(80.0) == "degraded"
        assert _pid_status(89.9) == "degraded"
        assert _pid_status(90.0) == "error"
        assert _pid_status(100.0) == "error"


class TestCgroupPathFromProc:
    def test_unified_line(self):
        out = _cgroup_path_from_proc(
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/genesis-server.service\n"
        )
        assert out == (
            "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
            "/app.slice/genesis-server.service"
        )

    def test_root(self):
        assert _cgroup_path_from_proc("0::/\n") == "/sys/fs/cgroup"

    def test_deleted_zombie_suffix_stripped(self):
        assert _cgroup_path_from_proc("0::/foo/bar (deleted)\n") == "/sys/fs/cgroup/foo/bar"

    def test_hybrid_v1_no_unified_line_is_none(self):
        # Legacy/hybrid v1: named controller lines, no '0::' unified line.
        assert _cgroup_path_from_proc("1:name=systemd:/user.slice\n2:pids:/user.slice\n") is None

    def test_garbage_is_none(self):
        assert _cgroup_path_from_proc("not a cgroup file") is None

    def test_malformed_unified_relpath_is_none(self):
        # A '0::' line whose path is not absolute → refuse (fall back to slice).
        assert _cgroup_path_from_proc("0::relative/path\n") is None


class TestWalkFromSelf:
    """base_dir overrides the leaf; the walk climbs its parent dirs. Monkeypatch
    the cgroup mount root to the tmp top so the boundary + 'container-root' label
    are exercised without touching the real /sys/fs/cgroup."""

    def _chain(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        slice_ = root / "user-1000.slice"
        svc = slice_ / "genesis-server.service"
        monkeypatch.setattr(infra, "_CGROUP_ROOT", str(root))
        return root, slice_, svc

    def test_single_finite_leaf(self, tmp_path, monkeypatch):
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "9999", "max")  # uncapped root
        _mk(slice_, "9999", "max")  # uncapped slice
        _mk(svc, "1000", "2400")  # only the leaf is capped
        out = _collect_pid_budget(str(svc))
        assert out == {
            "status": "healthy",
            "current": 1000,
            "max": 2400,
            "pct": 41.7,
            "scope": "genesis-server.service",
        }

    def test_leaf_max_finite_ancestor_binds(self, tmp_path, monkeypatch):
        # THE BUG FIX: leaf uncapped ('max'), but a finite ancestor binds → must
        # report the ancestor, NOT a false healthy/None.
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "500", "max")
        _mk(slice_, "2000", "2400")  # 83% → degraded
        _mk(svc, "150", "max")  # leaf has no sub-cap
        out = _collect_pid_budget(str(svc))
        assert out["status"] == "degraded"
        assert out["pct"] == 83.3
        assert out["scope"] == "user-1000.slice"
        assert out["max"] == 2400

    def test_binding_is_highest_pct(self, tmp_path, monkeypatch):
        # Binding = the level closest to its own ceiling (highest %).
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "400", "4000")  # 10%
        _mk(slice_, "2000", "2400")  # 83.3% ← nearest its ceiling
        _mk(svc, "100", "600")  # 16.7%
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "user-1000.slice"
        assert out["pct"] == 83.3
        assert out["status"] == "degraded"

    def test_leaf_binds_over_lower_ancestors(self, tmp_path, monkeypatch):
        # A service-scope git-burst: the leaf's own 600 cap is the binding one.
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "1000", "4000")  # 25%
        _mk(slice_, "1000", "2400")  # 42%
        _mk(svc, "552", "600")  # 92% → error, binds
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "genesis-server.service"
        assert out["status"] == "error"
        assert out["pct"] == 92.0

    def test_container_root_scope_label(self, tmp_path, monkeypatch):
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "3800", "4000")  # 95% ← the container root binds
        _mk(slice_, "100", "2400")
        _mk(svc, "50", "600")
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "container-root"
        assert out["status"] == "error"

    def test_saturated_ancestor_not_masked(self, tmp_path, monkeypatch):
        # THE false-green class: a lightly-loaded inner scope must NOT mask a
        # saturated ancestor. svc 400/600 (67%, healthy alone) but container-root at
        # 95% → the status MUST be 'error' and name the root, never a false healthy.
        # (Asserts STATUS explicitly — the gap that hid the bug before.)
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "3800", "4000")  # 95% ← saturated
        _mk(slice_, "100", "2400")  # 4%
        _mk(svc, "400", "600")  # 66.7%
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "container-root"
        assert out["pct"] == 95.0
        assert out["status"] == "error"

    def test_reports_most_full_level(self, tmp_path, monkeypatch):
        # The level nearest its own ceiling is reported (closest to Cannot fork):
        # slice 2250/2400 (94%) over service 500/600 (83%).
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "1000", "max")
        _mk(slice_, "2250", "2400")  # 93.8% ← nearest its ceiling
        _mk(svc, "500", "600")  # 83.3%
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "user-1000.slice"
        assert out["pct"] == 93.8
        assert out["status"] == "error"

    def test_pct_tie_prefers_innermost(self, tmp_path, monkeypatch):
        # Equal % → the innermost level wins (max() keeps the first, leaf-first).
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "2000", "4000")  # 50%
        _mk(slice_, "1200", "2400")  # 50%
        _mk(svc, "300", "600")  # 50% ← innermost wins the tie
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "genesis-server.service"
        assert out["pct"] == 50.0

    def test_uncapped_chain_healthy_no_pct(self, tmp_path, monkeypatch):
        # Entire visible chain uncapped → genuinely no sub-cap. (The LXC host cap
        # above our visible root is unmonitorable; we surface, never false-close.)
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "500", "max")
        _mk(slice_, "300", "max")
        _mk(svc, "150", "max")
        out = _collect_pid_budget(str(svc))
        assert out == {"status": "healthy", "current": 150, "max": "max", "pct": None}

    def test_missing_files_unavailable(self, tmp_path):
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "unavailable"}

    def test_zero_max_unavailable(self, tmp_path):
        _mk(tmp_path, "5\n", "0\n")
        assert _collect_pid_budget(str(tmp_path)) == {"status": "unavailable"}

    def test_malformed_current_unavailable(self, tmp_path):
        _mk(tmp_path, "notanint\n", "2400\n")
        assert _collect_pid_budget(str(tmp_path)) == {"status": "unavailable"}

    def test_malformed_max_unavailable(self, tmp_path):
        _mk(tmp_path, "5\n", "notanint\n")
        assert _collect_pid_budget(str(tmp_path)) == {"status": "unavailable"}

    def test_ancestor_finite_max_unreadable_current_is_unavailable(self, tmp_path, monkeypatch):
        # An ancestor with a finite pids.max but an unreadable pids.current has an
        # UNKNOWABLE % — it might be the saturated binding level, so the whole read
        # must fail to 'unavailable', never mask it behind the lightly-loaded leaf
        # (the residual false-green the convergence audit flagged). Distinct from a
        # level with NO pids.max at all (that one is correctly skipped, below).
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "500", "max")
        slice_.mkdir(parents=True, exist_ok=True)
        (slice_ / "pids.max").write_text("2400\n")  # finite cap, but NO pids.current file
        _mk(svc, "100", "600")  # leaf lightly loaded (17%) — must NOT be reported
        assert _collect_pid_budget(str(svc)) == {"status": "unavailable"}

    def test_unreadable_ancestor_skipped(self, tmp_path, monkeypatch):
        # A mid-chain dir with no pids files must not abort the walk — the binding
        # ancestor above it is still found.
        root, slice_, svc = self._chain(tmp_path, monkeypatch)
        _mk(root, "3800", "4000")  # 95% binds
        (slice_).mkdir(parents=True, exist_ok=True)  # no pids.* here
        _mk(svc, "100", "600")
        out = _collect_pid_budget(str(svc))
        assert out["scope"] == "container-root"
        assert out["status"] == "error"


def test_shape_on_real_box():
    # Live call (no base_dir): reads /proc/self/cgroup; always a valid status,
    # never raises, even where the cgroup path is absent.
    out = _collect_pid_budget()
    assert out["status"] in {"healthy", "degraded", "error", "unavailable"}
