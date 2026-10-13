"""Deadline/cache tests and a local BEP 9 peer that never serves video data."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


DIRECTORY = Path(__file__).resolve().parents[1] / "plugins.v2/episodeguard"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


metadata = load("metadata")
worker = load("metadata_worker")
HASH = "fed2bab6d136e0ab8c9e86e8a386d4c84cd4fa12"
MAGNET = "magnet:?xt=urn:btih:" + HASH


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = metadata.MetadataResolver()
        self.addCleanup(self.resolver.stop)

    def test_base32_and_hex_share_key(self):
        encoded = base64.b32encode(bytes.fromhex(HASH)).decode()
        self.assertEqual(metadata.magnet_key(MAGNET),
                         metadata.magnet_key("magnet:?xt=urn:btih:" + encoded + "&dn=other"))

    def test_v2_key(self):
        self.assertEqual(metadata.magnet_key("magnet:?xt=urn:btmh:1220" + "ab" * 32), "v2:" + "ab" * 32)

    def test_invalid_magnet_does_not_probe(self):
        with patch.object(self.resolver, "_probe") as probe:
            with self.assertRaises(metadata.MetadataUnavailable):
                self.resolver.resolve("magnet:?xt=urn:btih:test")
            probe.assert_not_called()

    def test_success_cache_shared_by_hash(self):
        with patch.object(self.resolver, "_probe", return_value=b"metadata") as probe:
            for suffix in ("&dn=one", "&dn=two"):
                self.assertEqual(self.resolver.resolve(MAGNET + suffix), b"metadata")
            self.assertEqual(probe.call_count, 1)

    def test_failure_cache_expires(self):
        with patch.object(metadata.time, "monotonic", return_value=10) as clock:
            with patch.object(self.resolver, "_probe", side_effect=metadata.MetadataUnavailable("timeout")) as probe:
                for _ in range(2):
                    with self.assertRaises(metadata.MetadataUnavailable):
                        self.resolver.resolve(MAGNET)
                self.assertEqual(probe.call_count, 1)
                clock.return_value = 10 + metadata.FAILURE_TTL + 1
                with self.assertRaises(metadata.MetadataUnavailable):
                    self.resolver.resolve(MAGNET)
                self.assertEqual(probe.call_count, 2)

    def test_each_candidate_keeps_full_timeout_after_prior_failures(self):
        with patch.object(metadata.time, "monotonic", return_value=10) as clock:
            attempts = []

            def probe_time(magnet, timeout, proxy):
                attempts.append(timeout)
                clock.return_value += timeout
                if len(attempts) <= 3:
                    raise metadata.MetadataUnavailable("磁力元数据获取超时")
                return b"valid metadata for later candidate"

            with patch.object(self.resolver, "_probe", side_effect=probe_time):
                for value in ("01", "02", "03"):
                    with self.assertRaises(metadata.MetadataUnavailable):
                        self.resolver.resolve("magnet:?xt=urn:btih:" + value * 20)
                content = self.resolver.resolve("magnet:?xt=urn:btih:" + "04" * 20)
        self.assertEqual(content, b"valid metadata for later candidate")
        self.assertEqual(attempts, [15, 15, 15, 15])

    def test_conflicting_hashes_rejected(self):
        with self.assertRaisesRegex(metadata.MetadataUnavailable, "相互冲突"):
            metadata.magnet_key(MAGNET + "&xt=urn:btih:" + "00" * 20)

    def test_concurrency_limit_does_not_queue(self):
        self.resolver._slots.acquire()
        self.resolver._slots.acquire()
        with self.assertRaisesRegex(metadata.MetadataUnavailable, "并发"):
            self.resolver.resolve(MAGNET)
        self.resolver._slots.release()
        self.resolver._slots.release()

    def test_concurrency_releases_slots_for_next_candidate(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        failures = []

        def probe(_magnet, timeout, _proxy):
            self.assertEqual(timeout, 15)
            barrier.wait(timeout=3)
            release.wait(timeout=3)
            return b"metadata"

        def run(uri):
            try:
                self.resolver.resolve(uri)
            except Exception as error:
                failures.append(error)

        with patch.object(self.resolver, "_probe", side_effect=probe):
            threads = [threading.Thread(target=run, args=("magnet:?xt=urn:btih:" + value * 20,))
                       for value in ("01", "02")]
            for thread in threads:
                thread.start()
            try:
                barrier.wait(timeout=3)
                with self.assertRaisesRegex(metadata.MetadataUnavailable, "并发"):
                    self.resolver.resolve(MAGNET)
            finally:
                release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual(failures, [])
        with patch.object(self.resolver, "_probe", return_value=b"metadata") as next_probe:
            self.assertEqual(self.resolver.resolve(MAGNET), b"metadata")
            self.assertEqual(next_probe.call_args.args[1], 15)

    def test_actual_hung_process_is_terminated_at_deadline(self):
        real_popen = subprocess.Popen
        processes = []

        def launch(_args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        started = time.monotonic()
        with patch.object(metadata.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(metadata.MetadataUnavailable, "超时"):
                self.resolver._probe(MAGNET, 0.15, {})
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(processes[0].poll())

    def test_cache_count_and_byte_limit(self):
        with patch.object(metadata, "CACHE_LIMIT", 2), patch.object(metadata, "CACHE_BYTES", 5):
            for key in ("a", "b", "c"):
                self.resolver._remember(key, b"abc", "", 3600)
            self.assertEqual(list(self.resolver._cache), ["c"])

    def test_timeout_kills_worker_and_cleans_directory(self):
        process = MagicMock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 1), (b"", b"")]
        process.poll.return_value = None
        with patch.object(metadata.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(metadata.MetadataUnavailable, "超时"):
                self.resolver._probe(MAGNET, 1, {})
        payload = json.loads(process.communicate.call_args_list[0].args[0])
        self.assertFalse(Path(payload["save_path"]).exists())
        process.kill.assert_called_once()
        self.assertEqual(self.resolver._processes, set())

    def test_only_magnet_and_proxy_passed_over_stdin(self):
        process = MagicMock()
        process.communicate.return_value = (b'{"error":"dependency"}', b"")
        process.poll.return_value = 0
        proxy = {"https": "socks5h://user:secret@proxy.invalid:1080"}
        with patch.object(metadata.subprocess, "Popen", return_value=process) as popen:
            with self.assertRaisesRegex(metadata.MetadataUnavailable, "libtorrent") as error:
                self.resolver._probe(MAGNET, 1, proxy)
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn("secret", str(popen.call_args))
            payload = json.loads(process.communicate.call_args_list[0].args[0])
            self.assertEqual(set(payload), {"magnet", "proxy", "timeout", "save_path"})
            self.assertEqual(payload["proxy"], proxy)

    def test_stop_kills_only_owned_workers(self):
        process = MagicMock()
        process.poll.return_value = None
        self.resolver._processes.add(process)
        self.resolver.stop()
        process.kill.assert_called_once()
        with self.assertRaises(metadata.MetadataUnavailable):
            self.resolver.resolve(MAGNET)


class ProxyTests(unittest.TestCase):
    def test_unsupported_proxy_does_not_go_direct(self):
        self.assertIsNone(worker.session_settings({"proxy": {"https": "http://proxy.invalid:8080"}}))
        settings = worker.session_settings({"proxy": {"https": "socks5h://user:secret@proxy.invalid:1080"}})
        self.assertEqual(settings["proxy_type"], 3)
        self.assertTrue(settings["proxy_peer_connections"])


try:
    import libtorrent as lt
except ImportError:
    lt = None


@unittest.skipUnless(lt, "libtorrent native checks require plugin requirements")
class NativeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.info = lt.bencode({b"name": b"The.Apothecary.Diaries.S03E02.mkv", b"length": 1,
                               b"piece length": 16384, b"pieces": b"0" * 20})
        self.hash = hashlib.sha1(self.info).digest()
        self.magnet = "magnet:?xt=urn:btih:" + self.hash.hex()
        self.content = lt.bencode({b"info": lt.bdecode(self.info)})

    def test_http_torrent_hash_verification(self):
        expected = lt.parse_magnet_uri(self.magnet).info_hashes
        self.assertTrue(worker.matches_hash(lt, expected, self.content))
        other = lt.parse_magnet_uri(MAGNET).info_hashes
        self.assertFalse(worker.matches_hash(lt, other, self.content))
        self.assertFalse(worker.matches_hash(lt, expected, b"not a torrent"))

    def test_peer_receives_metadata_requests_and_no_payload_requests(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(5)
        self.addCleanup(server.close)
        messages, errors = [], []

        def read_exact(connection, size):
            data = b""
            while len(data) < size:
                part = connection.recv(size - len(data))
                if not part:
                    raise EOFError()
                data += part
            return data

        def serve():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(5)
                    handshake = read_exact(connection, 68)
                    self.assertEqual(handshake[28:48], self.hash)
                    reserved = bytearray(8)
                    reserved[5] = 0x10
                    connection.sendall(b"\x13BitTorrent protocol" + reserved + self.hash + b"-EGTEST-123456789012")

                    def send(message):
                        connection.sendall(struct.pack("!I", len(message)) + message)

                    send(b"\x14\x00" + lt.bencode({b"m": {b"ut_metadata": 3}, b"metadata_size": len(self.info)}))
                    response_id = None
                    while True:
                        length = struct.unpack("!I", read_exact(connection, 4))[0]
                        if length == 0:
                            continue
                        message = read_exact(connection, length)
                        messages.append(message[0])
                        if message[:2] == b"\x14\x00":
                            response_id = lt.bdecode(message[2:])[b"m"][b"ut_metadata"]
                        elif message[:2] == b"\x14\x03":
                            request = lt.bdecode(message[2:])
                            self.assertEqual(request[b"msg_type"], 0)
                            header = lt.bencode({b"msg_type": 1, b"piece": request[b"piece"], b"total_size": len(self.info)})
                            send(bytes([20, response_id]) + header + self.info)
            except (EOFError, ConnectionResetError):
                pass
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        params = lt.parse_magnet_uri(self.magnet + "&x.pe=127.0.0.1:" + str(server.getsockname()[1]))
        with tempfile.TemporaryDirectory() as directory:
            request = {"proxy": {}, "save_path": directory}
            settings = worker.session_settings(request)
            settings["enable_dht"] = False
            with patch.object(worker, "session_settings", return_value=settings):
                result = worker.from_peers(lt, params, request, time.monotonic() + 4)
            self.assertEqual(list(Path(directory).rglob("*.mkv")), [])
        thread.join(5)
        self.assertEqual(errors, [])
        self.assertIn("content", result, result)
        self.assertEqual(base64.b64decode(result["content"]), self.content)
        self.assertIn(20, messages)
        self.assertNotIn(6, messages, "BEP 3 piece request must never be sent")


if __name__ == "__main__":
    unittest.main()
