"""Offline policy and host-event tests; never talk to live services."""

import ast
import importlib.util
import re
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class MediaType(Enum):
    TV = "电视剧"
    MOVIE = "电影"


class FakePluginBase:
    def get_data(self, _key):
        return []

    def save_data(self, _key, _value):
        pass


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    result.__path__ = []
    return result


def load_plugin():
    eventmanager = NS(register=lambda *_args, **_kwargs: lambda fn: fn)
    stubs = {
        name: module(name) for name in (
            "app", "app.chain", "app.schemas", "app.core", "app.helper"
        )
    }
    stubs.update({
        "app.chain.download": module("app.chain.download", DownloadChain=MagicMock()),
        "app.chain.subscribe": module("app.chain.subscribe", SubscribeChain=MagicMock()),
        "app.plugins": module("app.plugins", _PluginBase=FakePluginBase),
        "app.schemas.types": module("app.schemas.types", MediaType=MediaType,
                                   ChainEventType=NS(ResourceDownload="resource.download",
                                                     ResourceSelection="resource.selection")),
        "app.core.event": module("app.core.event", Event=object, eventmanager=eventmanager),
        "app.log": module("app.log", logger=MagicMock()),
        "app.core.metainfo": module("app.core.metainfo", MetaInfoPath=MagicMock()),
        "app.helper.torrent": module("app.helper.torrent", TorrentHelper=MagicMock()),
        "app.core.config": module("app.core.config", settings=NS(RMT_MEDIAEXT=[".mkv", ".mp4"])),
    })
    name = "episodeguard"
    source = ROOT / "plugins.v2" / "episodeguard" / "__init__.py"
    spec = importlib.util.spec_from_file_location(name, source, submodule_search_locations=[str(source.parent)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[name] = plugin
    # Leave stubs active for lazy imports, inside a per-test context.
    context = patch.dict(sys.modules, stubs)
    context.start()
    spec.loader.exec_module(plugin)
    return plugin, context


def meta(season=1, episodes=(50,)):
    return NS(title="药屋少女的呢喃", season_list=[season] if season is not None else [], episode_list=list(episodes),
              begin_season=season, end_season=None,
              begin_episode=min(episodes) if episodes else None,
              end_episode=max(episodes) if len(episodes) > 1 else None)


def event(episodes=(50,), origin='Subscribe|{"season":1}', candidate=None, kind=MediaType.TV):
    return NS(event_data=NS(cancel=False, source="", reason="", origin=origin, episodes=set(episodes),
        context=NS(meta_info=candidate or meta(), media_info=NS(type=kind),
                   torrent_info=NS(title="药屋少女的呢喃 - 50", site_name="测试站点",
                                   enclosure="https://example.invalid/file.torrent"))))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard, self.modules = load_plugin()
        self.addCleanup(self.modules.stop)
        self.plugin = self.guard.EpisodeGuard()
        self.plugin.init_plugin({"enabled": True})
        self.plugin.save_data = MagicMock()
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value = NS(
            season=1, total_episode=60, start_episode=1, custom_words=None)
        self.guard.DownloadChain.return_value.download_torrent.return_value = (b"torrent", "", [])
        self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = (
            "", ["The.Apothecary.Diaries.S01E50.1080p.mkv"])
        self.guard.MetaInfoPath = MagicMock(return_value=meta())

    def run_guard(self, **kwargs):
        item = event(**kwargs)
        self.plugin.on_resource_download(item)
        return item.event_data

    def files(self, names, parsed):
        self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = ("", names)
        self.guard.MetaInfoPath.side_effect = parsed

    def test_exact_user_problem_is_rejected(self):
        self.files(["The.Apothecary.Diaries.S03E02.The.Fire-Rat.Cloak.1080p.NF.WEB-DL.AAC2.0.H.264-VARYG.mkv"], [meta(3, (2,))])
        data = self.run_guard()
        self.assertTrue(data.cancel)
        self.assertIn("S03", data.reason)
        self.assertIn("S01E50", data.reason)

    def test_same_season_wrong_episode_rejected(self):
        self.guard.MetaInfoPath.return_value = meta(1, (51,))
        self.assertTrue(self.run_guard().cancel)

    def test_correct_resource_passes(self):
        self.assertFalse(self.run_guard().cancel)
        self.plugin.save_data.assert_not_called()

    def test_subscription_words_forwarded_unchanged(self):
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value.custom_words = "S03 => S01\nS01E <> END >> EP+48"
        self.assertFalse(self.run_guard().cancel)
        self.assertEqual(self.guard.MetaInfoPath.call_args.kwargs["custom_words"],
                         ["S03 => S01", "S01E <> END >> EP+48"])

    def test_no_subscription_words_keeps_global_fallback(self):
        self.run_guard()
        self.assertIsNone(self.guard.MetaInfoPath.call_args.kwargs["custom_words"])

    def test_manual_download_ignored_by_default(self):
        self.assertFalse(self.run_guard(origin="Manual").cancel)
        self.guard.DownloadChain.assert_not_called()

    def test_manual_download_can_be_checked(self):
        self.plugin._only_subscriptions = False
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        self.assertTrue(self.run_guard(origin="Manual").cancel)

    def test_movie_ignored(self):
        self.assertFalse(self.run_guard(kind=MediaType.MOVIE).cancel)
        self.guard.DownloadChain.assert_not_called()

    def test_disabled_does_not_fetch(self):
        self.plugin._enabled = False
        self.assertFalse(self.run_guard().cancel)
        self.guard.DownloadChain.assert_not_called()

    def test_existing_veto_not_overwritten(self):
        item = event()
        item.event_data.cancel = True
        item.event_data.source = "another plugin"
        self.plugin.on_resource_download(item)
        self.assertEqual(item.event_data.source, "another plugin")
        self.guard.DownloadChain.assert_not_called()

    def test_magnet_rejected_before_fetch(self):
        item = event()
        item.event_data.context.torrent_info.enclosure = "magnet:?xt=urn:btih:test"
        self.plugin.on_resource_download(item)
        self.assertTrue(item.event_data.cancel)
        self.guard.DownloadChain.assert_not_called()

    def test_magnet_can_be_allowed_explicitly(self):
        self.plugin._block_unverifiable = False
        item = event()
        item.event_data.context.torrent_info.enclosure = "magnet:?xt=urn:btih:test"
        self.plugin.on_resource_download(item)
        self.assertFalse(item.event_data.cancel)
        self.assertEqual(self.plugin._records[0]["result"], "放行（未校验）")

    def test_indirect_magnet_result_rejected(self):
        self.guard.DownloadChain.return_value.download_torrent.return_value = ("magnet:?test", "", [])
        self.assertTrue(self.run_guard().cancel)

    def test_magnet_metadata_checked_with_existing_policy(self):
        item = event()
        item.event_data.context.torrent_info.enclosure = "magnet:?xt=urn:btih:" + "ab" * 20
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        with patch.object(self.plugin._metadata, "resolve", return_value=b"metadata") as resolve:
            self.plugin.on_resource_download(item)
        self.assertTrue(item.event_data.cancel)
        self.assertIn("S03", item.event_data.reason)
        self.guard.DownloadChain.assert_not_called()
        resolve.assert_called_once()

    def test_indirect_url_magnet_metadata_can_pass(self):
        self.guard.DownloadChain.return_value.download_torrent.return_value = ("magnet:?test", "", [])
        with patch.object(self.plugin._metadata, "resolve", return_value=b"metadata"):
            self.assertFalse(self.run_guard().cancel)

    def test_unknown_magnet_setting_does_not_disable_verified_conflict(self):
        self.plugin._block_unverifiable = False
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        item = event()
        item.event_data.context.torrent_info.enclosure = "magnet:?xt=urn:btih:" + "ab" * 20
        with patch.object(self.plugin._metadata, "resolve", return_value=b"metadata"):
            self.plugin.on_resource_download(item)
        self.assertTrue(item.event_data.cancel)

    def test_selection_resets_search_budget(self):
        with patch.object(self.plugin._metadata, "begin_search") as begin:
            self.plugin.on_resource_selection(NS(event_data=NS(origin="Subscribe|87", source="")))
        begin.assert_called_once_with("Subscribe|87")

    def test_metadata_fetch_failure_rejected(self):
        self.guard.DownloadChain.return_value.download_torrent.return_value = (None, "", [])
        self.assertTrue(self.run_guard().cancel)

    def test_missing_subscription_does_not_guess(self):
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value = None
        self.assertTrue(self.run_guard().cancel)
        self.guard.DownloadChain.assert_not_called()

    def test_unknown_video_rejected(self):
        self.guard.MetaInfoPath.return_value = meta(1, ())
        self.assertTrue(self.run_guard().cancel)

    def test_mismatch_is_not_allowed_by_unknown_switch(self):
        self.plugin._block_unverifiable = False
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        self.assertTrue(self.run_guard().cancel)

    def test_no_video_files_rejected(self):
        self.files(["readme.txt", "subtitle.ass"], [])
        self.assertTrue(self.run_guard().cancel)
        self.guard.MetaInfoPath.assert_not_called()

    def test_sample_and_subtitles_ignored(self):
        self.files(["Sample/sample.mkv", "Show.S01E50.mkv", "Show.S03E02.ass"], [meta()])
        self.assertFalse(self.run_guard().cancel)
        self.assertEqual(self.guard.MetaInfoPath.call_count, 1)

    def test_single_file_root_not_added_twice(self):
        self.run_guard()
        self.assertEqual(str(self.guard.MetaInfoPath.call_args.args[0]),
                         "/episodeguard/The.Apothecary.Diaries.S01E50.1080p.mkv")

    def test_folder_season_context_preserved(self):
        self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = (
            "Show.S01", ["Season 1/50.mkv"])
        self.run_guard()
        self.assertEqual(str(self.guard.MetaInfoPath.call_args.args[0]), "/episodeguard/Show.S01/Season 1/50.mkv")

    def test_selected_subset_allows_extra_episodes(self):
        self.files(["Show.S01E49.mkv", "Show.S01E50.mkv"], [meta(1, (49,)), meta()])
        self.assertFalse(self.run_guard(candidate=meta(1, (49, 50))).cancel)

    def test_declared_pack_must_cover_each_target_episode(self):
        self.guard.MetaInfoPath.return_value = meta()
        self.assertTrue(self.run_guard(episodes=(49, 50), candidate=meta(1, (49, 50))).cancel)

    def test_declared_pack_with_wrong_extra_episode_rejected(self):
        self.guard.MetaInfoPath.return_value = meta(1, (49, 50))
        self.assertTrue(self.run_guard().cancel)

    def test_file_with_multiple_episode_range_passes(self):
        self.guard.MetaInfoPath.return_value = meta(1, (49, 50))
        self.assertFalse(self.run_guard(episodes=(49, 50), candidate=meta(1, (49, 50))).cancel)

    def test_subset_with_wrong_season_rejected(self):
        self.files(["Show.S03E50.mkv", "Show.S01E49.mkv"], [meta(3, (50,)), meta(1, (49,))])
        self.assertTrue(self.run_guard(candidate=meta(1, (49, 50))).cancel)

    def test_whole_season_uses_subscription_range(self):
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value = NS(
            season=1, total_episode=50, start_episode=49, custom_words=None)
        self.guard.MetaInfoPath.return_value = meta(1, (48, 49, 50))
        self.assertFalse(self.run_guard(episodes=(), candidate=meta(1, ())).cancel)

    def test_whole_season_with_unknown_total_rejected(self):
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value.total_episode = 0
        self.assertTrue(self.run_guard(episodes=(), candidate=meta(1, ())).cancel)

    def test_special_episode_zero_not_lost(self):
        self.guard.MetaInfoPath.return_value = meta(0, (0,))
        self.assertFalse(self.run_guard(episodes=(0,), candidate=meta(0, (0,))).cancel)

    def test_invalid_parent_path_rejected(self):
        self.files(["../../Show.S01E50.mkv"], [])
        self.assertTrue(self.run_guard().cancel)

    def test_invalid_root_path_rejected(self):
        self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = (
            "/unexpected", ["Show.S01E50.mkv"])
        self.assertTrue(self.run_guard().cancel)

    def test_large_range_rejected_before_list_property(self):
        class MaliciousMeta:
            begin_season = 1
            end_season = None
            season_list = [1]
            begin_episode = 1
            end_episode = 100000000

            @property
            def episode_list(self):
                raise AssertionError("must not allocate")
        self.assertTrue(self.run_guard(candidate=MaliciousMeta()).cancel)

    def test_external_exception_does_not_disclose_signed_url(self):
        self.guard.DownloadChain.return_value.download_torrent.side_effect = ValueError("https://site/file?passkey=secret")
        data = self.run_guard()
        self.assertTrue(data.cancel)
        self.assertNotIn("secret", data.reason)
        self.assertNotIn("secret", str(self.plugin._records))

    def test_record_failure_cannot_undo_veto(self):
        self.plugin.save_data.side_effect = RuntimeError("database unavailable")
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        self.assertTrue(self.run_guard().cancel)

    def test_records_bounded(self):
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        for _ in range(55):
            self.run_guard()
        self.assertEqual(len(self.plugin._records), 50)



    def test_rejected_candidate_keeps_missing_episode_and_next_runs(self):
        """Execute the actual V2 candidate loop with this plugin at its event boundary."""
        source_path = Path("/tmp/mpv2-download.py")
        if not source_path.exists():
            self.skipTest("V2 source snapshot not available")
        tree = ast.parse(source_path.read_text())
        branch = None
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for method in cls.body:
                if not isinstance(method, ast.FunctionDef) or method.name != "batch_download":
                    continue
                for node in method.body:
                    if isinstance(node, ast.If) and "开始电视剧完整集匹配" in ast.unparse(node):
                        branch = node
                        break
        self.assertIsNotNone(branch)
        first, second = event(), event()
        first.event_data.context.torrent_info.title = "bad"
        second.event_data.context.torrent_info.title = "good"
        failures, submitted, downloaded = [], [], []
        missing = {"show": {1: NS(episodes=[50], start_episode=1, total_episode=60)}}

        def download(context, **kwargs):
            self.guard.MetaInfoPath.return_value = meta(3, (2,)) if context.torrent_info.title == "bad" else meta()
            self.assertEqual(missing["show"][1].episodes, [50])
            item = first if context is first.event_data.context else second
            self.plugin.on_resource_download(item)
            if item.event_data.cancel:
                return None
            submitted.append(context.torrent_info.title)
            return "test-hash"

        def update(**kwargs):
            remaining = sorted(set(kwargs["_need"]) - kwargs["_current"])
            missing["show"][1].episodes = remaining
            return remaining

        import copy
        namespace = {
            "self": NS(download_single=download, _matches_media_identity=lambda *_: True),
            "copy": copy, "logger": MagicMock(), "global_vars": NS(is_system_stopped=False),
            "MediaType": MediaType, "no_exists": missing,
            "contexts": [first.event_data.context, second.event_data.context],
            "downloaded_list": downloaded, "__is_context_in_failure_cooldown": lambda _: False,
            "__apply_allowed_episodes": lambda needed, _: set(needed),
            "__requires_complete_coverage": lambda _: False,
            "__remember_context_failure": failures.append, "__update_episodes": update,
            **dict.fromkeys(["save_path", "channel", "source", "userid", "username", "downloader", "custom_words"]),
        }
        exec(compile(ast.Module(body=[branch], type_ignores=[]), str(source_path), "exec"), namespace)
        self.assertEqual(submitted, ["good"])
        self.assertEqual(failures, [first.event_data.context])
        self.assertEqual(downloaded, [second.event_data.context])
        self.assertEqual(missing["show"][1].episodes, [])


if __name__ == "__main__":
    unittest.main()
