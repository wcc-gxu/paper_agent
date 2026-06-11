#!/usr/bin/env python3
"""
断点续传 PDF 到远端服务器
逐文件 scp，已存在的（大小相同）自动跳过，中断后重跑即可继续。
"""

import os
import subprocess
import sys
import time

LOCAL_DIR = "../my_papers"
REMOTE_HOST = "ubuntu@124.156.201.202"
REMOTE_DIR = "/home/ubuntu/data_row/my_papers"


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """执行命令，隐藏输出。"""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def ssh(cmd: str) -> tuple[int, str, str]:
    """在远端执行命令，返回 (returncode, stdout, stderr)。"""
    result = run(["ssh", "-o", "ConnectTimeout=10", REMOTE_HOST, cmd])
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def remote_exists(remote_path: str) -> bool:
    """检查远端文件是否存在。"""
    # 对路径中的特殊字符加引号
    escaped = remote_path.replace("'", "'\\''")
    rc, _, _ = ssh(f"test -f '{escaped}'")
    return rc == 0


def remote_size(remote_path: str) -> int:
    """获取远端文件大小（不存在返回 -1）。"""
    escaped = remote_path.replace("'", "'\\''")
    rc, stdout, _ = ssh(f"stat -c%s '{escaped}' 2>/dev/null")
    if rc == 0 and stdout:
        return int(stdout)
    return -1


def remote_mkdir(remote_path: str) -> None:
    """在远端创建目录。"""
    escaped = remote_path.replace("'", "'\\''")
    ssh(f"mkdir -p '{escaped}'")


def upload_file(local_path: str, remote_path: str) -> bool:
    """上传单个文件到远端，成功返回 True。"""
    remote = f"{REMOTE_HOST}:{remote_path}"
    result = run(["scp", "-o", "ConnectTimeout=30", local_path, remote])
    return result.returncode == 0


def main() -> int:
    local_base = os.path.abspath(LOCAL_DIR)
    if not os.path.isdir(local_base):
        print(f"❌ 本地目录不存在: {local_base}")
        return 1

    # 收集所有文件
    all_files: list[tuple[str, str]] = []  # (local_path, rel_path)
    for root, _, files in os.walk(local_base):
        for f in files:
            local_path = os.path.join(root, f)
            rel_path = os.path.relpath(local_path, local_base).replace("\\", "/")
            all_files.append((local_path, rel_path))

    total = len(all_files)
    print(f"共 {total} 个文件待检查")

    skipped = 0
    uploaded = 0
    failed = 0

    for i, (local_path, rel_path) in enumerate(all_files, 1):
        local_size = os.path.getsize(local_path)
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        remote_dir = remote_path.rsplit("/", 1)[0]

        # 进度提示
        print(f"[{i}/{total}] {rel_path}  ", end="", flush=True)

        # 检查远端是否已有且大小一致
        rsize = remote_size(remote_path)
        if rsize == local_size:
            print("⏭ 跳过（已存在）")
            skipped += 1
            continue
        elif rsize > 0 and rsize != local_size:
            print(f"⚠ 大小不一致 (本地:{local_size} 远端:{rsize})，重新上传")

        # 确保远端目录存在
        remote_mkdir(remote_dir)

        # 上传
        ok = upload_file(local_path, remote_path)
        if ok:
            print(f"✓ 完成 ({local_size/1024:.0f} KB)")
            uploaded += 1
        else:
            print("❌ 失败")
            failed += 1
            # 失败不中断，继续传后面的
            time.sleep(1)

        # 每 10 个文件暂停一下，避免 SSH 连接被限流
        if uploaded % 10 == 0 and uploaded > 0:
            time.sleep(0.5)

    print()
    print("=" * 50)
    print(f"总计: {total} | 已跳过: {skipped} | 新上传: {uploaded} | 失败: {failed}")
    if failed:
        print("重跑脚本即可续传失败的文件。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
