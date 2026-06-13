#!/usr/bin/env python3
"""上传全部 PDF + Markdown + 数据库到远端服务器。断点续传，跳过已存在的文件。"""

import io
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows UTF-8 fix
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

REMOTE_HOST = "ubuntu@124.156.201.202"
REMOTE_BASE = "/home/ubuntu/data_row"

# 本地 → 远端 目录映射
UPLOAD_MAP = {
    str(Path.home() / "papers"): f"{REMOTE_BASE}/papers",
    str(Path.home() / ".paper_search"): f"{REMOTE_BASE}/.paper_search",
}


def ssh(cmd: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", REMOTE_HOST, cmd],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def remote_mkdir(path: str):
    ssh(f"mkdir -p '{path}'")


def remote_size(path: str) -> int:
    rc, stdout, _ = ssh(f"stat -c%s '{path}' 2>/dev/null")
    if rc == 0 and stdout:
        return int(stdout)
    return -1


def upload_file(local: str, remote: str) -> bool:
    result = subprocess.run(
        ["scp", "-o", "ConnectTimeout=30", local, f"{REMOTE_HOST}:{remote}"],
        capture_output=True, text=True, timeout=300,
    )
    return result.returncode == 0


def main():
    all_files = []
    for local_base, remote_base in UPLOAD_MAP.items():
        if not os.path.isdir(local_base):
            print(f"SKIP (不存在): {local_base}")
            continue
        for root, _, files in os.walk(local_base):
            # 跳过 __pycache__ 和 chroma 的内部 bin 文件
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".pyc"):
                    continue
                local = os.path.join(root, f)
                rel = os.path.relpath(local, local_base).replace("\\", "/")
                remote = f"{remote_base}/{rel}"
                all_files.append((local, remote, os.path.getsize(local)))

    total = len(all_files)
    total_bytes = sum(s for _, _, s in all_files)
    print(f"共 {total} 个文件, {total_bytes/1024**3:.1f} GB\n")

    skipped = uploaded = failed = 0
    uploaded_bytes = 0

    for i, (local, remote, size) in enumerate(all_files, 1):
        rel = os.path.relpath(local, str(Path.home())).replace("\\", "/")
        pct = f"[{i}/{total}]"
        mb = f"{size/1024**2:.1f}MB" if size > 1024**2 else f"{size/1024:.0f}KB"

        # 远端已存在且大小一致 → 跳过
        rsize = remote_size(remote)
        if rsize == size:
            skipped += 1
            if i % 100 == 0:
                print(f"{pct} {skipped} skip, {uploaded} ok, {failed} fail | {uploaded_bytes/1024**3:.1f}GB uploaded")
            continue

        # 确保远端目录存在
        remote_dir = remote.rsplit("/", 1)[0]
        remote_mkdir(remote_dir)

        # 上传
        ok = upload_file(local, remote)
        if ok:
            uploaded += 1
            uploaded_bytes += size
        else:
            failed += 1
            time.sleep(1)

        # 每10个文件或每100MB汇报一次
        if uploaded % 10 == 0 or i % 100 == 0:
            print(f"{pct} {rel[:60]} {mb} {'OK' if ok else 'FAIL'} | {uploaded} ok, {uploaded_bytes/1024**3:.1f}GB")

        if uploaded % 20 == 0 and uploaded > 0:
            time.sleep(0.5)

    print(f"\n{'='*50}")
    print(f"总计: {total} | 跳过: {skipped} | 上传: {uploaded} | 失败: {failed}")
    print(f"上传量: {uploaded_bytes/1024**3:.1f} GB")
    if failed:
        print("重跑脚本可续传失败文件")

if __name__ == "__main__":
    main()
