"""文件命名工具 — 复用 zotero_export.py 的命名逻辑."""

import re
from pathlib import Path

# 非法文件名字符（与 zotero_export.py 保持一致）
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# 文件名最大长度（保守 200 字符）
_MAX_FILENAME_LEN = 200

# 文件命名模板（与 zotero_export.py 保持一致）
_NAMING_FORMATS = {
    "title": "{title}",
    "author_year_title": "{author}_{year}_{title}",
    "authors_year_title": "{authors}_{year}_{title}",
}


def sanitize_path_segment(name: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """移除文件名中的非法字符，限制长度，去除首尾空白和点号。

    直接复刻 zotero_export.py 的 sanitize_path_segment()。
    """
    # 替换非法字符
    clean = _INVALID_FILENAME_CHARS.sub("_", name)
    # 合并连续下划线
    clean = re.sub(r"_+", "_", clean)
    # 去除首尾空白和点号
    clean = clean.strip(" .")
    # 截断到最大长度
    if len(clean) > max_len:
        stem, ext = clean.rsplit(".", 1) if "." in clean else (clean, "")
        if ext:
            stem = stem[: max_len - len(ext) - 1]
            clean = f"{stem}.{ext}"
        else:
            clean = clean[:max_len]
    # 空字符串回退
    return clean or "untitled"


def ensure_unique_path(path: Path) -> Path:
    """如果文件已存在，添加 _1, _2 后缀。

    复刻 zotero_export.py 的 _rename_duplicate()。
    """
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    parent = path.parent
    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{ext}"
        if not new_path.exists():
            return new_path
        counter += 1


def build_pdf_filename(
    paper,
    naming_format: str = "author_year_title",
) -> str:
    """根据论文元数据生成 PDF 文件名。

    Args:
        paper: Paper 对象，需有 title, authors, year 属性。
        naming_format: "title" | "author_year_title" | "authors_year_title"

    Returns:
        清理后的文件名（不含目录路径），例如 "Vaswani_2017_Attention_Is_All_You_Need.pdf"
    """
    title = paper.title if paper.title else "untitled"
    # 截断标题，避免文件名过长
    title_short = title[:120].strip()

    first_author = "Unknown"
    if paper.authors:
        # 取第一作者姓氏
        first_author = paper.authors[0].split()[-1] if paper.authors[0].split() else paper.authors[0]

    all_authors = "_".join(
        a.split()[-1] if a.split() else a for a in paper.authors[:3]
    ) if paper.authors else "Unknown"

    year = str(paper.year) if paper.year else "noyear"

    fmt = _NAMING_FORMATS.get(naming_format, _NAMING_FORMATS["author_year_title"])
    raw = fmt.format(
        author=first_author,
        authors=all_authors,
        year=year,
        title=title_short,
    )
    filename = sanitize_path_segment(raw) + ".pdf"
    return filename


def build_storage_path(paper, storage_dir: Path) -> Path:
    """构建完整存储路径: {storage_dir}/{source}/{year}/{filename}.pdf

    Args:
        paper: Paper 对象。
        storage_dir: 存储根目录。

    Returns:
        完整的目标路径。
    """
    year = str(paper.year) if paper.year else "noyear"
    source = paper.source.value
    filename = build_pdf_filename(paper)
    return storage_dir / source / year / filename
