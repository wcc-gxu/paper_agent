#!/usr/bin/env python3
"""Batch index papers from /home/ubuntu/data_row/papers/ into pgvector.

Usage:
    python scripts/batch_index_papers.py
    python scripts/batch_index_papers.py --dry-run
    python scripts/batch_index_papers.py --limit 10
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from paper_search.agent.pgvector_store import PgVectorStore
from paper_search.agent.pgdb import PostgresAgentDB
from paper_search.agent.chunker import SectionChunker

MARKDOWN_DIR = Path("/home/ubuntu/data_row/papers/markdown")
PDF_DIR = Path("/home/ubuntu/data_row/papers")
USER_ID = "user-default"


def extract_metadata(md_text: str, filename: str) -> dict:
    """Extract title, abstract, year, authors from markdown content."""
    lines = md_text.split("\n")
    title = ""
    abstract = ""
    authors_str = ""
    year = None

    # Try to extract from filename: "Author_2025_Title.md"
    name_no_ext = filename.rsplit(".", 1)[0]
    parts = name_no_ext.split("_")
    title_from_name = ""
    for i, p in enumerate(parts):
        if re.match(r"^\d{4}$", p):
            year = int(p)
            title_from_name = " ".join(parts[i + 1:])
            break

    # Extract title: first # heading
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("# ") and not line_stripped.startswith("## "):
            title = line_stripped[2:].strip()
            break
        elif line_stripped.startswith("Title:") or line_stripped.startswith("title:"):
            title = line_stripped.split(":", 1)[1].strip()

    if not title and title_from_name:
        title = title_from_name

    # Extract abstract
    in_abstract = False
    abstract_lines = []
    for line in lines:
        line_stripped = line.strip().lower()
        if "abstract" in line_stripped and len(line_stripped) < 30:
            in_abstract = True
            continue
        if in_abstract:
            if line.strip().startswith("#") or line.strip().startswith("##"):
                break
            if line.strip():
                abstract_lines.append(line.strip())
        if len(abstract_lines) > 10:
            break

    abstract = " ".join(abstract_lines)[:2000]

    # Fallback: first paragraph after title as abstract
    if not abstract:
        in_content = False
        para_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                in_content = True
                continue
            if in_content and stripped:
                if stripped.startswith("#"):
                    break
                para_lines.append(stripped)
            if len(" ".join(para_lines)) > 500:
                break
        abstract = " ".join(para_lines)[:2000]

    return {
        "title": title or name_no_ext,
        "abstract": abstract or title or name_no_ext,
        "year": year,
        "authors_str": authors_str,
    }


def generate_paper_id(filename: str) -> str:
    """Generate a unique paper ID from filename."""
    name = filename.rsplit(".", 1)[0]
    h = hashlib.md5(name.encode()).hexdigest()[:12]
    return f"local:{h}"


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    db = PostgresAgentDB()
    store = PgVectorStore(user_id=USER_ID)
    chunker = SectionChunker()

    # Find MD files
    md_files = []
    for f in sorted(MARKDOWN_DIR.iterdir()):
        if f.is_file():
            name = f.name
            # Skip hash-named files (no .md extension)
            if not name.endswith(".md"):
                if len(name) == 8 and all(c in "0123456789abcdef" for c in name):
                    continue  # skip hash dirs
                continue
            md_files.append(f)

    print(f"Found {len(md_files)} markdown files")

    if limit:
        md_files = md_files[:limit]
        print(f"Limited to {limit} files")

    # Track existing papers
    existing = set()
    try:
        rows = db._fetchall("SELECT id FROM papers WHERE user_id = %s", (USER_ID,))
        existing = {r["id"] for r in rows}
        print(f"Existing papers in DB: {len(existing)}")
    except Exception as e:
        print(f"Warning: could not fetch existing papers: {e}")

    # Track existing chunks
    existing_chunks = set()
    try:
        rows = db._fetchall(
            "SELECT DISTINCT paper_id FROM paper_chunks WHERE user_id = %s",
            (USER_ID,),
        )
        existing_chunks = {r["paper_id"] for r in rows}
        print(f"Papers already indexed: {len(existing_chunks)}")
    except Exception as e:
        print(f"Warning: could not fetch indexed papers: {e}")

    indexed_count = 0
    skipped_count = 0
    error_count = 0
    total_chunks = 0
    start_time = time.time()

    for i, md_path in enumerate(md_files):
        filename = md_path.name
        paper_id = generate_paper_id(filename)

        # Skip if already indexed
        if paper_id in existing_chunks:
            skipped_count += 1
            continue

        try:
            md_text = md_path.read_text(encoding="utf-8", errors="replace")
            if len(md_text) < 100:
                skipped_count += 1
                continue
        except Exception as e:
            print(f"  ERROR reading {filename}: {e}")
            error_count += 1
            continue

        meta = extract_metadata(md_text, filename)

        if dry_run:
            print(f"  [{i+1}/{len(md_files)}] {filename}: title={meta['title'][:60]}")
            continue

        try:
            # 1. Register paper in DB if not exists
            if paper_id not in existing:
                db._execute(
                    """INSERT INTO papers (id, user_id, title, abstract, year, source, file_path, md_path)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (
                        paper_id, USER_ID,
                        meta["title"][:500], meta["abstract"][:5000],
                        meta["year"] or 2024, "local",
                        "", str(md_path),
                    ),
                )
                existing.add(paper_id)

            # 2. Index abstract
            store.add_paper_abstract(
                paper_id=paper_id,
                title=meta["title"][:200],
                abstract=meta["abstract"][:2000],
                metadata={
                    "year": meta["year"] or 2024,
                    "source": "local",
                    "filename": filename,
                },
            )

            # 3. Chunk and index fulltext
            chunks = chunker.chunk(md_text, paper_id)
            if chunks:
                n = store.add_fulltext_chunks(chunks)
                total_chunks += n

            indexed_count += 1
            if (indexed_count) % 10 == 0:
                elapsed = time.time() - start_time
                rate = indexed_count / max(elapsed, 0.1)
                print(f"  [{i+1}/{len(md_files)}] {filename[:50]}... OK "
                      f"(rate: {rate:.1f}/s, chunks: {total_chunks})")

        except Exception as e:
            print(f"  [{i+1}/{len(md_files)}] {filename[:50]}... ERROR: {type(e).__name__}: {e}")
            error_count += 1
            # Pause briefly on error
            time.sleep(0.5)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"DONE in {elapsed:.1f}s")
    print(f"  Indexed: {indexed_count}")
    print(f"  Skipped (already indexed): {skipped_count}")
    print(f"  Errors: {error_count}")
    print(f"  Total chunks: {total_chunks}")
    print(f"  Rate: {indexed_count/max(elapsed,0.1):.1f} papers/s")

    if dry_run:
        print("\nDry run — no changes made.")


if __name__ == "__main__":
    main()
