"""Pure checks shared by the MoviePilot adapter and offline tests."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import FrozenSet, Iterable, Tuple


Pair = Tuple[int, int]
DEFAULT_VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".webm",
})
_SAMPLE = re.compile(r"(?:^|[\s._\-\[\]])(?:sample|trailer)(?:$|[\s._\-\[\]])", re.I)


@dataclass(frozen=True)
class FileEpisodes:
    name: str
    seasons: Tuple[int, ...]
    episodes: Tuple[int, ...]


@dataclass(frozen=True)
class Verdict:
    status: str  # match / mismatch / unknown
    reason: str


def video_paths(files: Iterable[str], extensions=DEFAULT_VIDEO_EXTENSIONS):
    """Use relative torrent paths; skip conventional sample/trailer videos only."""
    for raw in files:
        name = str(raw).replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", name):
            raise ValueError("种子内部文件路径不是有效的相对路径")
        if path.suffix.lower() not in extensions:
            continue
        if any(_SAMPLE.search(part) for part in path.parts):
            continue
        yield name


def describe(pairs: Iterable[Pair], limit: int = 8) -> str:
    values = sorted(set(pairs))
    result = ", ".join(f"S{s:02d}E{e:02d}" for s, e in values[:limit])
    return result + (f" 等 {len(values)} 集" if len(values) > limit else "")


def check_files(expected: FrozenSet[Pair], files: Iterable[FileEpisodes], *,
                allow_extra_episodes: bool = False) -> Verdict:
    """All target episodes must exist. A conflicting season never passes."""
    if not expected:
        return Verdict("unknown", "无法确定本次下载的目标季集")
    target_seasons = {s for s, _ in expected}
    actual = set()
    unknown = []
    for file in files:
        if not file.episodes or len(file.seasons) != 1:
            unknown.append(file.name)
            continue
        season = file.seasons[0]
        if season not in target_seasons:
            return Verdict("mismatch", f"文件 {file.name} 属于 S{season:02d}，目标为 {describe(expected)}")
        actual.update((season, e) for e in file.episodes)
    if unknown:
        return Verdict("unknown", f"无法确定视频文件季集：{unknown[0]}" +
                       (f" 等 {len(unknown)} 个文件" if len(unknown) > 1 else ""))
    if not actual:
        return Verdict("unknown", "种子中没有可识别的正片视频文件")
    missing = expected - actual
    if missing:
        return Verdict("mismatch", f"目标 {describe(expected)}；文件为 {describe(actual)}；缺少 {describe(missing)}")
    extra = actual - expected
    if extra and not allow_extra_episodes:
        return Verdict("mismatch", f"目标 {describe(expected)}；文件多出 {describe(extra)}")
    return Verdict("match", f"文件季集与本次下载目标一致：{describe(expected)}")
