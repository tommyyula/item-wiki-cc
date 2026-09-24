"""File classification by extension, plus noise / version-name heuristics."""
from __future__ import annotations

import re

KINDS: dict[str, set[str]] = {
    "document": {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "xlsm", "key", "pages",
                 "numbers", "md", "txt", "rtf", "odt", "ods", "odp", "epub", "html", "htm", "vsdx", "vsd", "mm", "xmind"},
    "data": {"csv", "tsv", "json", "jsonl", "xml", "yaml", "yml", "sql", "db", "sqlite", "parquet", "xmi"},
    "image": {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "heic", "heif", "webp", "svg", "raw", "cr2", "nef", "dng", "ico"},
    "video": {"mp4", "mov", "m4v", "avi", "mkv", "wmv", "flv", "webm", "mpg", "mpeg", "3gp"},
    "audio": {"mp3", "m4a", "wav", "aac", "flac", "ogg", "wma", "amr", "aiff"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "dmg", "iso", "pkg"},
    "code": {"py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp", "h", "cs", "rb", "php", "sh",
             "ipynb", "swift", "kt", "css", "scss", "vue", "dart"},
    "design": {"fig", "sketch", "psd", "ai", "xd", "indd", "drawio", "graffle"},
    "email": {"eml", "msg", "mbox"},
    "executable": {"exe", "msi", "app", "apk", "ipa", "bin", "jar", "dll", "so", "dylib"},
}
EXT_TO_KIND = {ext: kind for kind, exts in KINDS.items() for ext in exts}

# Files never worth indexing.
NOISE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r", ".localized", ".dropbox", ".dropbox.attr"}
NOISE_PREFIXES = ("._", "~$", ".~lock")
# Directories skipped entirely (not descended into).
DEFAULT_EXCLUDE_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".idea",
                        ".dropbox.cache", ".Trash", ".Spotlight-V100", ".fseventsd", ".TemporaryItems"}

# Directory names that usually mean "old stuff" (includes the common typo "Achieve").
_ARCH = r"archi?e?ve[sd]?|achieve[sd]?|archiv"   # archive, archieve, achieve (common typos)
ARCHIVE_DIR_RE = re.compile(rf"^({_ARCH}|old|backup[s]?|bak|deprecated|obsolete|trash|旧|旧版|归档|备份|历史|过期)$|"
                            rf"(^|[\s_\-])({_ARCH}|backup|old)([\s_\-]|$)|^_?old[\s_\-]", re.IGNORECASE)

# Extensions where "version family" analysis is meaningful (human-authored documents).
VERSIONED_EXTS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "xlsm", "key", "pages", "numbers", "md",
                  "vsdx", "vsd", "xmind", "fig", "sketch", "psd", "ai", "drawio", "mp4", "mov"}


def classify(ext: str) -> str:
    return EXT_TO_KIND.get(ext.lower(), "other")


def is_noise(name: str) -> bool:
    return name in NOISE_NAMES or name.startswith(NOISE_PREFIXES)


def is_archive_dir(name: str) -> bool:
    return bool(ARCHIVE_DIR_RE.search(name))


# Strip version-ish decorations so "Plan_v3 (1).pptx" and "Plan final.pptx" share a family key.
_VERSION_PATTERNS = [
    r"\s*\(\d+\)$",                                   # "(1)" duplicate-download suffix
    r"[\s_\-]*(copy|副本|拷贝)(\s*\d+)?$",
    r"[\s_\-]*(final|最终版?|定稿|终版|draft|草稿|修订版?|更新版?|new|latest|updated?|rev\d*)$",
    r"[\s_\-]*(?<![a-z])v(er(sion)?)?[\s_\-.]?\d+(\.\d+)*[a-z]?$",  # v2, V1.3, ver2 (not "DIEV_0709")
    r"[\s_\-]*(19|20)\d{2}[\-_.]?\d{2}[\-_.]?\d{2}$",   # 20240315 / 2024-03-15
    r"[\s_\-]*\d{6}$",                                  # 240315
    r"[\s_\-]+\d{4}$",                                   # _0709 (MMDD)
    r"[\s_\-]*(\d{1,2}[\-_.]\d{1,2})$",                 # 3.15 / 03-15
]
_VERSION_RES = [re.compile(p, re.IGNORECASE) for p in _VERSION_PATTERNS]


# Strict subset used for auto-skipping: only explicit version / copy markers, never dates
# (date-suffixed files are usually distinct snapshots, e.g. daily reports).
_STRICT_RES = [re.compile(p, re.IGNORECASE) for p in [
    r"\s*\(\d+\)$",
    r"[\s_\-]*(copy|副本|拷贝)(\s*\d+)?$",
    r"[\s_\-]*(final|最终版?|定稿|终版|draft|草稿|修订版?|更新版?|latest|updated?)$",
    r"[\s_\-]*(?<![a-z])v(er(sion)?)?[\s_\-.]?\d+(\.\d+)*[a-z]?$",
]]


def version_key(stem: str) -> str:
    s = stem.strip()
    while True:
        before = s
        for rx in _STRICT_RES:
            s = rx.sub("", s).strip()
        if s == before or not s:
            break
    s = s or stem
    return re.sub(r"[\s_\-]+", " ", s).strip().lower()


_VER_NUM_RE = re.compile(r"(?<![a-z])v(?:er(?:sion)?)?[\s_\-.]?(\d+(?:\.\d+)*)", re.IGNORECASE)


def version_tuple(stem: str) -> tuple[int, ...] | None:
    """Last explicit version number in a stem: 'Deck-v3.5.3 (1)' -> (3, 5, 3). None if absent."""
    m = _VER_NUM_RE.findall(stem)
    return tuple(int(x) for x in m[-1].split(".")) if m else None


def family_key(stem: str) -> str:
    """Normalize a filename stem to a version-family key. Loops until stable."""
    s = stem.strip()
    while True:
        before = s
        for rx in _VERSION_RES:
            s = rx.sub("", s).strip()
        if s == before or not s:
            break
    s = s or stem
    return re.sub(r"[\s_\-]+", " ", s).strip().lower()
