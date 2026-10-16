"""Run-local denial and cooldown handling for the three collection clients.

No configuration or persistent provider policy is changed. A denial aborts the
run; a cooldown skips affected origins and conservatively unresolved DOI
resolvers during publisher cooldowns, until a later independent invocation.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import email.utils
import functools
import math
import re
import threading
import urllib.error
import urllib.parse


class NetworkAccessDenied(RuntimeError):
    """Terminal access failure: never retry or try a fallback in this run."""

    def __init__(self):
        super().__init__("Collection stopped: network access denied; no automatic retry or fallback")


class ProviderTemporarilyUnavailable(RuntimeError):
    """A provider cannot be queried again yet."""


class ProviderDeferred(ProviderTemporarilyUnavailable):
    """An origin is unavailable for the remainder of this collection run."""

    def __init__(self, delay: float, *, conservative_resolver=False):
        self.delay = delay
        self.conservative_resolver = conservative_resolver
        detail = "; unresolved DOI resolver deferred during publisher cooldown" if conservative_resolver else ""
        super().__init__(f"Provider deferred for this run (retry delay {delay:g} seconds{detail})")


def _causes(error):
    pending = [error]; seen = set()
    while pending and len(seen) < 16:
        value = pending.pop(0)
        if id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        for name in ("reason", "__cause__", "__context__"):
            child = getattr(value, name, None)
            if isinstance(child, (BaseException, str)):
                pending.append(child)


_DENIAL = re.compile(
    r"\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|status(?:\s+code)?|tunnel connection failed)"
    r"\s*[:=]?\s*(?:401|403|407)\b|"
    r"\b(?:401\s+unauthorized|403\s+forbidden|407\s+proxy|unauthori[sz]ed|forbidden|"
    r"(?:access|permission)\s+(?:is\s+)?denied|proxy authentication required|"
    r"network access\s+(?:is\s+)?(?:disabled|blocked|denied|not allowed)|"
    r"blocked by (?:a |the )?(?:network |security |access )?policy)\b", re.IGNORECASE,
)


def _status(cause):
    for name in ("code", "status", "status_code"):
        value = getattr(cause, name, None)
        if type(value) is int and 100 <= value <= 599:
            return value
    return None


def access_denied(error) -> bool:
    for cause in _causes(error):
        if isinstance(cause, NetworkAccessDenied) or _status(cause) in (401, 403, 407):
            return True
        if isinstance(cause, (str, OSError, urllib.error.URLError)) and _DENIAL.search(str(cause)[:4096]):
            return True
    return False


def http_status(error):
    for cause in _causes(error):
        status = _status(cause)
        if status is not None:
            return status
    return None


def transient_error(error) -> bool:
    status = http_status(error)
    return not access_denied(error) and (
        status in (408, 429, 500, 502, 503, 504)
        or (status is None and isinstance(error, (urllib.error.URLError, TimeoutError)))
    )


def retry_after_seconds(error, *, now=None):
    """Parse finite seconds or an HTTP date, without retaining raw header text."""
    for cause in _causes(error):
        headers = getattr(cause, "headers", None)
        raw = (headers.get("Retry-After") or headers.get("retry-after")) if headers is not None else None
        if raw is None:
            continue
        if not isinstance(raw, str) or len(raw) > 128:
            return None
        raw = raw.strip()
        try:
            delay = float(raw)
        except (ValueError, OverflowError):
            try:
                when = email.utils.parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                delay = max(0.0, (when - (now or dt.datetime.now(dt.timezone.utc))).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None
        return delay if math.isfinite(delay) and delay >= 0 else None
    return None


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80))


class CollectionNetworkState:
    def __init__(self):
        self.denied = False
        self.deferred = {}
        self.resolver_origins = set()
        self._lock = threading.Lock()

    def raise_if_denied(self):
        with self._lock:
            if self.denied:
                raise NetworkAccessDenied() from None

    def check(self, url):
        with self._lock:
            if self.denied:
                raise NetworkAccessDenied() from None
            origin = _origin(url)
            delay = self.deferred.get(origin)
            if delay is not None:
                raise ProviderDeferred(delay, conservative_resolver=origin in self.resolver_origins) from None

    def reject_denial(self, error):
        if access_denied(error):
            with self._lock:
                self.denied = True
            raise NetworkAccessDenied() from None

    def defer(self, url, delay, error=None, *, publisher=False):
        requested = _origin(url)
        origins = {requested}
        resolvers = set()
        if publisher or requested[1] in {"doi.org", "dx.doi.org", "www.doi.org"}:
            # An unresolved DOI could redirect into the cooling publisher, even
            # when the first rate-limited request used a direct publisher URL.
            resolvers = {(scheme, host, port)
                         for scheme, port in (("http", 80), ("https", 443))
                         for host in ("doi.org", "dx.doi.org", "www.doi.org")}
            origins.update(resolvers)
        # urlopen follows redirects internally. Gate the final response origin
        # AND the requested resolver origin: without a pre-follow hook, another
        # DOI link could redirect into the same cooling publisher. This is a
        # conservative run-only resolver deferral, not a permanent publisher ban.
        for cause in _causes(error):
            if isinstance(cause, urllib.error.HTTPError):
                try:
                    final = _origin(cause.geturl())
                    if final[0] in ("http", "https") and final[1]:
                        origins.add(final)
                except (TypeError, ValueError):
                    pass
        with self._lock:
            self.resolver_origins.update(resolvers)
            for origin in origins:
                self.deferred[origin] = max(self.deferred.get(origin, 0.0), delay)
        return ProviderDeferred(delay, conservative_resolver=requested in resolvers)


_STATE = contextvars.ContextVar("collection_network_state", default=None)


def current_network_state():
    return _STATE.get() or CollectionNetworkState()


@contextlib.contextmanager
def collection_network_scope():
    """Nested stages share one gate; unrelated runs always start fresh."""
    if _STATE.get() is not None:
        yield _STATE.get()
        return
    state = CollectionNetworkState()
    token = _STATE.set(state)
    try:
        yield state
    finally:
        _STATE.reset(token)


def collection_network_run(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with collection_network_scope() as state:
            try:
                return function(*args, **kwargs)
            finally:
                # A parallel worker's denial must win over an earlier future's
                # cooldown/error, even if that worker's future was never read.
                state.raise_if_denied()
    return wrapped
