# Duplicate File Finder (SHA-256)

A single-file script that recursively scans a directory, groups files with identical content, and optionally deletes redundant copies.
It compares content only, not file names, so renaming files or moving them to different directories does not affect the result.

## Usage

```powershell
python Fund_duplicate_files.py                # Scan the current directory
python Fund_duplicate_files.py D:\photos -v   # Scan a specific directory and print statistics
python Fund_duplicate_files.py -m 1024        # Ignore files smaller than 1 KB
python Fund_duplicate_files.py -j 8           # Hash with 8 threads
python Fund_duplicate_files.py -d             # Delete redundant copies (asks for confirmation first)
python Fund_duplicate_files.py -d -y          # Delete without confirmation
```

## Arguments

| Argument         | Description                                                  |
| ---------------- | ------------------------------------------------------------ |
| `root`           | Directory to scan, defaults to `.`                           |
| `-m, --min-size` | Ignore files smaller than this many bytes, defaults to 0     |
| `-j, --jobs`     | Number of hashing threads; 0 means auto (default `min(32, CPU+4)`), 1 means single-threaded |
| `-v, --verbose`  | Print scanned file count and duplicate group count to stderr |
| `-d, --delete`   | Delete all but the kept file in each group                   |
| `-y, --yes`      | Used with `-d` to skip confirmation                          |

## Output

```
Scanned 4 files, 1 duplicate group
== Duplicate group 1: 3 files, 29 B each, 58 B reclaimable
   D:\demo\a\report.txt  [keep]
   D:\demo\b\report-copy.txt
   D:\demo\c\report-copy2.txt

------------------------------------------------------------
1 duplicate group in total, 2 redundant copies, 58 B reclaimable
```

Groups are sorted by how much space can be saved, largest first. The first line of each group is marked `[keep]`; deletion only touches the lines below it.

## How comparison works

Candidates are narrowed down step by step to avoid reading the entire disk:

1. Group by file size first; files with a unique size are eliminated immediately.
2. For files of the same size, compute the SHA-256 of the first 64 KB; for small files this is already the full-file hash.
3. For files whose first 64 KB also match, compute the full-file SHA-256 to confirm.

So two files that have the same size and the same beginning but differ later are still excluded at step 3.

`-j` controls concurrency for steps 2 and 3. `hashlib` releases the GIL when updating large chunks of data, so multithreading can saturate both CPU and disk queues at the same time; gains are smaller on pure mechanical hard drives.

## Deletion safety measures

- Which file to keep is deterministic: within a group, files are sorted by modification time ascending, and the earliest one is kept. The result is predictable and will not delete a different file on each run.
- Without `-y`, the script asks once for confirmation; running under redirection or a pipe causes it to refuse outright (on Windows, `isatty()` still returns True when stdin is the NUL device, and the script handles this case explicitly), preventing accidental deletion.
- Files that fail to delete only produce an error; the script continues deleting the rest and exits with code 1 at the end.

## Exit codes

| Code | Meaning                                                      |
| ---- | ------------------------------------------------------------ |
| 0    | Normal completion (including user cancelling deletion, no duplicates found) |
| 1    | One or more files failed to delete                           |
| 2    | Directory does not exist, invalid arguments, or unable to confirm interactively |

## Cases not handled

- Symbolic links: skipped during scanning and not followed, avoiding loops and duplicate traversal.
- Hard links: multiple paths to the same inode are counted only once, because deleting them would not free any space.
- Empty files: 0-byte files trivially have identical content, so they are grouped together, with 0 bytes reclaimable. Add `-m 1` if you do not want to see them.

## Performance reference

Windows + SSD, warmed up and taking the best of 3 runs (measured on the author's machine, for reference only):

| Scenario                                                     | Single-threaded | Default multithreaded |
| ------------------------------------------------------------ | --------------- | --------------------- |
| 1260 files / 70 MiB, mixed sizes                             | 70 ms           | 60 ms                 |
| 120 files of 1 MiB each, sharing the first 64 KB (full hash required) | 111 ms          | 47 ms                 |

Pure hashing benefits from multithreading, while directory traversal benefits from `os.scandir` reusing cached stat data; combining both gives the most noticeable gain.

## Verify it yourself

```powershell
mkdir demo\a, demo\b | Out-Null
"same content" | Set-Content demo\a\x.txt
Copy-Item demo\a\x.txt demo\b\x-copy.txt
python Fund_duplicate_files.py demo
```

It should list only `a\x.txt` and `b\x-copy.txt` as one group.

## Requirements

Python 3.8+, standard library only, no third-party dependencies.
