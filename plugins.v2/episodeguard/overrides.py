"""Generate exact, per-file recognition rules for a frozen single episode."""

from pathlib import PurePosixPath
import re

from .policy import video_paths


MARKER = "episodeguard_snapshot"
MAX_WORDS_SIZE = 256 * 1024


class OverrideUnavailable(ValueError):
    """A fixed explanation for a mapping that cannot be prepared safely."""


def season_episode(seasons, episodes):
    """Read one season and one episode from a native download history row."""
    season = re.fullmatch(r"S(\d+)(?:-S?(\d+))?", str(seasons or ""), re.I)
    episode = re.fullmatch(r"E(\d+)(?:-E?(\d+))?", str(episodes or ""), re.I)
    if not season or not episode:
        return None
    if ((season[2] and int(season[2]) != int(season[1]))
            or (episode[2] and int(episode[2]) != int(episode[1]))):
        return None
    return int(season[1]), int(episode[1])


def row_identity(row):
    """Use the native media key, falling back to older history identifiers."""
    if row.get("media_source") and row.get("media_id"):
        return str(row["media_source"]), str(row["media_id"])
    for source, key in (("themoviedb", "tmdbid"), ("douban", "doubanid"),
                        ("bangumi", "bangumiid"), ("anilist", "anilistid")):
        if row.get(key) is not None:
            return source, str(row[key])
    return None


def compatible_row(row, plan):
    """A reused hash may not be bound to another media identity or episode."""
    return (row_identity(row) == tuple(plan["identity"])
            and season_episode(row.get("seasons"), row.get("episodes")) == tuple(plan["target"]))


def build_words(files, expected, base_words, prepare, video_extensions, subtitle_extensions):
    """Append exact rules after existing conversions; retain technical filename tokens."""
    if len(expected) != 1:
        raise OverrideUnavailable("沿用模式目前只支持一个明确的目标季集，不猜测合集映射")
    videos = list(video_paths(files, video_extensions))
    if len(videos) != 1:
        raise OverrideUnavailable("沿用模式需要且仅允许一个正片视频文件")
    main = PurePosixPath(videos[0])
    # Word matching is on individual path components, so duplicate basenames
    # in other directories (including samples) cannot be distinguished.
    if sum(PurePosixPath(str(f).replace("\\", "/")).name == main.name for f in files) != 1:
        raise OverrideUnavailable("种子内存在同名文件，无法生成精确的任务识别覆盖")
    mapped = [videos[0]]
    for name in video_paths(files, subtitle_extensions):
        path = PurePosixPath(name)
        if path.parent == main.parent and (path.stem == main.stem or path.stem.startswith(main.stem + ".")):
            mapped.append(name)
    if len(mapped) > 50:
        raise OverrideUnavailable("需要覆盖的附加文件过多")
    season, episode = next(iter(expected))
    words = list(base_words or [])
    words.append("# EpisodeGuard: task-specific recognition snapshot")
    names = []
    for name in mapped:
        original = PurePosixPath(name).name
        if sum(PurePosixPath(str(f).replace("\\", "/")).name == original for f in files) != 1:
            raise OverrideUnavailable("种子内存在同名文件，无法生成精确的任务识别覆盖")
        converted, _ = prepare(original, custom_words=base_words or ["# frozen empty words"])
        if (not converted or len(converted) > 4096
                or any(t in converted for t in ("\n", "\r", "=>", "<>", ">>"))):
            raise OverrideUnavailable("文件名不支持生成精确识别覆盖")
        path = PurePosixPath(converted)
        # MP supports explicit season/episode tags after word preprocessing.
        # Equal range endpoints still render as E26-E26 in native rename/history.
        # Use scalar tags; the caller rejects any residual source range in preflight.
        result = path.stem + f" {{[type=tv;s={season};e={episode}]}}" + path.suffix
        # Replacement strings interpret backslashes; literal ones must be escaped.
        rule = "^" + re.escape(converted) + "$ => " + result.replace("\\", "\\\\")
        words.append(rule)
        names.append(original)
    text = "\n".join(words)
    if len(text.encode()) > MAX_WORDS_SIZE:
        raise OverrideUnavailable("任务识别词快照超过大小上限")
    return text, mapped, names


def torrent_hashes(content):
    """Include v1, v2 and the v2 client-compatible 20-byte representation."""
    try:
        import libtorrent as lt
        hashes = lt.torrent_info(content).info_hashes()
        result = []
        if hashes.has_v1():
            result.append(str(hashes.v1))
        if hashes.has_v2():
            value = str(hashes.v2)
            result.extend((value, value[:40]))
        if result:
            return sorted(set(result))
    except Exception:
        pass
    raise OverrideUnavailable("无法确定任务的种子哈希，未生成识别覆盖")
