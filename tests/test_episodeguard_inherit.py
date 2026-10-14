"""Task-scoped mapping, real ORM transaction, and transfer association tests."""

from copy import deepcopy
from pathlib import Path
import re
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_episodeguard import load_plugin, meta, event, module, MediaType


HASH = "ab" * 20
NAME = "The.Apothecary.Diaries.S03E02.1080p.mkv"
ORIGIN = 'Subscribe|{"season":1}'


def prepare(name, custom_words=None):
    for word in custom_words or []:
        if " => " in word:
            old, new = word.split(" => ", 1)
            name = re.sub(old, new, name)
    return name, []


def plan():
    return {"hashes": [HASH], "identity": ["themoviedb", "220542"],
            "target": [1, 50], "origin": ORIGIN, "words": "# frozen\nexact => override",
            "files": [NAME], "names": [NAME], "original_custom_words": None}


class InheritTests(unittest.TestCase):
    def setUp(self):
        self.guard, context = load_plugin()
        self.addCleanup(context.stop)
        self.plugin = self.guard.EpisodeGuard()
        self.plugin.init_plugin({"enabled": True})
        self.addCleanup(self.plugin.stop_service)
        self.plugin.save_data = MagicMock()
        self.guard.SubscribeChain.return_value.get_subscribe_by_source.return_value = NS(
            season=1, total_episode=60, start_episode=1, custom_words=None)
        self.guard.DownloadChain.return_value.download_torrent.return_value = (b"torrent", "", [])
        self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = ("", [NAME])
        self.guard.MetaInfoPath = MagicMock(return_value=meta())
        self.guard.torrent_hashes = MagicMock(return_value=[HASH])
        self.snapshot = sys.modules["episodeguard.snapshots"]
        self.override = sys.modules["episodeguard.overrides"]
        stubs = {
            "app.core.meta.words": module("app.core.meta.words", WordsMatcher=lambda: NS(prepare=prepare)),
            "app.db.systemconfig_oper": module("app.db.systemconfig_oper", SystemConfigOper=lambda: NS(get=lambda _key: [])),
            "app.utils.media": module("app.utils.media", resolve_media_identity=lambda **_kwargs: ("themoviedb", "220542")),
        }
        extras = patch.dict(sys.modules, stubs)
        extras.start()
        self.addCleanup(extras.stop)
        sys.modules["app.schemas.types"].SystemConfigKey = NS(CustomIdentifiers="words")

    def enable_inherit(self, blocked):
        self.plugin._mode = "inherit"
        self.plugin._block_unverifiable = blocked
        self.plugin._snapshots = self.snapshot.TaskSnapshots([], self.plugin.save_data)
        self.plugin._snapshots.rows = MagicMock(return_value=[])

    def test_conflicting_filename_gets_frozen_target_with_either_setting(self):
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                self.enable_inherit(blocked)
                item = event()
                self.plugin.on_resource_download(item)
                self.assertFalse(item.event_data.cancel)
                plans = self.plugin._snapshots.matching_hash(HASH)
                self.assertEqual(plans[0]["target"], [1, 50])
                self.assertEqual(plans[0]["identity"], ["themoviedb", "220542"])
                self.assertIn("s=1-1;e=50-50", plans[0]["words"])
                self.assertEqual(plans[0]["names"], [NAME])

    def test_unknown_metadata_is_always_skipped_in_inherit(self):
        for blocked in (True, False):
            self.enable_inherit(blocked)
            self.guard.DownloadChain.return_value.download_torrent.return_value = (None, "", [])
            item = event()
            self.plugin.on_resource_download(item)
            self.assertTrue(item.event_data.cancel)

    def test_multiple_targets_and_multiple_videos_never_guess_mapping(self):
        self.enable_inherit(False)
        for files, candidate, episodes in [([NAME], meta(1, (49, 50)), (49, 50)),
                                            ([NAME, "other.S03E01.mkv"], meta(), (50,))]:
            self.guard.TorrentHelper.return_value.get_fileinfo_from_torrent_content.return_value = ("", files)
            item = event(candidate=candidate, episodes=episodes)
            self.plugin.on_resource_download(item)
            self.assertTrue(item.event_data.cancel)
        self.assertFalse(self.plugin._snapshots.matching_hash(HASH))

    def test_generation_failure_or_persistence_failure_cannot_allow_download(self):
        self.enable_inherit(False)
        self.guard.MetaInfoPath.return_value = meta(3, (2,))
        item = event()
        self.plugin.on_resource_download(item)
        self.assertTrue(item.event_data.cancel)
        self.guard.MetaInfoPath.return_value = meta()
        self.plugin.save_data.side_effect = OSError("private provider data")
        item = event()
        self.plugin.on_resource_download(item)
        self.assertTrue(item.event_data.cancel)
        self.assertNotIn("private provider", item.event_data.reason)

    def test_exact_rules_follow_base_conversion_and_do_not_match_other_files(self):
        rules, mapped, names = self.override.build_words(
            [NAME, NAME.replace(".mkv", ".chs.srt"), "Sample/sample.mkv"], {(1, 50)},
            ["S03E02 => S01E50"], prepare, {".mkv"}, {".srt"})
        result, _ = prepare(NAME, rules.splitlines())
        self.assertIn("S01E50.1080p", result)
        self.assertIn("s=1-1;e=50-50", result)
        other, _ = prepare("Other.S03E01.mkv", rules.splitlines())
        self.assertEqual(other, "Other.S03E01.mkv")
        self.assertEqual(len(mapped), 2)
        self.assertEqual(names[0], NAME)

    def test_unknown_number_can_be_overridden_but_ambiguous_files_cannot(self):
        rules, _, _ = self.override.build_words(["Show.mkv"], {(1, 50)}, [], prepare, {".mkv"}, {".srt"})
        self.assertIn("e=50-50", prepare("Show.mkv", rules.splitlines())[0])
        for files in ([NAME, "sample/" + NAME], ["bad => title.mkv"], ["one.mkv", "two.mkv"]):
            with self.assertRaises(self.override.OverrideUnavailable):
                self.override.build_words(files, {(1, 50)}, [], prepare, {".mkv"}, {".srt"})

    def test_ui_hides_unknown_switch_in_inherit_and_existing_configs_keep_validate(self):
        form, defaults = self.plugin.get_form()
        control = next(c for c in form[0]["content"] if c.get("props", {}).get("model") == "block_unverifiable")
        self.assertEqual(control["props"]["v-show"], "mode === 'validate'")
        self.assertEqual(defaults["mode"], "validate")
        self.assertEqual(self.plugin._mode, "validate")
        self.plugin.init_plugin({"enabled": True, "block_unverifiable": False})
        self.assertEqual(self.plugin._mode, "validate")
        self.assertFalse(self.plugin._block_unverifiable)

    def transfer_fixture(self, ready=True, actual=None, value=HASH):
        self.enable_inherit(False)
        fileitem = NS(storage="local", path="/download/" + NAME)
        self.tasks = [NS(fileitem=fileitem, download_hash=value, state="running")]
        self.chain = MagicMock()
        self.chain.jobview.list_jobs.side_effect = lambda: [NS(tasks=self.tasks)]
        self.chain.recognize_media.return_value = NS(type=MediaType.TV)
        sys.modules["app.chain.transfer"] = module("app.chain.transfer", TransferChain=lambda: self.chain)
        self.addCleanup(sys.modules.pop, "app.chain.transfer", None)
        self.plugin._snapshots.prepare(plan())
        self.row = {"note": {self.guard.MARKER: plan()}, "custom_words": "# words", "media_source": "themoviedb", "media_id": "220542"}
        self.plugin._snapshots.ready = MagicMock(return_value=self.row if ready else None)
        return NS(event_data=NS(cancel=False, source="", reason="", fileitem=fileitem,
                               meta=actual or meta(), mediainfo=NS(type=MediaType.TV)))

    def test_transfer_ready_correct_snapshot_passes(self):
        item = self.transfer_fixture()
        self.plugin.on_transfer_intercept(item)
        self.assertFalse(item.event_data.cancel)

    def test_transfer_stale_or_missing_snapshot_is_blocked_before_file_operation(self):
        for ready, actual in ((False, meta()), (True, meta(3, (2,)))):
            item = self.transfer_fixture(ready, actual)
            self.plugin.on_transfer_intercept(item)
            self.assertTrue(item.event_data.cancel)
            self.assertTrue(item.event_data.reason.startswith("EpisodeGuard 快照保护："))

    def test_same_filename_different_hash_does_not_use_another_search(self):
        item = self.transfer_fixture(ready=False, value="cd" * 20)
        self.plugin.on_transfer_intercept(item)
        self.assertFalse(item.event_data.cancel)

    def test_same_episodes_different_media_blocked(self):
        item = self.transfer_fixture()
        sys.modules["app.utils.media"].resolve_media_identity = lambda **_kwargs: ("themoviedb", "999")
        self.plugin.on_transfer_intercept(item)
        self.assertTrue(item.event_data.cancel)

    def test_download_added_reports_committed_binding(self):
        self.transfer_fixture()
        self.plugin.on_download_added(NS(event_data={"hash": HASH}))
        self.assertEqual(self.plugin._records[0]["result"], "覆盖规则已绑定")

    def test_retry_only_our_failure_once_and_never_remove_running_task(self):
        item = self.transfer_fixture()
        self.plugin._schedule_retry = MagicMock()
        data = {"transferinfo": NS(message="normal filesystem failure"),
                "download_hash": HASH, "fileitem": item.event_data.fileitem}
        self.plugin.on_transfer_failed(NS(event_data=data))
        self.plugin._schedule_retry.assert_not_called()
        data["transferinfo"].message = "EpisodeGuard 快照保护：任务季集过期"
        self.plugin.on_transfer_failed(NS(event_data=data))
        self.plugin.on_transfer_failed(NS(event_data=data))
        self.plugin._schedule_retry.assert_called_once()

    def run_retry(self, data):
        timers = []
        class FakeTimer:
            def __init__(self, delay, callback):
                self.callback = callback
                timers.append(self)
            def start(self): pass
            def cancel(self): pass
        with patch.object(self.guard, "Timer", FakeTimer):
            self.plugin._schedule_retry(data, 0)
            while timers:
                timers.pop(0).callback()

    def test_retry_uses_saved_words_and_hash_only_after_own_task_failed(self):
        item = self.transfer_fixture()
        self.tasks[0].state = "failed"
        data = {"download_hash": HASH, "fileitem": item.event_data.fileitem, "downloader": "qb"}
        self.run_retry(data)
        self.chain.jobview.remove_task.assert_called_once_with(item.event_data.fileitem)
        kwargs = self.chain.do_transfer.call_args.kwargs
        self.assertEqual(kwargs["download_hash"], HASH)
        self.assertEqual(kwargs["meta"].episode_list, [50])
        self.assertNotIn("reorganize", kwargs)

    def test_retry_does_not_remove_running_or_unrelated_task(self):
        item = self.transfer_fixture()
        data = {"download_hash": HASH, "fileitem": item.event_data.fileitem}
        self.run_retry(data)
        self.chain.jobview.remove_task.assert_not_called()
        self.tasks[0].state = "failed"
        self.tasks[0].download_hash = "cd" * 20
        self.run_retry(data)
        self.chain.jobview.remove_task.assert_not_called()

    def test_retry_can_requeue_after_native_cleanup_removes_failed_job(self):
        item = self.transfer_fixture()
        self.tasks.clear()
        self.run_retry({"download_hash": HASH, "fileitem": item.event_data.fileitem})
        self.chain.jobview.remove_task.assert_not_called()
        self.chain.do_transfer.assert_called_once()


try:
    from sqlalchemy import create_engine, Column, Integer, String, JSON, select
    from sqlalchemy.orm import declarative_base, Session
    SQLALCHEMY = True
except ImportError:
    SQLALCHEMY = False


@unittest.skipUnless(SQLALCHEMY, "SQLAlchemy is supplied by MoviePilot; install for transaction tests")
class SnapshotTransactionTests(unittest.TestCase):
    def setUp(self):
        guard, context = load_plugin()
        self.addCleanup(context.stop)
        self.snapshot = sys.modules["episodeguard.snapshots"]
        self.override = sys.modules["episodeguard.overrides"]
        base = declarative_base()
        class History(base):
            __tablename__ = "downloadhistory"
            id = Column(Integer, primary_key=True)
            download_hash = Column(String)
            media_source = Column(String)
            media_id = Column(String)
            seasons = Column(String)
            episodes = Column(String)
            note = Column(JSON)
            custom_words = Column(String)
            date = Column(String)
            downloader = Column(String)
        self.model = History
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        base.metadata.create_all(self.engine)
        self.saved = MagicMock()
        def reader(hashes):
            with self.engine.connect() as connection:
                return [dict(r) for r in connection.execute(select(History.__table__).where(
                    History.download_hash.in_(hashes))).mappings()]
        self.reader = reader
        self.snapshots = self.snapshot.TaskSnapshots([], self.saved)
        self.snapshots.start(History, reader)
        self.addCleanup(self.snapshots.stop)
        self.snapshots.prepare(plan())

    def row(self, **changes):
        values = dict(download_hash=HASH, media_source="themoviedb", media_id="220542",
                      seasons="S01", episodes="E50", note={"source": ORIGIN, "native": "keep"},
                      custom_words=None, date="2026-10-14 10:00:00", downloader="qb")
        values.update(changes)
        return self.model(**values)

    def insert(self, **changes):
        with Session(self.engine) as session:
            row = self.row(**changes)
            session.add(row)
            session.commit()
        return self.reader([HASH])

    def test_words_and_native_history_commit_in_one_transaction(self):
        rows = self.insert()
        self.assertEqual(rows[0]["custom_words"], plan()["words"])
        self.assertEqual(rows[0]["note"]["native"], "keep")
        marker = rows[0]["note"][self.override.MARKER]
        self.assertEqual(marker["target"], [1, 50])
        self.assertIsNone(marker["original_custom_words"])
        self.assertEqual(self.snapshots.ready([HASH])["id"], rows[0]["id"])
        self.saved.assert_called_once()  # No plugin-data IO inside the ORM transaction.

    def test_rollback_does_not_leave_separate_override_record(self):
        with Session(self.engine) as session:
            row = self.row()
            session.add(row)
            session.flush()
            self.assertEqual(row.custom_words, plan()["words"])
            session.rollback()
        self.assertEqual(self.reader([HASH]), [])
        self.assertIsNone(self.snapshots.ready([HASH]))

    def test_hash_source_identity_episode_and_original_words_must_all_match(self):
        for changes in ({"download_hash": "cd" * 20}, {"note": {"source": "Manual"}},
                        {"media_id": "999"}, {"episodes": "E49"}, {"custom_words": "user changed rules"}):
            with Session(self.engine) as session:
                row = self.row(**changes)
                session.add(row)
                session.flush()
                self.assertNotIn(self.override.MARKER, row.note)
                self.assertEqual(row.custom_words, changes.get("custom_words"))
                session.rollback()

    def test_conflicting_old_history_rejects_new_plan_and_hook_binding(self):
        self.insert(media_id="999")
        with self.assertRaises(self.override.OverrideUnavailable):
            self.snapshots.prepare(plan())
        rows = self.insert()
        self.assertTrue(all(self.override.MARKER not in r["note"] for r in rows))
        self.assertIsNone(self.snapshots.ready([HASH]))

    def test_same_hash_different_pending_target_rejected(self):
        other = plan()
        other["target"] = [1, 49]
        with self.assertRaises(self.override.OverrideUnavailable):
            self.snapshots.prepare(other)

    def test_latest_unmarked_history_cannot_inherit_older_snapshot(self):
        self.insert()
        self.insert(date="2026-10-14 11:00:00", custom_words="manual change")
        self.assertIsNone(self.snapshots.ready([HASH]))

    def test_stop_removes_listener_without_erasing_committed_words(self):
        self.insert()
        self.snapshots.stop()
        rows = self.insert(date="2026-10-14 11:00:00")
        self.assertEqual(rows[0]["custom_words"], plan()["words"])
        self.assertIsNone(rows[1]["custom_words"])


if __name__ == "__main__":
    unittest.main()
