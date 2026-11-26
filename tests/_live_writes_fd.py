"""Positive returned-FD witnesses for otherwise ambiguous relative os.open writes.

Report-only accident prevention, not an OS security boundary. Failed calls,
extra events, cached native aliases and unsupported forms retain primary evidence.
No published hit or classification fault is ever removed. Python wrapping changes
callable identity/builtin status; signature, pickle and dir_fd metadata survive.
"""

import contextlib
import functools
import inspect
import os
import stat
import sys
import threading
from types import SimpleNamespace

_CONTROL = (KeyboardInterrupt, SystemExit, GeneratorExit)
_GUESS = "relative os.open (dir_fd unknown; judged against cwd)"


class Witness:
    def __init__(self, audit):
        self.audit = audit
        self.native = audit.os.open
        self._close, self._pid = audit.os.close, audit.os.getpid
        self._readlink, self._fstat = audit.os.readlink, audit.os.fstat
        self.pid = self._pid()
        self.local = threading.local()
        self.lock = threading.RLock()
        self.active = {}
        self.barriers = 0
        self.installed = False

    def _scope(self):
        # A fork child must not touch an inherited lock or refinement scope.
        return getattr(self.local, "scope", None) if self._pid() == self.pid else None

    def invalidate(self):
        scope = self._scope()
        if scope is not None:
            scope.usable = False

    def observe(self, args):
        scope = self._scope()
        if scope is not None:
            scope.events += 1
            if scope.events == 1:
                scope.mode_none = args[1] is None
                scope.flags = args[2] if type(args[2]) is int else None

    def note(self, event, args):
        """Contain witness bookkeeping errors while primary judgment continues."""
        if event != "open":
            return
        try:
            self.observe(args)
        except _CONTROL:
            raise
        except BaseException as error:
            self.invalidate()
            self._fault(error)

    def defer(self, key):
        scope = self._scope()
        if scope is None or key[1:3] != (_GUESS, "write") or not all(type(x) is str for x in key):
            return False
        with self.lock:
            if scope.sealed:
                return False
            scope.rows.append(key)
        return True

    def _parent(self, fd):
        st = self._fstat(fd)
        path = self._readlink(f"/proc/self/fd/{fd}")
        if not stat.S_ISDIR(st.st_mode) or not path.startswith("/") or " (deleted)" in path:
            return None
        return path, st.st_dev, st.st_ino

    def _begin(self, parent, leaf):
        scope = SimpleNamespace(parent=parent, leaf=leaf, hits=self.audit._S.hits,
                                rows=[], events=0, mode_none=False, flags=None,
                                usable=True, sealed=False)
        with self.lock:
            denied = bool(self.barriers or not self.installed)
            if not denied:
                self.active[id(scope)] = scope
        if denied:
            # Primary cwd judgment can be empty for a protected dir_fd. Count
            # uncertainty before forwarding, outside the registry lock.
            self._fault()
            return None
        return scope

    def _fault(self, error=None):
        self.audit._unknown("open", "fd-witness", error if error is not None else RuntimeError())

    def _retain(self, scope, *, pending=False):
        if pending or scope.hits is not self.audit._S.hits:
            self._fault()
        if scope.hits is self.audit._S.hits:
            for row in scope.rows:
                try:
                    scope.hits[row] = None
                except _CONTROL:
                    raise
                except BaseException as error:
                    self._fault(error)

    def _publish_pending(self, scopes):
        for scope in scopes:
            self._retain(scope, pending=True)

    def capture(self, metadata):
        """Drain and capture both readers while new refinement is disabled."""
        if self._pid() != self.pid:
            return list(dict.copy(self.audit._S.hits)), metadata(self.audit._S)
        with self.lock:
            overlapping = bool(self.barriers)
            self.barriers += 1
            scopes = tuple(self.active.values())
            self.active.clear()
            for scope in scopes:
                scope.sealed = True
        try:
            if overlapping:
                self._fault()
            self._publish_pending(scopes)
            return list(dict.copy(self.audit._S.hits)), metadata(self.audit._S)
        finally:
            with self.lock:
                self.barriers -= 1
            # Keep detached scopes/references alive until outside the lock.

    def _finish(self, scope, resolved):
        with self.lock:
            # Metadata/judgment can yield after the first ownership check.
            # Commit discharge only while the captured evidence still belongs
            # to current state; unresolved scopes remain pending until retained.
            if resolved and not scope.sealed and scope.hits is self.audit._S.hits:
                self.active.pop(id(scope), None)
                return
        # Keep unresolved evidence registered until it has actually published.
        try:
            self._retain(scope)
        except _CONTROL:
            self._fault()
            raise
        finally:
            with self.lock:
                self.active.pop(id(scope), None)

    def _eligible(self, args, kwargs):
        if type(kwargs.get("dir_fd")) is not int or kwargs["dir_fd"] < 0:
            return None
        try:
            bound = self.signature.bind(*args, **kwargs).arguments
        except TypeError:
            return None  # native binding owns its exact error messages
        path, flags = bound.get("path"), bound.get("flags")
        if (type(path) not in (str, bytes) or type(flags) is not int
                or not flags & (os.O_WRONLY | os.O_RDWR)
                or flags & getattr(os, "O_PATH", 0)
                or not (flags & os.O_NOFOLLOW or flags & os.O_CREAT and flags & os.O_EXCL)):
            return None
        leaf = os.fsdecode(path)
        if not leaf or leaf in (".", "..") or "/" in leaf or "\0" in leaf:
            return None
        return bound["dir_fd"], leaf

    def _resolved(self, scope, fd, dir_fd):
        a = self.audit
        if (not scope.usable or scope.sealed or scope.events != 1 or not scope.mode_none
                or scope.flags is None or scope.hits is not a._S.hits
                or self._parent(dir_fd) != scope.parent):
            return False
        target = a._absolute(fd)
        if target != scope.parent[0].rstrip("/") + "/" + scope.leaf or " (deleted)" in target:
            return False
        old_busy = getattr(a._busy, "on", False)
        a._busy.on = True
        try:
            a._judge("open", (fd, None, scope.flags))
        finally:
            a._busy.on = old_busy
        return True

    def _open(self, args, kwargs):
        a = self.audit
        if (self._pid() != self.pid or getattr(a._busy, "on", False)
                or not self.installed):
            return self.native(*args, **kwargs)
        scope = None
        try:
            eligible = self._eligible(args, kwargs)
            if eligible is not None:
                dir_fd, leaf = eligible
                parent = self._parent(dir_fd)
                if parent is not None:
                    scope = self._begin(parent, leaf)
                else:
                    self._fault()
        except _CONTROL:
            raise
        except BaseException as error:
            self._fault(error)
        if scope is None:
            return self.native(*args, **kwargs)
        previous = self._scope()
        self.local.scope = scope
        resolved = False
        fd = None
        try:
            try:
                try:
                    fd = self.native(*args, **kwargs)
                finally:
                    # Post-open metadata/signal callbacks own their primary
                    # evidence; only the native call may donate deferred rows.
                    self.local.scope = previous
                try:
                    resolved = self._resolved(scope, fd, dir_fd)
                    if not resolved and not scope.sealed:
                        self._fault()
                except _CONTROL:
                    raise
                except BaseException as error:
                    self._fault(error)
            finally:
                self._finish(scope, resolved)
        except _CONTROL:
            if fd is not None:
                with contextlib.suppress(BaseException):
                    self._close(fd)
            raise
        return fd

    def install(self):
        try:
            self._install()
        except _CONTROL:
            raise
        except BaseException as error:
            self._fault(error)

    def _install(self):
        if self.installed:
            return
        if sys.platform != "linux" or sys.implementation.name != "cpython":
            return  # the documented limit: relative opens keep the cwd guess
        if (os.open is not self.native or not inspect.isbuiltin(self.native)
                or type(os.supports_dir_fd) is not set):
            # Supported here, but another wrapper owns os.open: refinement is
            # unavailable, so the census must not read as complete.
            self._fault()
            return
        self.signature = inspect.signature(self.native)

        @functools.wraps(self.native)
        def wrapped(*args, **kwargs):
            return self._open(args, kwargs)

        wrapped.__module__ = "os"
        self.wrapped = wrapped
        self.members = os.supports_dir_fd
        try:
            if self.native in self.members:
                self.members.add(wrapped)
            os.open = wrapped
            self.installed = True
        except BaseException:
            self.close()
            raise

    def close(self):
        self.installed = False
        try:
            self.capture(lambda state: None)
        finally:
            if hasattr(self, "wrapped"):
                if os.open is self.wrapped:
                    os.open = self.native
                self.members.discard(self.wrapped)
