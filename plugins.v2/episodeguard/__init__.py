"""Reject subscription torrents whose files would organize to other episodes."""

from datetime import datetime
from pathlib import Path
from threading import RLock

from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, MediaType

from app.core.event import Event, eventmanager
from app.log import logger
from app.core.metainfo import MetaInfoPath
from app.helper.torrent import TorrentHelper
from app.core.config import settings

from .policy import DEFAULT_VIDEO_EXTENSIONS, FileEpisodes, Verdict, check_files, describe, video_paths


class CheckUnavailable(ValueError):
    """A safe, user-facing explanation produced by this plugin itself."""


class EpisodeGuard(_PluginBase):
    plugin_name = "下载季集校验"
    plugin_desc = "下载前核对种子内部视频的季集，冲突时跳过并继续订阅择优。"
    plugin_icon = "https://raw.githubusercontent.com/z-henry/MoviePilot-Plugins/main/icons/episodeguard.svg"
    plugin_version = "1.0.0"
    plugin_author = "henryz"
    author_url = "https://github.com/z-henry"
    plugin_config_prefix = "episodeguard_"
    plugin_order = 30
    auth_level = 1

    def init_plugin(self, config=None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._only_subscriptions = bool(config.get("only_subscriptions", True))
        self._block_unverifiable = bool(config.get("block_unverifiable", True))
        self._lock = RLock()
        self._records = list(self.get_data("records") or [])[:50]

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
        # No timers, workers, patches, or persistent connections to stop.
        pass

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

    def _inspect(self, data):
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
        if not torrent.enclosure or torrent.enclosure.startswith("magnet:"):
            raise CheckUnavailable("磁力链或空下载地址不能在提交下载前取得内部文件清单")

        # Reuse the host's credentials, indirect-URL handling, proxy and .torrent
        # cache. This fetches metadata only, never adds a downloader task.
        content, folder, files = DownloadChain().download_torrent(torrent)
        if not content or isinstance(content, str):
            raise CheckUnavailable("未取得可解析的 .torrent 文件（读取失败或返回磁力链）")
        # Parse from the content rather than trusting a partial cached list.
        folder, files = TorrentHelper().get_fileinfo_from_torrent_content(content)
        if len(files) > 10000:
            raise CheckUnavailable("种子文件数过多，未执行不完整的校验")
        try:
            videos = list(video_paths(files, self._video_extensions()))
        except ValueError:
            raise CheckUnavailable("种子内部文件路径不是有效的相对路径") from None
        parsed = []
        root = Path("/episodeguard")
        if folder:
            try:
                list(video_paths([f"{folder}/probe.mkv"], {".mkv"}))
            except ValueError:
                raise CheckUnavailable("种子根目录不是有效的相对路径") from None
            root /= folder
        for name in videos:
            # Match the organizer: filenames may inherit seasons from directories.
            # The synthetic root is deliberately free of media/season tokens.
            meta = MetaInfoPath(root / name, custom_words=words)
            parsed.append(FileEpisodes(name, self._bounded_numbers(meta, "season"),
                                       self._bounded_numbers(meta, "episode")))
        return check_files(expected, parsed, allow_extra_episodes=allow_extra), describe(expected)

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
            verdict, expected = self._inspect(data)
        except Exception as error:
            # Avoid emitting signed URLs, cookies or provider errors to the UI.
            reason = str(error) if isinstance(error, CheckUnavailable) else "校验异常，详见错误类型"
            verdict = Verdict("unknown", reason)
            logger.warn(f"【{self.plugin_name}】无法校验，异常类型：{type(error).__name__}")
        blocked = verdict.status == "mismatch" or (
            verdict.status == "unknown" and self._block_unverifiable
        )
        if blocked:
            data.cancel = True
            data.source = self.plugin_name
            data.reason = verdict.reason
        title = context.torrent_info.title or "未命名资源"
        site = context.torrent_info.site_name or ""
        if verdict.status != "match":
            logger.info(f"【{self.plugin_name}】{'跳过' if blocked else '放行未校验资源'}：{title}；{verdict.reason}")
            self._record(title, site, expected, verdict, blocked)
        else:
            logger.debug(f"【{self.plugin_name}】校验通过：{title}；{verdict.reason}")

    def _record(self, title, site, expected, verdict, blocked):
        try:
            with self._lock:
                self._records.insert(0, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "title": title[:300], "site": site[:100], "expected": expected[:200],
                    "result": "跳过" if blocked else "放行（未校验）", "reason": verdict.reason[:800],
                })
                del self._records[50:]
                self.save_data("records", list(self._records))
        except Exception as error:
            # A records/UI failure must not turn a veto into a download.
            logger.warn(f"【{self.plugin_name}】记录保存失败：{type(error).__name__}")

    def get_form(self):
        return [{"component": "VForm", "content": [
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用下载季集校验"}},
            {"component": "VSwitch", "props": {"model": "only_subscriptions", "label": "仅检查订阅下载"}},
            {"component": "VSwitch", "props": {"model": "block_unverifiable", "label": "无法校验时跳过资源"}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal"},
             "text": "先读取种子内部文件，用整理识别器和订阅识别词核对季集。冲突时跳过当前候选，继续寻找资源。仅检查订阅下载默认开启。"},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal"},
             "text": "无法校验时默认跳过，包括磁力链、无法识别的文件和多季合集；关闭此选项会放行这类资源。读取 .torrent 可能增加一次站点请求，普通地址会复用 MP 的缓存。"},
        ]}], {"enabled": False, "only_subscriptions": True, "block_unverifiable": True}

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
