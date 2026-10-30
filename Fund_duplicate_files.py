"""dupfind：按 SHA-256 找重复文件。

先用文件大小分组，再比前 64KB，最后全文哈希，逐级淘汰候选。
"""

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

CHUNK = 1 << 20
PARTIAL = 64 * 1024
JOBS = min(32, (os.cpu_count() or 1) + 4)


def iter_files(root):
    """产出 (path, stat)，只含普通文件，不跟符号链接。"""
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            scan = os.scandir(cur)
        except OSError as e:
            print(f"跳过目录 {cur}: {e}", file=sys.stderr)
            continue
        with scan:
            for de in scan:
                try:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                    elif de.is_file(follow_symlinks=False):
                        yield de.path, de.stat(follow_symlinks=False)
                except OSError as e:
                    print(f"跳过 {de.path}: {e}", file=sys.stderr)


def file_hash(path, limit=None):
    """返回 SHA-256 摘要；limit 为读取上限，文件偏短或读不了时返回 None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            left = limit
            while left is None or left > 0:
                n = CHUNK if left is None else min(CHUNK, left)
                data = f.read(n)
                if not data:
                    break
                h.update(data)
                if left is not None:
                    left -= len(data)
            if left:
                return None
    except OSError as e:
        print(f"跳过 {path}: {e}", file=sys.stderr)
        return None
    return h.digest()


def hash_many(paths, limit, jobs):
    """{path: digest}。大块 sha256 会放掉 GIL，多线程能同时压 CPU 和磁盘队列。"""
    if jobs <= 1 or len(paths) < 2:
        return {p: file_hash(p, limit) for p in paths}
    with ThreadPoolExecutor(max_workers=min(jobs, len(paths))) as pool:
        return dict(zip(paths, pool.map(lambda p: file_hash(p, limit), paths)))


def find_duplicates(root, min_size=0, jobs=JOBS):
    """返回 (重复组, 扫描文件数)。每个组是 (大小, [(路径, mtime), ...])。"""
    by_size = defaultdict(list)
    total = 0
    for path, st in iter_files(root):
        if st.st_size >= min_size:
            by_size[st.st_size].append((path, st.st_mtime))
            total += 1

    groups = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue

        # Windows 上 scandir 给的 st_ino 是 0，要 os.stat 才能认出硬链接；
        # 只对大小撞车的文件付这笔开销。
        uniq, seen = [], set()
        for path, mtime in items:
            try:
                st = os.stat(path)
            except OSError as e:
                print(f"跳过 {path}: {e}", file=sys.stderr)
                continue
            if st.st_ino:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            uniq.append((path, mtime))

        if len(uniq) < 2:
            continue

        mtimes = dict(uniq)
        paths = [p for p, _ in uniq]

        digests = hash_many(paths, min(size, PARTIAL), jobs)
        same_head = defaultdict(list)
        for p in paths:
            if digests[p] is not None:
                same_head[digests[p]].append(p)

        for cand in same_head.values():
            if len(cand) < 2:
                continue
            if size <= PARTIAL:
                groups.append((size, [(p, mtimes[p]) for p in cand]))
                continue
            full = hash_many(cand, size, jobs)
            same_all = defaultdict(list)
            for p in cand:
                if full[p] is not None:
                    same_all[full[p]].append(p)
            for g in same_all.values():
                if len(g) > 1:
                    groups.append((size, [(p, mtimes[p]) for p in g]))

    return groups, total


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    ap = argparse.ArgumentParser(
        description="按 SHA-256 查找重复文件（默认递归扫描当前目录）")
    ap.add_argument("root", nargs="?", default=".", help="扫描目录，默认 '.'")
    ap.add_argument("-m", "--min-size", type=int, default=0,
                    help="忽略小于该字节数的文件")
    ap.add_argument("-j", "--jobs", type=int, default=0,
                    help=f"哈希线程数，0 用默认值 {JOBS}")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印扫描统计")
    ap.add_argument("-d", "--delete", action="store_true",
                    help="删除每组除保留项外的副本")
    ap.add_argument("-y", "--yes", action="store_true", help="删除时不问确认")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"目录不存在或不是目录: {args.root}", file=sys.stderr)
        return 2

    groups, total = find_duplicates(
        args.root, args.min_size, JOBS if args.jobs == 0 else max(1, args.jobs))

    # 组内按修改时间排序，最早的那份当保留项，删谁就固定了
    for _, items in groups:
        items.sort(key=lambda it: (it[1], it[0]))
    groups.sort(key=lambda g: (-(g[0] * (len(g[1]) - 1)), g[0]))

    if args.verbose:
        print(f"扫描 {total} 个文件，{len(groups)} 组重复", file=sys.stderr)

    if not groups:
        print("未发现重复文件")
        return 0

    wasted = redundant = 0
    for i, (size, items) in enumerate(groups, 1):
        w = size * (len(items) - 1)
        wasted += w
        redundant += len(items) - 1
        print(f"== 重复组 {i}: {len(items)} 个文件，每个 {human(size)}，"
              f"可释放 {human(w)}")
        for j, (p, _) in enumerate(items):
            print(f"   {p}{'  [保留]' if j == 0 else ''}")
        print()

    if args.delete:
        if not args.yes:
            try:
                # stdin 接 NUL 设备时 isatty() 在 Windows 上仍返回 True
                if not sys.stdin.isatty():
                    raise EOFError
                ans = input(f"删除 {redundant} 个冗余副本？[y/N] ").strip().lower()
            except (EOFError, OSError):
                print("拒绝删除：无法交互确认，请加 -y", file=sys.stderr)
                return 2
            if ans not in ("y", "yes"):
                print("已取消")
                return 0

        failed = 0
        for _, items in groups:
            for p, _ in items[1:]:
                try:
                    os.remove(p)
                    print(f"   已删除 {p}")
                except OSError as e:
                    failed += 1
                    print(f"   删除失败 {p}: {e}", file=sys.stderr)
        print()
        if failed:
            print(f"{failed} 个文件删除失败", file=sys.stderr)

    print("-" * 60)
    print(f"共 {len(groups)} 组重复，{redundant} 个冗余副本，"
          f"可释放 {human(wasted)}")
    return 1 if (args.delete and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
