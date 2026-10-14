"""Validate torrent episodes or freeze search episodes in native task words."""

from datetime import datetime
from pathlib import Path
from threading import RLock, Timer

from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaType

from app.core.event import Event, eventmanager
from app.log import logger
from app.core.metainfo import MetaInfoPath
from app.helper.torrent import TorrentHelper
from app.core.config import settings

from .policy import DEFAULT_VIDEO_EXTENSIONS, FileEpisodes, Verdict, check_files, describe, video_paths
from .metadata import MetadataResolver, MetadataUnavailable
from .overrides import MARKER, OverrideUnavailable, build_words, torrent_hashes
from .snapshots import TaskSnapshots


class CheckUnavailable(ValueError):
    """A safe, user-facing explanation produced by this plugin itself."""


class EpisodeGuard(_PluginBase):
    plugin_name = "下载季集校验"
    plugin_desc = "校验文件季集，或使用任务识别词沿用搜索匹配的季集。"
    plugin_icon = "https://raw.githubusercontent.com/z-henry/MoviePilot-Plugins/main/icons/episodeguard.svg"
    plugin_version = "1.2.0"
    plugin_author = "henryz"
    author_url = "https://github.com/z-henry"
    plugin_config_prefix = "episodeguard_"
    plugin_order = 30
    auth_level = 1

    def init_plugin(self, config=None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._only_subscriptions = bool(config.get("only_subscriptions", True))
        self._block_unverifiable = bool(config.get("block_unverifiable", True))
        self._mode = config.get("mode", "validate")
        if self._mode not in ("validate", "inherit"):
            self._mode = "validate"
        self._lock = RLock()
        self._records = list(self.get_data("records") or [])[:50]
        self._metadata = MetadataResolver()
        self._timers = set()
        self._retried = set()
        self._stopped = False
        self._snapshots = None
        self._snapshot_error = False
        plans = self.get_data("inherit_plans") or []
        if self._enabled and (self._mode == "inherit" or plans):
            try:
                self._snapshots = TaskSnapshots(plans, lambda value: self.save_data("inherit_plans", value))
                self._snapshots.start()
            except Exception as error:
                self._snapshot_error = True
                logger.warn(f"【{self.plugin_name}】任务快照初始化失败：{type(error).__name__}")

    def get_state(self):
        return self._enabled

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return []

    def get_service(self):
        return []

    def stop_service(self):
        self._stopped = True
        if getattr(self, "_metadata", None):
            self._metadata.stop()
        if getattr(self, "_snapshots", None):
            self._snapshots.stop()
        for timer in tuple(getattr(self, "_timers", ())):
            timer.cancel()

    @staticmethod
    def _bounded_numbers(meta, kind):
        """Check bounds before host properties allocate a season/episode range."""
        maximum = 100 if kind == "season" else 10000
        begin = getattr(meta, f"begin_{kind}", None)
        end = getattr(meta, f"end_{kind}", None)
        for value in (begin, end):
            if value is not None and (type(value) is not int or value < 0 or value > maximum):
                raise CheckUnavailable("识别出的季集超出可校验范围")
        if begin is not None and end is not None and end < begin:
            raise CheckUnavailable("识别出的季集范围倒序")
        values = tuple(getattr(meta, f"{kind}_list", ()) or ())
        if any(type(v) is not int or v < 0 or v > maximum for v in values):
            raise CheckUnavailable("识别出的季集格式无效")
        return values

    def _target(self, data, subscription):
        meta = data.context.meta_info
        seasons = self._bounded_numbers(meta, "season")
        if len(seasons) != 1:
            raise CheckUnavailable("无法唯一确定本次下载的目标季（多季合集暂不校验）")
        declared = set(self._bounded_numbers(meta, "episode"))
        requested = set(data.episodes or declared)
        if any(type(e) is not int or e < 0 or e > 10000 for e in requested):
            raise CheckUnavailable("本次下载的目标集数无效")
        # A selected subset may have other episodes in the torrent. Whole-season
        # candidates may contain earlier episodes already present in the library.
        allow_extra = bool(requested and requested != declared)
        if not requested and subscription is not None:
            if getattr(subscription, "season", None) != seasons[0]:
                raise CheckUnavailable("订阅季号与本次候选季号不一致")
            total = int(getattr(subscription, "total_episode", 0) or 0)
            start = int(getattr(subscription, "start_episode", 1) or 1)
            if not 0 <= start <= total <= 10000 or not total:
                raise CheckUnavailable("整季资源缺少可靠的目标集数范围")
            requested = set(range(start, total + 1))
            allow_extra = True
        expected = frozenset((seasons[0], e) for e in requested)
        if not expected:
            raise CheckUnavailable("无法确定本次下载的目标集数")
        return expected, allow_extra

    @staticmethod
    def _video_extensions():
        return frozenset(e.lower() for e in settings.RMT_MEDIAEXT) or DEFAULT_VIDEO_EXTENSIONS

    def _load_resource(self, data):
        origin = data.origin or ""
        subscription = None
        if origin.startswith("Subscribe|"):
            subscription = SubscribeChain().get_subscribe_by_source(origin)
            if subscription is None:
                raise CheckUnavailable("无法读取本次订阅及其识别词，未猜测整理结果")
        expected, allow_extra = self._target(data, subscription)
        custom_words = getattr(subscription, "custom_words", None)
        words = custom_words.split("\n") if custom_words else None
        torrent = data.context.torrent_info
        if not torrent.enclosure:
            raise CheckUnavailable("资源没有可用的下载地址")

        # Reuse the host's credentials, indirect-URL handling, proxy and .torrent
        # cache. This fetches metadata only, never adds a downloader task.
        if torrent.enclosure.lower().startswith("magnet:"):
            content = torrent.enclosure
        else:
            content, _, _ = DownloadChain().download_torrent(torrent)
        if isinstance(content, str) and content.lower().startswith("magnet:"):
            try:
                content = self._metadata.resolve(
                    content,
                    proxy=getattr(settings, "PROXY", None) if getattr(torrent, "site_proxy", False) else None,
                )
            except MetadataUnavailable as error:
                raise CheckUnavailable(str(error)) from None
        if not content or isinstance(content, str):
            raise CheckUnavailable("未取得可解析的 .torrent 文件")
        # Parse from the content rather than trusting a partial cached list.
        folder, files = TorrentHelper().get_fileinfo_from_torrent_content(content)
        if len(files) > 10000:
            raise CheckUnavailable("种子文件数过多，未执行不完整的校验")
        try:
            videos = list(video_paths(files, self._video_extensions()))
        except ValueError:
            raise CheckUnavailable("种子内部文件路径不是有效的相对路径") from None
        root = Path("/episodeguard")
        if folder:
            try:
                list(video_paths([f"{folder}/probe.mkv"], {".mkv"}))
            except ValueError:
                raise CheckUnavailable("种子根目录不是有效的相对路径") from None
            root /= folder
        return {"expected": expected, "allow_extra": allow_extra, "words": words,
                "original_custom_words": custom_words, "content": content,
                "files": files, "videos": videos, "root": root}

    def _inspect(self, data):
        resource = self._load_resource(data)
        parsed = []
        for name in resource["videos"]:
            meta = MetaInfoPath(resource["root"] / name, custom_words=resource["words"])
            parsed.append(FileEpisodes(name, self._bounded_numbers(meta, "season"),
                                       self._bounded_numbers(meta, "episode")))
        return check_files(resource["expected"], parsed, allow_extra_episodes=resource["allow_extra"]), describe(resource["expected"])

    def _prepare_inheritance(self, data):
        """Freeze a precise single-file mapping before MP adds the task."""
        if not self._snapshots or self._snapshot_error:
            raise OverrideUnavailable("任务识别快照未就绪，未允许未经覆盖的资源下载")
        resource = self._load_resource(data)
        from app.core.meta.words import WordsMatcher
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey
        from app.utils.media import resolve_media_identity
        base_words = resource["words"]
        if not base_words:
            base_words = list(SystemConfigOper().get(SystemConfigKey.CustomIdentifiers) or [])
        identity = resolve_media_identity(media=data.context.media_info)
        if not all(identity):
            raise OverrideUnavailable("无法确定搜索匹配的媒体身份")
        text, mapped, names = build_words(
            resource["files"], resource["expected"], base_words, WordsMatcher().prepare,
            self._video_extensions(), frozenset(getattr(settings, "RMT_SUBEXT", [".srt", ".ass", ".ssa"])),
        )
        for name in mapped:
            meta = MetaInfoPath(resource["root"] / name, custom_words=text.split("\n"))
            actual = frozenset((s, e) for s in self._bounded_numbers(meta, "season")
                               for e in self._bounded_numbers(meta, "episode"))
            if meta.type != MediaType.TV or actual != resource["expected"]:
                raise OverrideUnavailable("生成的任务识别词未通过当前 MP 识别器预检")
        plan = {"hashes": torrent_hashes(resource["content"]), "identity": list(identity),
                "target": list(next(iter(resource["expected"]))), "origin": data.origin or "",
                "words": text, "files": mapped, "names": names,
                "original_custom_words": resource["original_custom_words"]}
        self._snapshots.prepare(plan)
        return Verdict("inherit", "任务规则已预检并保存，下载历史写入时将原子绑定"), describe(resource["expected"])

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event = None):
        if not self._enabled or not event or not event.event_data:
            return
        data = event.event_data
        # Respect earlier vetoes; never undo another plugin's decision.
        if data.cancel:
            return
        origin = data.origin or ""
        if self._only_subscriptions and not origin.startswith("Subscribe|"):
            return
        context = data.context
        if not context or not context.meta_info or not context.torrent_info or not context.media_info:
            return
        if context.media_info.type != MediaType.TV:
            return
        expected = ""
        try:
            verdict, expected = (self._prepare_inheritance(data) if self._mode == "inherit"
                                 else self._inspect(data))
        except Exception as error:
            # Avoid emitting signed URLs, cookies or provider errors to the UI.
            reason = str(error) if isinstance(error, (CheckUnavailable, OverrideUnavailable)) else "校验异常，详见错误类型"
            verdict = Verdict("unknown", reason)
            logger.warn(f"【{self.plugin_name}】无法校验，异常类型：{type(error).__name__}")
        blocked = verdict.status == "mismatch" or (
            verdict.status == "unknown" and (self._mode == "inherit" or self._block_unverifiable)
        )
        if blocked:
            data.cancel = True
            data.source = self.plugin_name
            data.reason = verdict.reason
        title = context.torrent_info.title or "未命名资源"
        site = context.torrent_info.site_name or ""
        if verdict.status not in ("match", "inherit"):
            logger.info(f"【{self.plugin_name}】{'跳过' if blocked else '放行未校验资源'}：{title}；{verdict.reason}")
            self._record(title, site, expected, verdict, blocked)
        elif verdict.status == "inherit":
            self._record(title, site, expected, verdict, False, result="覆盖规则待绑定")
        else:
            logger.debug(f"【{self.plugin_name}】校验通过：{title}；{verdict.reason}")

    @staticmethod
    def _same_file(left, right):
        return (left.storage == right.storage
                and Path(left.path) == Path(right.path))

    def _file_tasks(self, fileitem):
        from app.chain.transfer import TransferChain
        return [t for job in TransferChain().jobview.list_jobs() for t in job.tasks
                if self._same_file(t.fileitem, fileitem)]

    def _matches_snapshot(self, meta, snapshot):
        return (meta is not None and meta.type == MediaType.TV
                and frozenset((s, e) for s in self._bounded_numbers(meta, "season")
                              for e in self._bounded_numbers(meta, "episode"))
                == frozenset([tuple(snapshot["target"])]))

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event = None):
        if not self._enabled or not self._snapshots or not event or not event.event_data:
            return
        data = event.event_data
        value = data.get("hash")
        if not value:
            return
        try:
            row = self._snapshots.ready([value.lower()])
            if row:
                self._record(row.get("torrent_name") or "下载任务", row.get("torrent_site") or "",
                             describe([tuple(row["note"][MARKER]["target"])]),
                             Verdict("inherit", "下载历史已绑定任务识别词；原生整理将使用该快照"),
                             False, result="覆盖规则已绑定")
            elif self._snapshots.matching_hash(value):
                context = data.get("context")
                torrent = getattr(context, "torrent_info", None)
                self._record(getattr(torrent, "title", None) or "下载任务",
                             getattr(torrent, "site_name", None) or "", "",
                             Verdict("unknown", "下载历史未能绑定快照，请查看来源、媒体、季集或识别词是否变化；暂停自动整理并检查该任务"),
                             True, result="覆盖规则未绑定")
                logger.warn(f"【{self.plugin_name}】下载历史未能绑定任务快照，请检查任务；不应依赖原文件集号整理")
        except Exception as error:
            logger.warn(f"【{self.plugin_name}】绑定状态读取失败：{type(error).__name__}")

    @eventmanager.register(ChainEventType.TransferIntercept)
    def on_transfer_intercept(self, event: Event = None):
        """Check the queued task's hash, never associate by a filename alone."""
        if not self._enabled or not self._snapshots or not event or not event.event_data:
            return
        data = event.event_data
        if data.cancel:
            return
        try:
            tasks = self._file_tasks(data.fileitem)
            hashes = {str(t.download_hash).lower() for t in tasks if t.download_hash}
            protected = []
            name = Path(data.fileitem.path).name
            for value in hashes:
                row = self._snapshots.ready([value])
                snapshot = row["note"][MARKER] if row else None
                plans = self._snapshots.matching_hash(value)
                if ((snapshot and name in snapshot["names"])
                        or any(name in p["names"] for p in plans)):
                    protected.append((row, snapshot))
            if not protected:
                return
            if len(hashes) != 1 or len(protected) != 1:
                raise OverrideUnavailable("整理任务与下载哈希的对应关系不唯一")
            row, snapshot = protected[0]
            if not row or not self._matches_snapshot(data.meta, snapshot):
                raise OverrideUnavailable("任务快照未就绪或整理季集未沿用搜索结果")
            from app.utils.media import resolve_media_identity
            if tuple(resolve_media_identity(media=data.mediainfo)) != tuple(snapshot["identity"]):
                raise OverrideUnavailable("整理媒体与下载任务的搜索结果不一致")
        except Exception as error:
            data.cancel = True
            data.source = self.plugin_name
            data.reason = "EpisodeGuard 快照保护：" + (
                str(error) if isinstance(error, OverrideUnavailable) else "无法核实整理任务快照")
            logger.warn(f"【{self.plugin_name}】{data.reason}（{type(error).__name__}）")

    @eventmanager.register(EventType.TransferFailed)
    def on_transfer_failed(self, event: Event = None):
        """Requeue only our stale-metadata failure, once, using frozen words."""
        if not self._enabled or not self._snapshots or not event or not event.event_data:
            return
        data = event.event_data
        message = getattr(data.get("transferinfo"), "message", "") or ""
        value, fileitem = data.get("download_hash"), data.get("fileitem")
        if not message.startswith("EpisodeGuard 快照保护：") or not value or not fileitem:
            return
        key = (value, fileitem.storage, fileitem.path)
        with self._lock:
            if key in self._retried or len(self._retried) >= 128:
                return
            self._retried.add(key)
        self._schedule_retry(data, 0)

    def _schedule_retry(self, data, attempt):
        generation = self._timers
        def run():
            with self._lock:
                generation.discard(timer)
            if self._stopped or generation is not self._timers:
                return
            try:
                row = self._snapshots.ready([data["download_hash"]])
                tasks = self._file_tasks(data["fileitem"])
                waiting = (not row or any(t.state != "failed" for t in tasks))
                if waiting and attempt < 2:
                    self._schedule_retry(data, attempt + 1)
                    return
                if waiting or len(tasks) > 1:
                    raise OverrideUnavailable("快照或原整理任务尚未就绪，请稍后手动重新整理")
                if tasks and tasks[0].download_hash != data["download_hash"]:
                    raise OverrideUnavailable("原整理任务哈希已改变，未重试")
                meta = MetaInfoPath(Path(data["fileitem"].path), custom_words=row["custom_words"].split("\n"))
                if not self._matches_snapshot(meta, row["note"][MARKER]):
                    raise OverrideUnavailable("任务识别词重试预检失败")
                from app.chain.transfer import TransferChain
                chain = TransferChain()
                media = chain.recognize_media(mtype=MediaType.TV,
                                              source=row.get("media_source"), mediaid=row.get("media_id"),
                                              tmdbid=row.get("tmdbid"), doubanid=row.get("doubanid"),
                                              bangumiid=row.get("bangumiid"), anilistid=row.get("anilistid"),
                                              episode_group=row.get("episode_group"))
                if not media:
                    raise OverrideUnavailable("无法识别快照中的媒体，未重试")
                if self._stopped or generation is not self._timers:
                    return
                # MP normally removes a finished failed job itself. Only remove
                # a remaining failed task after confirming its hash above.
                if tasks:
                    chain.jobview.remove_task(data["fileitem"])
                chain.do_transfer(fileitem=data["fileitem"], meta=meta,
                                  mediainfo=media,
                                  download_hash=data["download_hash"], downloader=data.get("downloader"),
                                  force=True, background=True)
            except Exception as error:
                reason = str(error) if isinstance(error, OverrideUnavailable) else "重试异常"
                logger.warn(f"【{self.plugin_name}】快照整理重试停止：{reason}（{type(error).__name__}）")
        timer = Timer(2 ** attempt, run)
        timer.daemon = True
        with self._lock:
            generation.add(timer)
        timer.start()

    def _record(self, title, site, expected, verdict, blocked, result=None):
        try:
            with self._lock:
                self._records.insert(0, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "title": title[:300], "site": site[:100], "expected": expected[:200],
                    "result": result or ("跳过" if blocked else "放行（未校验）"), "reason": verdict.reason[:800],
                })
                del self._records[50:]
                self.save_data("records", list(self._records))
        except Exception as error:
            # A records/UI failure must not turn a veto into a download.
            logger.warn(f"【{self.plugin_name}】记录保存失败：{type(error).__name__}")

    def get_form(self):
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用下载季集校验"}},
            {"component": "VSelect", "props": {"model": "mode", "label": "处理模式", "items": [
                {"title": "校验一致性（默认）", "value": "validate"},
                {"title": "沿用搜索季集（custom_words）", "value": "inherit"},
            ]}},
            {"component": "VSwitch", "props": {"model": "only_subscriptions", "label": "仅检查订阅下载"}},
            {"component": "VSwitch", "props": {"model": "block_unverifiable", "label": "无法校验时跳过资源",
                                                                       "v-show": "mode === 'validate'"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "v-show": "mode === 'validate'"},
             "text": "比较识别后的文件与标题季集。明确冲突始终跳过；无法识别、读取失败或磁力超时按开关处理。"},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "v-show": "mode === 'inherit'"},
             "text": "信任搜索匹配的季集，自动生成单集任务专属识别词。仅支持一个目标集号和一个正片；规则无法准备时跳过，不使用校验模式的开关。请使用下载器监控整理。已绑定的规则会保留在下载历史中。"},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
             "text": "只检查 MP 提供的资源：种子地址沿用 MP 获取逻辑，磁力链只获取 BT 元数据，不抓取详情页。每条磁力独立最多等 15 秒，前面资源超时不影响后面资源。"},
        ]}], {"enabled": False, "mode": "validate", "only_subscriptions": True, "block_unverifiable": True}

    def get_page(self):
        with self._lock:
            records = list(self._records)
        if not records:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
                     "text": "暂无冲突或无法校验的记录。"}]
        return [{"component": "VCard", "props": {"variant": "outlined", "class": "mb-3"},
                 "content": [
                     {"component": "VCardTitle", "text": item["title"]},
                     {"component": "VCardSubtitle", "text": f'{item["time"]} · {item["site"]} · {item["result"]}'},
                     {"component": "VCardText", "text": item["reason"]},
                 ]} for item in records]
