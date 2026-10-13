"""Read only BEP 9 metadata for MP-provided magnets; never request video pieces."""

import base64
import json
import sys
import time
from urllib.parse import unquote, urlsplit


MAX_METADATA = 2 * 1024 * 1024


def matches_hash(lt, expected, content):
    """Require every hash in the magnet to match the fetched torrent."""
    if not content or len(content) > MAX_METADATA:
        return False
    try:
        actual = lt.torrent_info(content).info_hashes()
        return ((not expected.has_v1() or expected.v1 == actual.v1)
                and (not expected.has_v2() or expected.v2 == actual.v2))
    except Exception:
        return False


def session_settings(request):
    """Small, temporary client with no port mapping or local discovery."""
    settings = {
        "listen_interfaces": "0.0.0.0:0", "connections_limit": 12,
        "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
        "max_metadata_size": MAX_METADATA, "alert_mask": 0,
    }
    proxies = request["proxy"]
    if proxies:
        # Do not bypass MoviePilot's requested proxy by silently going direct.
        proxy = urlsplit(proxies.get("https") or proxies.get("http") or "")
        if proxy.scheme not in ("socks5", "socks5h") or not proxy.hostname:
            return None
        settings.update({
            "proxy_type": 3 if proxy.username else 2, "proxy_hostname": proxy.hostname,
            "proxy_port": proxy.port or 1080, "proxy_username": unquote(proxy.username or ""),
            "proxy_password": unquote(proxy.password or ""), "proxy_hostnames": True,
            "proxy_peer_connections": True, "proxy_tracker_connections": True,
        })
    return settings


def from_peers(lt, params, request, deadline):
    """Upload mode with auto-management off prevents payload piece requests."""
    settings = session_settings(request)
    if settings is None:
        return {"error": "proxy"}
    params.save_path = request["save_path"]
    flags = lt.torrent_flags
    params.flags |= flags.upload_mode | flags.default_dont_download
    params.flags &= ~(flags.auto_managed | flags.paused)
    # The magnet may contain web seeds; these are never needed for metadata.
    params.url_seeds = []
    params.http_seeds = []
    params.file_priorities = []
    session = lt.session(settings)
    handle = None
    try:
        handle = session.add_torrent(params)
        while time.monotonic() < deadline:
            if handle.status().has_metadata:
                handle.pause()
                info = bytes(handle.torrent_file().info_section())
                if len(info) > MAX_METADATA:
                    return {"error": "invalid"}
                content = lt.bencode({b"info": lt.bdecode(info)})
                if matches_hash(lt, params.info_hashes, content):
                    return {"content": base64.b64encode(content).decode("ascii")}
                return {"error": "invalid"}
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return {"error": "timeout"}
    finally:
        if handle is not None:
            session.remove_torrent(handle)
        session.pause()


def resolve(request):
    """Return only validated torrent bytes or a fixed error code."""
    try:
        import libtorrent as lt
    except ImportError:
        return {"error": "dependency"}
    deadline = time.monotonic() + request["timeout"]
    params = lt.parse_magnet_uri(request["magnet"])
    return from_peers(lt, params, request, deadline)


if __name__ == "__main__":
    try:
        result = resolve(json.load(sys.stdin))
    except Exception:
        result = {"error": "invalid"}
    print(json.dumps(result))
