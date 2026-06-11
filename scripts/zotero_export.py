#!/usr/bin/env python3
"""
Zotero 参考文献 PDF 批量导出工具
==================================

从 Zotero 数据库中批量导出 PDF 附件，按照 Zotero 的收藏集（Collection）层级结构
重建输出目录。

用法:
    python scripts/zotero_export.py [OPTIONS]

依赖: 仅 Python 标准库（sqlite3），无需安装第三方包。
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

# ─── 常量 ────────────────────────────────────────────────────────────────

# 各平台 Zotero 数据目录的默认位置（基本目录，不含具体 profile）
_ZOTERO_BASE_DIRS: dict[str, list[str]] = {
    "win32": [
        os.path.expandvars(r"%APPDATA%\Zotero\Zotero"),
        os.path.expandvars(r"%LOCALAPPDATA%\Zotero\Zotero"),
        os.path.expandvars(r"%USERPROFILE%\Zotero"),
    ],
    "darwin": [
        os.path.expanduser("~/Library/Application Support/Zotero"),
    ],
    "linux": [
        os.path.expanduser("~/.zotero/zotero"),
        os.path.expanduser("~/Zotero"),
        os.path.expanduser("~/snap/zotero/common/.zotero/zotero"),
    ],
}

# 无效文件名字符（Windows 文件名禁用字符 + macOS/Linux 保守处理）
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# 文件名最大长度（byte-safe, 保守 200 字符）
_MAX_FILENAME_LEN = 200

# 文件命名模板
_NAMING_FORMATS = {
    "title": "{title}",
    "author_year_title": "{author}_{year}_{title}",
    "authors_year_title": "{authors}_{year}_{title}",
}


# ─── 路径工具 ────────────────────────────────────────────────────────────


def sanitize_path_segment(name: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """移除文件名中的非法字符，限制长度，去除首尾空白和点号。"""
    # 替换非法字符
    clean = _INVALID_FILENAME_CHARS.sub("_", name)
    # 合并连续下划线
    clean = re.sub(r"_+", "_", clean)
    # 去除首尾空白和点号
    clean = clean.strip(" .")
    # 截断到最大长度
    if len(clean) > max_len:
        # 保留扩展名
        stem, ext = os.path.splitext(clean)
        stem = stem[: max_len - len(ext)]
        clean = stem + ext
    # 空字符串回退
    return clean or "untitled"


def ensure_dir(path: Path) -> None:
    """创建目录（含父目录），存在则静默跳过。"""
    path.mkdir(parents=True, exist_ok=True)


# ─── Zotero 数据目录探测 ──────────────────────────────────────────────────


def detect_zotero_dir(zotero_dir: Optional[str] = None) -> Path:
    """
    定位 Zotero 数据目录。

    优先级:
    1. 用户显式指定的路径
    2. 从 profiles.ini 读取默认 profile
    3. 按平台搜索候选目录

    返回包含 zotero.sqlite 的目录路径。
    """
    if zotero_dir:
        candidate = Path(zotero_dir).expanduser().resolve()
        _validate_zotero_dir(candidate)
        return candidate

    # 按平台尝试
    platform = sys.platform
    base_dirs = _ZOTERO_BASE_DIRS.get(platform, _ZOTERO_BASE_DIRS["linux"])

    for base in base_dirs:
        base_path = Path(base)
        if not base_path.exists():
            continue

        # 目录本身是否就是 profile 目录（包含 zotero.sqlite）
        if (base_path / "zotero.sqlite").exists():
            return base_path.resolve()

        # 先尝试 profiles.ini
        profiles_ini = base_path / "profiles.ini"
        if profiles_ini.exists():
            profile_dir = _parse_profiles_ini(profiles_ini, base_path)
            if profile_dir:
                return profile_dir

        # 回退：找任意 profile 子目录（包含 zotero.sqlite）
        for child in base_path.iterdir():
            if child.is_dir() and (child / "zotero.sqlite").exists():
                return child.resolve()

    # 尝试直接读取 prefs.js 找最近使用的 profile
    for base in base_dirs:
        base_path = Path(base)
        prefs = base_path / "prefs.js"
        if prefs.exists():
            result = _parse_prefs_js(prefs, base_path)
            if result:
                return result

    raise FileNotFoundError(
        "无法自动定位 Zotero 数据目录。请使用 --zotero-dir 显式指定。\n"
        f"已搜索: {', '.join(str(Path(d)) for d in base_dirs)}"
    )


def _validate_zotero_dir(path: Path) -> None:
    """验证 Zotero 数据目录（需包含 zotero.sqlite）。"""
    if not path.exists():
        raise FileNotFoundError(f"目录不存在: {path}")
    if not (path / "zotero.sqlite").exists():
        raise FileNotFoundError(
            f"目录中未找到 zotero.sqlite: {path}\n"
            f"请确认这是 Zotero 数据目录（应包含 zotero.sqlite 和 storage/）。"
        )


def _parse_profiles_ini(ini_path: Path, base_dir: Path) -> Optional[Path]:
    """
    解析 profiles.ini 找到默认/最近使用的 profile。

    格式 (类 INI):
        [Profile0]
        Name=default
        Path=Profiles/xxxxxxxx.default
        Default=1

        [Profile1]
        Name=xxx
        Path=Profiles/yyyyyyyy.xxx
    """
    try:
        content = ini_path.read_text(encoding="utf-8")
    except OSError:
        return None

    current_section: dict[str, str] = {}
    profiles: list[dict[str, str]] = []

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            if current_section:
                profiles.append(current_section)
            current_section = {}
        elif "=" in line and current_section is not None:
            key, _, value = line.partition("=")
            current_section[key.strip()] = value.strip()

    if current_section:
        profiles.append(current_section)

    # 优先选 Default=1 的
    for p in profiles:
        if p.get("Default") == "1":
            return _resolve_profile_path(p, base_dir)

    # 回退到第一个
    if profiles:
        return _resolve_profile_path(profiles[0], base_dir)

    return None


def _resolve_profile_path(profile: dict[str, str], base_dir: Path) -> Optional[Path]:
    """根据 profile 条目解析实际路径。"""
    path_str = profile.get("Path", "")
    if not path_str:
        return None

    # Path 可能是相对路径或绝对路径
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    if candidate.exists() and (candidate / "zotero.sqlite").exists():
        return candidate.resolve()

    return None


def _parse_prefs_js(prefs_path: Path, base_dir: Path) -> Optional[Path]:
    """
    从 prefs.js 中解析 Zotero 数据目录路径。

    Zotero 7 可能用 'extensions.zotero.dataDir'
    Zotero 6 可能用 'extensions.zotero.lastDataDir' 或 'extensions.zotero.dataDir'
    """
    try:
        content = prefs_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # 匹配 user_pref("key", "value");
    for line in content.splitlines():
        for key in (
            "extensions.zotero.dataDir",
            "extensions.zotero.lastDataDir",
        ):
            pattern = rf'user_pref\("{re.escape(key)}",\s*"(.*?)"\);'
            m = re.search(pattern, line)
            if m:
                candidate = Path(m.group(1))
                if candidate.exists() and (candidate / "zotero.sqlite").exists():
                    return candidate.resolve()

    return None


# ─── Zotero 数据库查询 ────────────────────────────────────────────────────


class ZoteroExporter:
    """Zotero PDF 导出器 — 读取 SQLite 数据库，导出 PDF 到按 Collection 组织的目录。"""

    def __init__(
        self,
        zotero_dir: Optional[str] = None,
        output_dir: str = "./zotero_export",
        naming: str = "author_year_title",
        duplicates: str = "rename",
        first_collection_only: bool = False,
        verbose: bool = False,
    ):
        self.zotero_dir = detect_zotero_dir(zotero_dir)
        self.output_dir = Path(output_dir).resolve()
        self.naming = naming
        self.duplicates = duplicates
        self.first_collection_only = first_collection_only
        self.verbose = verbose

        self._conn: Optional[sqlite3.Connection] = None

        if self.naming not in _NAMING_FORMATS:
            raise ValueError(
                f"不支持的命名格式: {self.naming}。可选: {', '.join(_NAMING_FORMATS)}"
            )
        if self.duplicates not in ("rename", "skip", "overwrite"):
            raise ValueError(
                f"不支持的重复处理策略: {self.duplicates}。可选: rename, skip, overwrite"
            )

    # ── 数据库连接 ──────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """
        建立到 zotero.sqlite 的只读连接。

        为避免 Zotero 运行时数据库被锁定，先将 zotero.sqlite 复制到临时文件，
        在临时副本上操作，close 时自动清理。
        """
        db_path = self.zotero_dir / "zotero.sqlite"

        # 复制到临时文件（避免 Zotero 运行时的锁冲突）
        self._tmp_db = tempfile.NamedTemporaryFile(
            suffix=".sqlite", prefix="zotero_export_", delete=False
        )
        self._tmp_db_path = Path(self._tmp_db.name)
        self._tmp_db.close()  # 关闭句柄，让 sqlite3 自己打开

        shutil.copy2(db_path, self._tmp_db_path)

        # 同时复制 WAL/SHM 文件（如果存在），它们可能包含最新数据
        for suffix in ("-wal", "-shm"):
            wal_path = db_path.with_name(db_path.name + suffix)
            if wal_path.exists():
                shutil.copy2(wal_path, self._tmp_db_path.with_name(
                    self._tmp_db_path.name + suffix
                ))

        self._conn = sqlite3.connect(str(self._tmp_db_path), uri=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA query_only=ON")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        # 清理临时文件
        if hasattr(self, "_tmp_db_path"):
            try:
                self._tmp_db_path.unlink(missing_ok=True)
            except OSError:
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未连接，请先调用 connect()")
        return self._conn

    @property
    def storage_dir(self) -> Path:
        return self.zotero_dir / "storage"

    # ── Collection 树 ───────────────────────────────────────────────────

    def build_collection_tree(self) -> dict[int, str]:
        """
        构建 collectionID → 完整路径 的映射。

        递归地从根出发，为每个 collection 拼接完整的层级路径。
        返回 {collectionID: "一级/二级/三级", ...}。
        """
        rows = self.conn.execute(
            "SELECT collectionID, collectionName, parentCollectionID FROM collections"
        ).fetchall()

        # 构建 ID → (name, parent_id) 映射
        nodes: dict[int, tuple[str, Optional[int]]] = {}
        for r in rows:
            nodes[r["collectionID"]] = (
                sanitize_path_segment(r["collectionName"], max_len=100),
                r["parentCollectionID"],
            )

        # 递归构建完整路径（带缓存避免重复计算）
        cache: dict[int, str] = {}

        def get_path(cid: int) -> str:
            if cid in cache:
                return cache[cid]
            if cid not in nodes:
                return ""
            name, parent_id = nodes[cid]
            if parent_id is None or parent_id == 0:
                cache[cid] = name
            else:
                parent_path = get_path(parent_id)
                cache[cid] = (
                    f"{parent_path}/{name}" if parent_path else name
                )
            return cache[cid]

        for cid in nodes:
            get_path(cid)

        return cache

    # ── PDF 条目查询 ────────────────────────────────────────────────────

    def get_all_pdf_items(self, target_collection: Optional[str] = None) -> list[dict]:
        """
        查询所有带 PDF 附件的文献条目。

        返回列表，每项 dict:
            - item_id: int              (父条目的 itemID)
            - key: str                  (父条目的 Zotero key)
            - attachment_item_id: int   (附件条目的 itemID)
            - attachment_key: str       (附件条目的 Zotero key)
            - attachment_path: str      (如 "storage:KEY/filename.pdf")
            - title: str
            - date: str
            - filename: str             (PDF 文件名)
        """
        query = """
            SELECT
                parent.itemID      AS item_id,
                parent.key         AS key,
                att.itemID         AS attachment_item_id,
                att_item.key       AS attachment_key,
                att.path           AS attachment_path,
                att.contentType    AS content_type
            FROM itemAttachments att
            JOIN items parent ON parent.itemID = att.parentItemID
            JOIN items att_item ON att_item.itemID = att.itemID
            WHERE att.contentType = 'application/pdf'
               OR att.path LIKE '%.pdf'
            ORDER BY parent.itemID
        """
        rows = self.conn.execute(query).fetchall()

        items: list[dict] = []
        for r in rows:
            # 解析 attachment 中的 storage 路径，提取文件名
            path_raw = r["attachment_path"] or ""
            filename = self._extract_filename(path_raw)

            # 获取父条目的元数据（title, date, authors）
            meta = self.get_item_metadata(r["item_id"])

            items.append(
                {
                    "item_id": r["item_id"],
                    "key": r["key"],
                    "attachment_item_id": r["attachment_item_id"],
                    "attachment_key": r["attachment_key"],
                    "attachment_path": path_raw,
                    "filename": filename,
                    "title": meta["title"],
                    "date": meta.get("date", ""),
                    "year": meta.get("year", ""),
                    "authors": meta.get("authors", []),
                }
            )

        return items

    def _extract_filename(self, path_raw: str) -> str:
        """
        从 Zotero attachment path 提取文件名。

        Zotero 格式: "storage:ABCDEFGH/filename.pdf"
                 或  "attachments/filename.pdf"
        """
        if not path_raw:
            return "unknown.pdf"

        # 去掉 "storage:" 前缀
        clean = path_raw
        if clean.startswith("storage:"):
            clean = clean[len("storage:"):]

        # 去掉 Zotero key 目录前缀
        # 存储路径可能是 "ABCDEFGH/real_filename.pdf" 或直接用 key 作为目录
        parts = clean.split("/")
        if len(parts) >= 2:
            # 最后一段是文件名
            return parts[-1]

        return clean or "unknown.pdf"

    def _resolve_attachment_source(self, attachment_path: str, attachment_key: str) -> Optional[Path]:
        """
        解析 PDF 附件的实际文件系统路径。

        尝试多种可能的路径模式：
        1. storage/<attachment_key>/<filename>
        2. storage/<attachment_key>/<attachment_key>.pdf
        3. storage/<attachment_key>.<ext>
        """
        storage = self.storage_dir

        # 模式 1: 从 path 提取的文件名
        filename = self._extract_filename(attachment_path)
        candidate = storage / attachment_key / filename
        if candidate.exists():
            return candidate

        # 模式 2: key.pdf
        candidate = storage / attachment_key / f"{attachment_key}.pdf"
        if candidate.exists():
            return candidate

        # 模式 3: 在 storage/<key>/ 下搜索 .pdf 文件
        key_dir = storage / attachment_key
        if key_dir.is_dir():
            pdfs = list(key_dir.glob("*.pdf"))
            if pdfs:
                return pdfs[0]

        # 模式 4: 部分 key（旧版 Zotero）
        for child in storage.iterdir():
            if child.is_dir() and child.name.startswith(attachment_key[:6]):
                pdfs = list(child.glob("*.pdf"))
                if pdfs:
                    return pdfs[0]

        return None

    # ── 条目元数据 ──────────────────────────────────────────────────────

    def get_item_metadata(self, item_id: int) -> dict:
        """
        获取文献条目的元数据（title, date, year, authors）。

        返回:
            {"title": str, "date": str, "year": str, "authors": [str]}
        """
        # 获取 title 和 date
        rows = self.conn.execute(
            """
            SELECT f.fieldName, v.value
            FROM itemData d
            JOIN fields f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE d.itemID = ?
              AND f.fieldName IN ('title', 'date', 'year', 'publicationTitle')
            """,
            (item_id,),
        ).fetchall()

        meta: dict = {"title": "", "date": "", "year": "", "authors": []}
        for r in rows:
            field = r["fieldName"]
            value = r["value"] or ""
            if field == "title":
                meta["title"] = value
            elif field == "date":
                meta["date"] = value
                meta["year"] = self._extract_year(value)
            elif field == "year":
                meta["year"] = value

        # 回退：如果 date 里有年份但 year 没设置
        if not meta["year"] and meta["date"]:
            meta["year"] = self._extract_year(meta["date"])

        # 获取作者
        author_rows = self.conn.execute(
            """
            SELECT c.firstName, c.lastName
            FROM itemCreators ic
            JOIN creators c ON c.creatorID = ic.creatorID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        ).fetchall()

        meta["authors"] = [
            f"{r['lastName'] or ''}, {r['firstName'] or ''}".strip(", ")
            for r in author_rows
        ]

        return meta

    @staticmethod
    def _extract_year(date_str: str) -> str:
        """从日期字符串中提取年份。"""
        m = re.search(r"(\d{4})", date_str)
        return m.group(1) if m else ""

    # ── Collection 关联 ─────────────────────────────────────────────────

    def get_item_collections(self, item_id: int) -> list[int]:
        """获取条目所属的所有 collection ID。"""
        rows = self.conn.execute(
            "SELECT collectionID FROM collectionItems WHERE itemID = ?",
            (item_id,),
        ).fetchall()
        return [r["collectionID"] for r in rows]

    # ── 文件命名 ────────────────────────────────────────────────────────

    def generate_filename(self, item: dict) -> str:
        """
        根据命名模板生成输出文件名。

        模板变量: {title}, {author}, {authors}, {year}
        """
        authors = item.get("authors", [])
        first_author = authors[0].split(",")[0].strip() if authors else "Unknown"
        all_authors = "_".join(
            a.split(",")[0].strip() for a in authors[:3]
        )  # 最多3个
        title = (item.get("title") or "untitled").strip()
        # 标题截断：避免文件名过长
        title = re.sub(r"\s+", " ", title)[:120]
        year = item.get("year", "")

        # 保留 PDF 扩展名
        original_name = item.get("filename", "untitled.pdf")
        _, ext = os.path.splitext(original_name)
        ext = ext if ext.lower() == ".pdf" else ".pdf"

        template = _NAMING_FORMATS.get(
            self.naming, _NAMING_FORMATS["author_year_title"]
        )

        result = template.format(
            title=title,
            author=first_author,
            authors=all_authors if all_authors else first_author,
            year=year,
        )

        return sanitize_path_segment(result + ext)

    # ── 导出逻辑 ────────────────────────────────────────────────────────

    def export_all(
        self,
        target_collection: Optional[str] = None,
        dry_run: bool = False,
    ) -> int:
        """
        执行批量导出。

        返回实际导出的 PDF 数量。
        """
        self.connect()
        try:
            collection_tree = self.build_collection_tree()

            items = self.get_all_pdf_items()

            if not items:
                print("未找到任何带 PDF 附件的文献。", file=sys.stderr)
                return 0

            print(f"找到 {len(items)} 个带 PDF 附件的条目。")

            if self.verbose:
                print(f"Zotero 数据目录: {self.zotero_dir}")
                print(f"输出目录:       {self.output_dir}")
                print(f"命名格式:       {self.naming}")
                print(f"重复处理:       {self.duplicates}")
                print(f"Collection 数:  {len(collection_tree)}")
                print("-" * 60)

            exported = 0
            skipped_missing = 0
            skipped_dup = 0
            no_collection = 0

            for item in items:
                # 查找源文件
                source = self._resolve_attachment_source(
                    item["attachment_path"], item["attachment_key"]
                )

                if source is None:
                    skipped_missing += 1
                    if self.verbose:
                        print(
                            f"  ⚠ 跳过（找不到 PDF）: {item.get('title', '?')[:60]} "
                            f"[key={item['attachment_key']}]"
                        )
                    continue

                # 生成文件名
                filename = self.generate_filename(item)

                # 找到所属 Collections
                collection_ids = self.get_item_collections(item["item_id"])

                if not collection_ids:
                    no_collection += 1
                    # 根目录输出
                    output_paths = [self.output_dir / filename]
                else:
                    if self.first_collection_only:
                        collection_ids = collection_ids[:1]

                    output_paths = []
                    for cid in collection_ids:
                        coll_path = collection_tree.get(cid, "")
                        if coll_path:
                            output_paths.append(
                                self.output_dir / coll_path / filename
                            )
                        else:
                            output_paths.append(self.output_dir / filename)

                # 去重输出路径（同一个 item 可能在同一 collection 下重复出现）
                seen_paths = set()
                unique_output_paths: list[Path] = []
                for p in output_paths:
                    key = str(p.resolve())
                    if key not in seen_paths:
                        seen_paths.add(key)
                        unique_output_paths.append(p)
                output_paths = unique_output_paths

                # 执行导出
                for dest in output_paths:
                    if dry_run:
                        print(f"  [DRY-RUN] {source.name} → {dest}")
                        exported += 1
                    else:
                        status = self._copy_pdf(source, dest)
                        if status == "copied":
                            exported += 1
                            if self.verbose:
                                short_dest = str(dest.relative_to(self.output_dir))
                                print(f"  ✓ {short_dest}")
                        elif status == "skipped":
                            skipped_dup += 1
                        elif status == "overwritten":
                            exported += 1
                            if self.verbose:
                                short_dest = str(dest.relative_to(self.output_dir))
                                print(f"  ↻ {short_dest}")

            # 输出统计
            print("-" * 60)
            print(f"导出完成:")
            print(f"  成功导出: {exported} 个 PDF")
            if skipped_missing:
                print(f"  缺失源文件: {skipped_missing}")
            if skipped_dup:
                print(f"  跳过重复: {skipped_dup}")
            if no_collection:
                print(f"  无 Collection: {no_collection} 个条目（已输出到根目录）")

            return exported

        finally:
            self.close()

    def _copy_pdf(self, source: Path, dest: Path) -> str:
        """
        复制 PDF 到目标路径，处理重复策略。

        返回: "copied" | "skipped" | "overwritten"
        """
        existed = dest.exists()

        if existed:
            if self.duplicates == "skip":
                return "skipped"
            elif self.duplicates == "rename":
                dest = self._rename_duplicate(dest)
                existed = False  # 重命名后目标路径不再存在

        ensure_dir(dest.parent)
        shutil.copy2(source, dest)
        return "overwritten" if existed else "copied"

    @staticmethod
    def _rename_duplicate(path: Path) -> Path:
        """为重复文件生成新名称 (添加 _1, _2 后缀)。"""
        stem = path.stem
        ext = path.suffix
        parent = path.parent
        counter = 1
        while True:
            new_path = parent / f"{stem}_{counter}{ext}"
            if not new_path.exists():
                return new_path
            counter += 1


# ─── CLI ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Zotero 批量导出 PDF 附件，按 Collection 层级组织目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 自动探测 Zotero 目录，导出到 ./zotero_export/
  python scripts/zotero_export.py

  # 指定 Zotero 目录和输出目录
  python scripts/zotero_export.py --zotero-dir ~/Zotero/xxxx.default --output-dir ./papers

  # 预览模式（不实际复制）
  python scripts/zotero_export.py --dry-run

  # 按标题命名，每个条目只导出到第一个 Collection
  python scripts/zotero_export.py --naming title --first-collection-only
        """,
    )

    parser.add_argument(
        "--zotero-dir",
        type=str,
        default=None,
        help="Zotero 数据目录路径（包含 zotero.sqlite 和 storage/ 的目录）。省略则自动探测。",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./zotero_export",
        help="导出目标目录 (默认: ./zotero_export)",
    )
    parser.add_argument(
        "--naming",
        type=str,
        default="author_year_title",
        choices=list(_NAMING_FORMATS.keys()),
        help="文件命名格式 (默认: author_year_title)",
    )
    parser.add_argument(
        "--duplicates",
        type=str,
        default="rename",
        choices=["rename", "skip", "overwrite"],
        help="重复文件名处理策略 (默认: rename)",
    )
    parser.add_argument(
        "--first-collection-only",
        action="store_true",
        help="当条目属于多个 Collection 时，只导出到第一个",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：列出将导出的文件，但不实际复制",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="显示详细进度"
    )

    return parser


def main() -> None:
    # 修复 Windows 终端中文编码问题
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    try:
        exporter = ZoteroExporter(
            zotero_dir=args.zotero_dir,
            output_dir=args.output_dir,
            naming=args.naming,
            duplicates=args.duplicates,
            first_collection_only=args.first_collection_only,
            verbose=args.verbose,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        count = exporter.export_all(dry_run=args.dry_run)
        if count == 0 and not args.dry_run:
            print("没有文件被导出。", file=sys.stderr)
    except sqlite3.Error as e:
        print(f"数据库错误: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"文件系统错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
