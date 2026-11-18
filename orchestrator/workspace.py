"""Per-task workspace handling: creation, pre-run baseline, post-run change collection, cleanup.

Workspace contents are UNTRUSTED (an agent wrote them). The host therefore never executes anything from
them: no `git` is run in the workspace (git can execute repo-configured programs), symlinks are never
followed, only regular files are opened (O_NOFOLLOW|O_NONBLOCK, so FIFOs cannot hang the orchestrator), and
diffs are produced in pure Python.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
IGNORED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", ".cache"})
MAX_SCAN_FILES = 20000
MAX_HASH_BYTES = 64 * 1024 * 1024
TEXT_DIFF_MAX = 256 * 1024
DIFF_OUTPUT_MAX = 512 * 1024
BASELINE_COPY_MAX_BYTES = 64 * 1024 * 1024


class WorkspaceError(ValueError):
    pass


def validate_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise WorkspaceError(f"invalid {what} {value!r}: use 1-64 chars of [A-Za-z0-9_.-], starting alphanumeric")
    return value


def resolve_workspace(root: Path, workspace_id: str) -> Path:
    """Logical workspace id -> directory directly under `root` (created if missing). Never a free-form path."""
    validate_id(workspace_id, "workspace")
    root = Path(root).resolve()
    path = root / workspace_id
    if path.is_symlink():
        raise WorkspaceError(f"workspace {workspace_id!r} is a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.resolve().parent != root or not path.is_dir():
        raise WorkspaceError(f"workspace {workspace_id!r} escapes the workspace root")
    return path


def _open_regular(path: Path):
    """Open a regular file without following symlinks or blocking on FIFOs; None if not a regular file."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None


def _sha256(path: Path, size: int):
    if size > MAX_HASH_BYTES:
        return None
    fd = _open_regular(path)
    if fd is None:
        return None
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan(ws_dir: Path) -> tuple:
    """-> ({relpath: entry}, truncated). Entry: {"type": "file", size, sha256, mode} | {"type": "symlink", target}."""
    entries, truncated = {}, False
    stack = [Path(ws_dir)]
    while stack:
        current = stack.pop()
        try:
            it = list(os.scandir(current))
        except OSError:
            continue
        for e in it:
            rel = os.path.relpath(e.path, ws_dir)
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    entries[rel] = {"type": "symlink", "target": os.readlink(e.path)}
                except OSError:
                    pass
            elif stat.S_ISDIR(st.st_mode):
                if e.name not in IGNORED_DIRS:
                    stack.append(Path(e.path))
            elif stat.S_ISREG(st.st_mode):
                if len(entries) >= MAX_SCAN_FILES:
                    truncated = True
                    continue
                entries[rel] = {"type": "file", "size": st.st_size, "sha256": _sha256(Path(e.path), st.st_size),
                                "mode": stat.S_IMODE(st.st_mode)}
    return entries, truncated


def tree_size(ws_dir: Path, limit_bytes: int) -> int:
    """Total bytes of regular files (lstat only); stops early once above limit_bytes."""
    total = 0
    stack = [str(ws_dir)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
                        if total > limit_bytes:
                            return total
        except OSError:
            continue
    return total


def _read_text(path: Path):
    fd = _open_regular(path)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(TEXT_DIFF_MAX + 1)
    if len(data) > TEXT_DIFF_MAX or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def snapshot_baseline(ws_dir: Path, baseline_dir: Path) -> dict:
    """Record the pre-run state: manifest plus a copy of small text files (needed to diff them later)."""
    baseline_dir = Path(baseline_dir)
    (baseline_dir / "files").mkdir(parents=True, exist_ok=True)
    manifest, truncated = scan(ws_dir)
    budget = BASELINE_COPY_MAX_BYTES
    for rel, entry in manifest.items():
        if entry["type"] != "file" or entry["size"] > TEXT_DIFF_MAX or budget <= 0:
            continue
        text = _read_text(Path(ws_dir) / rel)
        if text is None:
            continue
        dest = baseline_dir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        budget -= entry["size"]
    (baseline_dir / "manifest.json").write_text(json.dumps({"entries": manifest, "truncated": truncated}))
    return {"files": len(manifest), "truncated": truncated}


def collect_changes(ws_dir: Path, baseline_dir: Path) -> dict:
    """Compare the workspace with the baseline. -> {artifacts, counts, diff, diffTruncated, scanTruncated}."""
    baseline_dir = Path(baseline_dir)
    try:
        saved = json.loads((baseline_dir / "manifest.json").read_text())
        before, before_trunc = saved["entries"], saved.get("truncated", False)
    except (OSError, ValueError):
        before, before_trunc = {}, True
    after, after_trunc = scan(ws_dir)
    artifacts, diff_parts, diff_len, diff_truncated = [], [], 0, False

    def add_diff(chunk: str):
        nonlocal diff_len, diff_truncated
        if diff_len + len(chunk) > DIFF_OUTPUT_MAX:
            diff_truncated = True
            return
        diff_parts.append(chunk)
        diff_len += len(chunk)

    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        if a is not None and b is not None and a == b:
            continue
        if a is not None and b is not None and a["type"] == "file" and b["type"] == "file" and a["sha256"] == b["sha256"] \
                and a["sha256"] is not None:
            continue  # only a mode/mtime change
        status = "added" if b is None else "deleted" if a is None else "modified"
        cur = a or b
        artifacts.append({"path": rel, "status": status, "type": cur["type"],
                          "size": cur.get("size"), "sha256": (a or {}).get("sha256")})
        old = _read_text(baseline_dir / "files" / rel) if b and b["type"] == "file" else None
        new = _read_text(Path(ws_dir) / rel) if a and a["type"] == "file" else None
        header = f"diff --git a/{rel} b/{rel}\n"
        if status == "added":
            header += "new file mode 100644\n"
        elif status == "deleted":
            header += "deleted file mode 100644\n"
        text_ok = (a is None or (a["type"] == "file" and new is not None)) and \
                  (b is None or (b["type"] == "file" and old is not None))
        if text_ok:
            lines = difflib.unified_diff(
                (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
                fromfile="/dev/null" if status == "added" else f"a/{rel}",
                tofile="/dev/null" if status == "deleted" else f"b/{rel}")
            body = "".join(l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in lines)
            add_diff(header + body)
        else:
            add_diff(header + f"Binary or non-text change (type={cur['type']}); content not included\n")
    counts = {s: sum(1 for x in artifacts if x["status"] == s) for s in ("added", "modified", "deleted")}
    return {"artifacts": artifacts, "counts": counts, "diff": "".join(diff_parts), "diffTruncated": diff_truncated,
            "scanTruncated": bool(before_trunc or after_trunc)}


def remove_tree(path: Path) -> None:
    """Delete a directory tree (never following symlinks), repairing permissions an agent may have removed."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            if stat.S_ISDIR(os.lstat(current).st_mode):
                os.chmod(current, 0o700)
                with os.scandir(current) as it:
                    stack.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)
