"""Bounded magnet metadata probes, isolated from MoviePilot and its downloader."""

import base64
from collections import OrderedDict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore, RLock
import time
from urllib.parse import parse_qs, urlsplit


MAX_METADATA = 2 * 1024 * 1024
RESOURCE_TIMEOUT = 15
SEARCH_TIMEOUT = 30
CACHE_LIMIT = 128
CACHE_BYTES = 16 * 1024 * 1024
SUCCESS_TTL = 24 * 60 * 60
FAILURE_TTL = 5 * 60


class MetadataUnavailable(ValueError):
    """A fixed explanation safe to show without credentials or URLs."""


def magnet_key(uri):
    """Normalize equivalent hexadecimal/base32 v1 magnets and v2 hashes."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != "magnet":
        raise MetadataUnavailable("不是有效的磁力链接")
    hashes = []
    for value in parse_qs(parts.query).get("xt", []):
        try:
            if value.lower().startswith("urn:btih:"):
                raw = value[9:]
                digest = bytes.fromhex(raw) if len(raw) == 40 else base64.b32decode(raw.upper())
                if len(digest) != 20:
                    raise ValueError()
                hashes.append("v1:" + digest.hex())
            elif value.lower().startswith("urn:btmh:1220"):
                digest = bytes.fromhex(value[13:])
                if len(digest) != 32:
                    raise ValueError()
                hashes.append("v2:" + digest.hex())
        except (ValueError, TypeError):
            raise MetadataUnavailable("磁力链接的资源哈希无效") from None
    if not hashes:
        raise MetadataUnavailable("磁力链接缺少支持的资源哈希")
    if any(len({h for h in hashes if h.startswith(kind)}) > 1 for kind in ("v1:", "v2:")):
        raise MetadataUnavailable("磁力链接包含相互冲突的资源哈希")
    return "|".join(sorted(set(hashes)))


class MetadataResolver:
    """Cache raw metadata, never verdicts that depend on subscription settings."""

    def __init__(self):
        self._lock = RLock()
        self._slots = BoundedSemaphore(2)
        self._cache = OrderedDict()
        self._budgets = OrderedDict()
        self._processes = set()
        self._closed = False

    def begin_search(self, source):
        """ResourceSelection marks the start of one candidate download batch."""
        with self._lock:
            self._budgets[source] = [float(SEARCH_TIMEOUT)]
            self._budgets.move_to_end(source)
            while len(self._budgets) > CACHE_LIMIT:
                self._budgets.popitem(last=False)

    def stop(self):
        """Stop only probes created by this resolver when the plugin reloads."""
        with self._lock:
            self._closed = True
            for process in self._processes:
                if process.poll() is None:
                    process.kill()

    def resolve(self, magnet, source, *, proxy=None):
        """Fetch at most 2 MiB with a 15s resource and 30s search deadline."""
        key = magnet_key(magnet)
        with self._lock:
            if self._closed:
                raise MetadataUnavailable("磁力校验已停止")
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                self._cache.move_to_end(key)
                if cached[1] is None:
                    raise MetadataUnavailable(cached[2])
                return cached[1]
            self._cache.pop(key, None)
            # Reserve waiting time under the same lock to bound concurrent probes.
            # Refund unused time; ordinary downloads do not consume this budget.
            budget = self._budgets.get(source)
            timeout = min(RESOURCE_TIMEOUT, budget[0] if budget is not None else RESOURCE_TIMEOUT)
            if timeout <= 0:
                raise MetadataUnavailable("本次搜索的磁力校验等待已达 30 秒上限")
            if not self._slots.acquire(blocking=False):
                raise MetadataUnavailable("磁力校验并发已达上限，本资源暂未校验")
            if budget is not None:
                budget[0] -= timeout
        started = time.monotonic()
        try:
            content = self._probe(magnet, timeout, proxy)
        except MetadataUnavailable as error:
            self._remember(key, None, str(error), FAILURE_TTL)
            raise
        finally:
            if budget is not None:
                with self._lock:
                    budget[0] += max(0, timeout - (time.monotonic() - started))
            self._slots.release()
        self._remember(key, content, "", SUCCESS_TTL)
        return content

    def _remember(self, key, content, reason, ttl):
        """Limit both successful metadata and short-lived failure cache entries."""
        with self._lock:
            self._cache[key] = (time.monotonic() + ttl, content, reason)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_LIMIT:
                self._cache.popitem(last=False)
            while sum(len(entry[1] or b"") for entry in self._cache.values()) > CACHE_BYTES:
                self._cache.popitem(last=False)

    def _probe(self, magnet, timeout, proxy):
        """Enforce a wall-clock deadline even on DNS/library shutdown stalls."""
        request = {
            "magnet": magnet,
            "proxy": proxy or {}, "timeout": timeout,
        }
        worker = Path(__file__).with_name("metadata_worker.py")
        process = None
        with tempfile.TemporaryDirectory(prefix="episodeguard-") as directory:
            request["save_path"] = directory
            try:
                with self._lock:
                    if self._closed:
                        raise MetadataUnavailable("磁力校验已停止")
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-B", str(worker)], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    )
                    self._processes.add(process)
                output, _ = process.communicate(json.dumps(request).encode(), timeout=timeout)
                result = json.loads(output)
                if result.get("error"):
                    reasons = {
                        "dependency": "磁力校验依赖 libtorrent 未就绪，请检查插件依赖安装日志",
                        "timeout": "磁力元数据获取超时或没有可用节点",
                        "proxy": "当前代理不支持磁力元数据连接（需要 SOCKS5），未改用直连",
                    }
                    raise MetadataUnavailable(reasons.get(result["error"], "未取得哈希匹配的种子元数据"))
                content = base64.b64decode(result["content"], validate=True)
                if not content or len(content) > MAX_METADATA:
                    raise MetadataUnavailable("种子元数据为空或超过 2 MiB 上限")
                return content
            except subprocess.TimeoutExpired:
                raise MetadataUnavailable("磁力元数据获取超时或没有可用节点") from None
            except MetadataUnavailable:
                raise
            except (ValueError, KeyError, OSError):
                raise MetadataUnavailable("磁力元数据校验进程未正常完成") from None
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
                    with self._lock:
                        self._processes.discard(process)
