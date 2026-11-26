"""Layer merging and directory export engine.

The engine only depends on the Python standard library so that it can be used
from the CLI and tests without FastAPI installed.

Model
-----
Every regular tar member creates a fresh immutable :class:`Node` (its bytes
stay inside the original layer tar and are read lazily through a
:class:`Blob`).  A hard link binds its path to the *same* Node that is
currently bound to its link target.  Replacing a path with a new regular file
therefore creates a new Node while older hard links keep pointing at the Node
they were created against -- this is exactly the "hard link keeps the content
from creation time" rule.

White-out files (``.wh.<name>``) and ``.wh..wh..opq`` markers only remove
entries originating from *lower* layers.  Entries contributed by the layer
currently being processed always survive, regardless of tar member order.
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple

WHITEOUT_PREFIX = ".wh."
OPAQUE_BASENAME = ".wh..wh..opq"

# PAX / SCHILY xattr variants used by some tools to mark opaque directories.
OPAQUE_XATTR_KEYS = (
    "SCHILY.xattr.user.overlay.opaque",
    "user.overlay.opaque",
)

DEFAULT_DIR_MODE = 0o755
DEFAULT_FILE_MODE = 0o644


class ExtractionError(Exception):
    """Base class for domain errors."""


class InvalidLayer(ExtractionError):
    """An uploaded layer is not a readable tar archive."""


class DirectoryNotFound(ExtractionError):
    """Requested directory does not exist in the merged rootfs."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"directory not found in merged image: {path or '/'}")


class NotADirectory(ExtractionError):
    """Requested path exists but is not a directory."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"path is not a directory: {path or '/'}")


# ---------------------------------------------------------------------------
# Path helpers (virtual, root relative POSIX paths)
# ---------------------------------------------------------------------------

def normalize_path(name: Optional[str]) -> str:
    """Normalize a tar member / request path to a root-relative POSIX path.

    Leading ``/`` and ``./`` are dropped, ``.`` segments removed and ``..``
    segments pop the previous component (never escaping the virtual root).
    Backslashes are *not* separators: in tar names they are valid filename
    characters on Linux.
    """
    if not name:
        return ""
    text = str(name)
    parts: List[str] = []
    for seg in text.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            # leading '..' is simply dropped -- everything is virtual
            continue
        parts.append(seg)
    return "/".join(parts)


def parent_path(path: str) -> str:
    if "/" in path:
        return path.rsplit("/", 1)[0]
    return ""


def base_name(path: str) -> str:
    if "/" in path:
        return path.rsplit("/", 1)[1]
    return path


def join_path(parent: str, name: str) -> str:
    if not parent:
        return name
    if not name:
        return parent
    return f"{parent}/{name}"


def is_within(path: str, directory: str) -> bool:
    if not directory:
        return True
    return path == directory or path.startswith(directory + "/")


# ---------------------------------------------------------------------------
# Blob access
# ---------------------------------------------------------------------------

class _LimitedReader:
    """Binary reader exposing at most ``limit`` bytes of another file object."""

    def __init__(self, raw: BinaryIO, limit: int, close_raw: bool = True):
        self._raw = raw
        self._remaining = max(0, int(limit))
        self._close_raw = close_raw

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self._remaining
        size = min(size, self._remaining)
        chunk = self._raw.read(size)
        if not chunk:
            self._remaining = 0
            return b""
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        if self._close_raw:
            try:
                self._raw.close()
            except Exception:
                pass

    def readable(self) -> bool:  # pragma: no cover - tarfile capability check
        return True


@dataclass
class Blob:
    """Reference to immutable regular-file bytes.

    Exactly one source is used:

    * ``data`` for in-memory bytes,
    * ``layer_path`` + ``offset`` for zero-copy lazy reads out of an
      uncompressed layer tar,
    * ``spool_path`` for bytes spooled out of a compressed layer.
    """

    size: int
    data: Optional[bytes] = None
    layer_path: Optional[str] = None
    offset: Optional[int] = None
    spool_path: Optional[str] = None

    def open_reader(self) -> BinaryIO:
        if self.data is not None:
            return io.BytesIO(self.data)
        if self.spool_path is not None:
            raw = open(self.spool_path, "rb")
            return _LimitedReader(raw, self.size)
        raw = open(self.layer_path or "", "rb")
        if self.offset:
            raw.seek(self.offset)
        return _LimitedReader(raw, self.size)

    def read_bytes(self) -> bytes:
        reader = self.open_reader()
        try:
            chunks: List[bytes] = []
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            reader.close()


# ---------------------------------------------------------------------------
# Merged model
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """An immutable file/inode version.

    Several :class:`Entry` paths may reference the same Node -- that is the
    hard-link cluster.
    """

    id: int
    blob: Blob
    size: int = 0
    mode: int = DEFAULT_FILE_MODE
    mtime: int = 0
    uid: int = 0
    gid: int = 0
    uname: str = ""
    gname: str = ""


@dataclass
class Entry:
    path: str
    kind: str  # dir | file | symlink | char | block | fifo | hardlink-pending
    layer: int
    node: Optional[Node] = None
    via_hardlink: bool = False
    target: Optional[str] = None
    mode: int = DEFAULT_FILE_MODE
    mtime: int = 0
    uid: int = 0
    gid: int = 0
    uname: str = ""
    gname: str = ""
    devmajor: int = 0
    devminor: int = 0


@dataclass
class MergedLayers:
    entries: Dict[str, Entry] = field(default_factory=dict)
    nodes: List[Node] = field(default_factory=list)
    layer_count: int = 0
    temp_dirs: List[str] = field(default_factory=list)

    def new_node(self, blob: Blob, **meta) -> Node:
        node = Node(id=len(self.nodes), blob=blob, **meta)
        self.nodes.append(node)
        return node

    def close(self) -> None:
        import shutil

        for d in self.temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.temp_dirs.clear()


# ---------------------------------------------------------------------------
# Layer processing
# ---------------------------------------------------------------------------

_COMPRESSED_MAGIC = (
    b"\x1f\x8b",          # gzip
    b"BZh",               # bzip2
    b"\xfd7zXZ\x00",      # xz
    b"\x28\xb5\x2f\xfd",  # zstd
)


def _is_plain_tar(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            magic = fh.read(6)
    except OSError:
        return False
    return not any(magic.startswith(sig) for sig in _COMPRESSED_MAGIC)


def _spool_member_bytes(
    model: MergedLayers,
    ti: tarfile.TarInfo,
    tf: tarfile.TarFile,
) -> Blob:
    """Read a member out of a (possibly compressed) stream into a temp blob."""
    import tempfile

    if not model.temp_dirs:
        model.temp_dirs.append(tempfile.mkdtemp(prefix="layer-blobs-"))
    fd, spool = tempfile.mkstemp(prefix="blob-", dir=model.temp_dirs[0])
    try:
        src = tf.extractfile(ti)
        with os.fdopen(fd, "wb") as dst:
            if src is not None:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
    except Exception:
        try:
            os.unlink(spool)
        except OSError:
            pass
        raise
    return Blob(size=ti.size, spool_path=spool)


def _blob_for_regular(
    model: MergedLayers,
    ti: tarfile.TarInfo,
    layer_path: Path,
    plain: bool,
    tf: tarfile.TarFile,
) -> Blob:
    if ti.size == 0:
        return Blob(size=0, data=b"")
    if plain and ti.offset_data:
        return Blob(
            size=ti.size,
            layer_path=str(layer_path),
            offset=ti.offset_data,
        )
    return _spool_member_bytes(model, ti, tf)


def _meta_kwargs(ti: tarfile.TarInfo, default_mode: int) -> dict:
    return dict(
        mode=(ti.mode & 0o7777) if ti.mode else default_mode,
        mtime=int(ti.mtime or 0),
        uid=int(ti.uid or 0),
        gid=int(ti.gid or 0),
        uname=ti.uname or "",
        gname=ti.gname or "",
    )


def _synthetic_dir(layer: int) -> Entry:
    return Entry(path="", kind="dir", layer=layer, mode=DEFAULT_DIR_MODE)


def _ensure_parent_dirs(model: MergedLayers, path: str, layer: int) -> None:
    parts = path.split("/")
    for i in range(1, len(parts)):
        d = "/".join(parts[:i])
        existing = model.entries.get(d)
        if existing is None:
            e = _synthetic_dir(layer)
            e.path = d
            model.entries[d] = e
        # If an existing non-directory sits here, real image layers would be
        # invalid; leave it untouched rather than destroying data.


def _apply_whiteout(model: MergedLayers, target: str, layer: int) -> None:
    """Remove lower-layer entries at ``target`` (recursively).

    Entries contributed by the current layer (``entry.layer == layer``) are
    protected: markers mask only *lower* content.
    """
    if not target:
        return
    for key in list(model.entries.keys()):
        if key == target or key.startswith(target + "/"):
            if model.entries[key].layer < layer:
                del model.entries[key]


def _apply_opaque(model: MergedLayers, directory: str, layer: int) -> None:
    """Hide every lower-layer entry below ``directory``."""
    if directory:
        prefix = directory + "/"
        for key in list(model.entries.keys()):
            if key.startswith(prefix) and model.entries[key].layer < layer:
                del model.entries[key]
        existing = model.entries.get(directory)
        if existing is None or existing.kind != "dir":
            e = _synthetic_dir(layer)
            e.path = directory
            model.entries[directory] = e
        else:
            # This layer owns/touches the directory.
            existing.layer = layer
    else:
        # Opaque root: wipe all lower entries.
        for key in list(model.entries.keys()):
            if model.entries[key].layer < layer:
                del model.entries[key]


def _has_opaque_xattr(ti: tarfile.TarInfo) -> bool:
    headers = getattr(ti, "pax_headers", None) or {}
    for key in OPAQUE_XATTR_KEYS:
        value = headers.get(key)
        if value is not None and str(value).strip().lower() in ("y", "1", "true"):
            return True
    return False


def _resolve_hardlink(
    model: MergedLayers,
    raw_target: Optional[str],
    member_path: str,
) -> Tuple[Optional[Node], Optional[str]]:
    candidates: List[str] = []
    root_relative = normalize_path(raw_target)
    if root_relative:
        candidates.append(root_relative)
    # Some tools may store the target relative to the member's directory.
    relative = normalize_path(join_path(parent_path(member_path), raw_target or ""))
    if relative and relative not in candidates:
        candidates.append(relative)

    for candidate in candidates:
        entry = model.entries.get(candidate)
        if entry is not None and entry.kind == "file" and entry.node is not None:
            return entry.node, candidate
    return None, (candidates[0] if candidates else None)


def _process_layer(model: MergedLayers, index: int, layer_path: Path) -> None:
    if not layer_path.exists():
        raise InvalidLayer(f"layer file missing: {layer_path}")

    plain = _is_plain_tar(layer_path)
    try:
        tf = tarfile.open(str(layer_path), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise InvalidLayer(f"cannot open layer tar {layer_path.name}: {exc}") from exc

    with tf:
        try:
            iterator = iter(tf)
            for ti in iterator:
                path = normalize_path(ti.name)
                if not path:
                    continue

                base = base_name(path)

                # ---- OCI / AUFS white-out markers -----------------------
                if base == OPAQUE_BASENAME:
                    _ensure_parent_dirs(model, path, index)
                    _apply_opaque(model, parent_path(path), index)
                    continue

                if base.startswith(WHITEOUT_PREFIX):
                    deleted_name = base[len(WHITEOUT_PREFIX):]
                    if deleted_name == OPAQUE_BASENAME:
                        _ensure_parent_dirs(model, path, index)
                        _apply_opaque(model, parent_path(path), index)
                        continue
                    target = normalize_path(
                        join_path(parent_path(path), deleted_name)
                    )
                    # The marker's parent directory exists in this layer.
                    _ensure_parent_dirs(model, path, index)
                    _apply_whiteout(model, target, index)
                    continue

                _ensure_parent_dirs(model, path, index)

                # ---- Directories ---------------------------------------
                if ti.isdir():
                    meta = _meta_kwargs(ti, DEFAULT_DIR_MODE)
                    model.entries[path] = Entry(
                        path=path,
                        kind="dir",
                        layer=index,
                        **meta,
                    )
                    if _has_opaque_xattr(ti):
                        _apply_opaque(model, path, index)
                    continue

                # ---- Regular files -------------------------------------
                if ti.isreg():
                    blob = _blob_for_regular(model, ti, layer_path, plain, tf)
                    meta = _meta_kwargs(ti, DEFAULT_FILE_MODE)
                    node = model.new_node(
                        blob=blob,
                        size=int(ti.size),
                        **meta,
                    )
                    model.entries[path] = Entry(
                        path=path,
                        kind="file",
                        layer=index,
                        node=node,
                        via_hardlink=False,
                        **meta,
                    )
                    continue

                # ---- Hard links ----------------------------------------
                if ti.islnk():
                    node, resolved = _resolve_hardlink(model, ti.linkname, path)
                    meta = _meta_kwargs(ti, DEFAULT_FILE_MODE)
                    if node is not None:
                        model.entries[path] = Entry(
                            path=path,
                            kind="file",
                            layer=index,
                            node=node,
                            via_hardlink=True,
                            target=resolved,
                            **meta,
                        )
                    else:
                        # Out-of-spec ordering (target not seen yet). Keep
                        # the raw link and try again during finalization.
                        model.entries[path] = Entry(
                            path=path,
                            kind="hardlink-pending",
                            layer=index,
                            via_hardlink=True,
                            target=ti.linkname,
                            **meta,
                        )
                    continue

                # ---- Symbolic links ------------------------------------
                if ti.issym():
                    meta = _meta_kwargs(ti, DEFAULT_FILE_MODE)
                    model.entries[path] = Entry(
                        path=path,
                        kind="symlink",
                        layer=index,
                        target=ti.linkname or "",
                        **meta,
                    )
                    continue

                # ---- Device nodes / fifos (preserve best effort) --------
                if ti.ischr() or ti.isblk():
                    meta = _meta_kwargs(ti, DEFAULT_FILE_MODE)
                    model.entries[path] = Entry(
                        path=path,
                        kind="char" if ti.ischr() else "block",
                        layer=index,
                        devmajor=int(ti.devmajor or 0),
                        devminor=int(ti.devminor or 0),
                        **meta,
                    )
                    continue

                if ti.isfifo():
                    meta = _meta_kwargs(ti, DEFAULT_FILE_MODE)
                    model.entries[path] = Entry(
                        path=path,
                        kind="fifo",
                        layer=index,
                        **meta,
                    )
                    continue

                # Anything else (e.g. sparse/unknown) is ignored.
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise InvalidLayer(
                f"error while reading layer {layer_path.name}: {exc}"
            ) from exc


def _finalize(model: MergedLayers) -> None:
    # Resolve hard links whose target had not appeared yet when scanned.
    for path, entry in list(model.entries.items()):
        if entry.kind != "hardlink-pending":
            continue
        node, resolved = _resolve_hardlink(model, entry.target, path)
        if node is not None:
            entry.kind = "file"
            entry.node = node
            entry.target = resolved
        # otherwise stays dangling; export emits a best-effort link member.

    # Rebuild any directory ancestors missing after white-out sweeps.
    for path in list(model.entries.keys()):
        _ensure_parent_dirs(model, path, model.entries[path].layer)


def merge_layers(layer_paths: Sequence[str | os.PathLike[str]]) -> MergedLayers:
    """Merge ordered uncompressed layer tars (bottom first, top last)."""
    model = MergedLayers(layer_count=len(layer_paths))
    for index, raw_path in enumerate(layer_paths):
        _process_layer(model, index, Path(raw_path))
    _finalize(model)
    return model


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

LAYOUT_BASENAME = "basename"
LAYOUT_RELATIVE = "relative"
LAYOUT_FULL = "full"
VALID_LAYOUTS = (LAYOUT_BASENAME, LAYOUT_RELATIVE, LAYOUT_FULL)

_LAYOUT_ALIASES = {
    "basename": LAYOUT_BASENAME,
    "base": LAYOUT_BASENAME,
    "dir": LAYOUT_BASENAME,
    "directory": LAYOUT_BASENAME,
    "top": LAYOUT_BASENAME,
    "name": LAYOUT_BASENAME,
    "docker": LAYOUT_BASENAME,
    LAYOUT_BASENAME: LAYOUT_BASENAME,
    "relative": LAYOUT_RELATIVE,
    "rel": LAYOUT_RELATIVE,
    "contents": LAYOUT_RELATIVE,
    "content": LAYOUT_RELATIVE,
    "flat": LAYOUT_RELATIVE,
    "subtree": LAYOUT_RELATIVE,
    "dot": LAYOUT_RELATIVE,
    ".": LAYOUT_RELATIVE,
    LAYOUT_RELATIVE: LAYOUT_RELATIVE,
    "full": LAYOUT_FULL,
    "absolute": LAYOUT_FULL,
    "root": LAYOUT_FULL,
    "container": LAYOUT_FULL,
    "fq": LAYOUT_FULL,
    "fqn": LAYOUT_FULL,
    "fullpath": LAYOUT_FULL,
    LAYOUT_FULL: LAYOUT_FULL,
}


def parse_layout(value: Optional[str], default: str = LAYOUT_BASENAME) -> str:
    if value is None or str(value).strip() == "":
        return default
    key = str(value).strip().lower().replace("-", "_")
    if key in _LAYOUT_ALIASES:
        return _LAYOUT_ALIASES[key]
    # Loose match that ignores '_' and '-' differences (e.g. "full-path").
    loose = key.replace("_", "")
    for alias, canonical in _LAYOUT_ALIASES.items():
        if alias.replace("_", "").replace("-", "") == loose:
            return canonical
    # Unknown value: fall back to the default rather than failing.
    return default


def default_archive_name(target: str, compressed: bool = False) -> str:
    base = base_name(target) if target else "rootfs"
    if not base:
        base = "rootfs"
    return f"{base}.tar.gz" if compressed else f"{base}.tar"


def _arcname(path: str, target: str, layout: str) -> str:
    if layout == LAYOUT_FULL or not target:
        return path

    if path == target:
        if layout == LAYOUT_BASENAME:
            return base_name(target) or "."
        return "."  # relative layout: archive root

    rel = path[len(target) + 1:] if path.startswith(target + "/") else path

    if layout == LAYOUT_RELATIVE:
        return rel

    top = base_name(target)
    return f"{top}/{rel}" if top else rel


def _build_arc_map(
    paths: Sequence[str], target: str, layout: str
) -> Dict[str, str]:
    return {p: _arcname(p, target, layout) for p in paths}


def _tarinfo_from_entry(arcname: str, entry: Entry) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name=arcname)
    ti.mtime = int(entry.mtime or 0)
    ti.uid = entry.uid
    ti.gid = entry.gid
    ti.uname = entry.uname or ""
    ti.gname = entry.gname or ""
    ti.mode = entry.mode or DEFAULT_FILE_MODE
    return ti


def _select_primaries(
    model: MergedLayers, file_paths: Sequence[str]
) -> Dict[int, str]:
    """Choose one current path per Node cluster to carry the bytes."""
    clusters: Dict[int, List[str]] = {}
    for p in file_paths:
        node = model.entries[p].node
        if node is None:
            continue
        clusters.setdefault(node.id, []).append(p)

    primary: Dict[int, str] = {}
    for node_id, paths in clusters.items():
        non_links = sorted(p for p in paths if not model.entries[p].via_hardlink)
        if non_links:
            primary[node_id] = non_links[0]
        else:
            primary[node_id] = sorted(paths)[0]
    return primary


def resolve_target(model: MergedLayers, directory: Optional[str]) -> str:
    target = normalize_path(directory)
    if target:
        entry = model.entries.get(target)
        if entry is None:
            raise DirectoryNotFound(target)
        if entry.kind != "dir":
            raise NotADirectory(target)
    return target


def write_directory_tar(
    model: MergedLayers,
    directory: Optional[str],
    output: BinaryIO,
    layout: str = LAYOUT_BASENAME,
) -> dict:
    """Write the visible contents of ``directory`` as a tar stream.

    Returns a small stats dict.
    """
    target = resolve_target(model, directory)
    layout = parse_layout(layout)

    paths = sorted(
        p
        for p, entry in model.entries.items()
        if is_within(p, target)
    )

    # Make sure the requested directory itself is represented.
    if target and target not in model.entries:
        e = _synthetic_dir(model.layer_count)
        e.path = target
        model.entries[target] = e
        paths = sorted(set(paths) | {target})

    arc = _build_arc_map(paths, target, layout)

    file_paths = [
        p for p in paths if model.entries[p].kind == "file"
        and model.entries[p].node is not None
    ]
    primary = _select_primaries(model, file_paths)
    arc_primary = {nid: arc[p] for nid, p in primary.items()}

    written = 0
    hardlinks = 0
    inlined = 0

    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # 1) directories first (stable extraction)
        for p in paths:
            entry = model.entries[p]
            if entry.kind != "dir":
                continue
            name = arc[p]
            if name in ("", None):
                continue
            ti = _tarinfo_from_entry(name, entry)
            ti.type = tarfile.DIRTYPE
            ti.mode = entry.mode or DEFAULT_DIR_MODE
            tar.addfile(ti)
            written += 1

        # 2) byte-carrying regular files, symlinks and special nodes
        deferred_hardlinks: List[Tuple[str, Entry, str, str]] = []

        for p in paths:
            entry = model.entries[p]
            name = arc[p]
            if not name or name == ".":
                # '.' root is emitted as directory above; skip same name here.
                if name == "." and entry.kind == "dir":
                    continue

            if entry.kind == "file" and entry.node is not None:
                nid = entry.node.id
                if primary.get(nid) == p:
                    node = entry.node
                    ti = _tarinfo_from_entry(name, entry)
                    ti.type = tarfile.REGTYPE
                    ti.size = node.size
                    # Prefer the Node's original metadata for the inode.
                    ti.mode = node.mode or ti.mode
                    ti.mtime = node.mtime or ti.mtime
                    ti.uid = node.uid
                    ti.gid = node.gid
                    ti.uname = node.uname
                    ti.gname = node.gname
                    tar.addfile(ti, node.blob.open_reader())
                    inlined += 1
                    written += 1
                else:
                    target_arc = arc_primary[nid]
                    deferred_hardlinks.append((name, entry, target_arc, p))
                continue

            if entry.kind == "hardlink-pending":
                # No resolvable target and no bytes available anywhere.
                raw = normalize_path(entry.target)
                link_arc = arc.get(raw, entry.target or "")
                ti = _tarinfo_from_entry(name, entry)
                ti.type = tarfile.LNKTYPE
                ti.linkname = link_arc or ""
                ti.size = 0
                deferred_hardlinks.append((name, entry, ti.linkname, p))
                continue

            if entry.kind == "symlink":
                ti = _tarinfo_from_entry(name, entry)
                ti.type = tarfile.SYMTYPE
                ti.linkname = entry.target or ""
                tar.addfile(ti)
                written += 1
                continue

            if entry.kind in ("char", "block"):
                ti = _tarinfo_from_entry(name, entry)
                ti.type = tarfile.CHRTYPE if entry.kind == "char" else tarfile.BLKTYPE
                ti.devmajor = entry.devmajor
                ti.devminor = entry.devminor
                tar.addfile(ti)
                written += 1
                continue

            if entry.kind == "fifo":
                ti = _tarinfo_from_entry(name, entry)
                ti.type = tarfile.FIFOTYPE
                tar.addfile(ti)
                written += 1
                continue

        # 3) hard link members after every byte-carrying primary
        for name, entry, target_arc, _p in sorted(
            deferred_hardlinks, key=lambda x: x[0]
        ):
            ti = _tarinfo_from_entry(name, entry)
            ti.type = tarfile.LNKTYPE
            ti.linkname = target_arc
            ti.size = 0
            tar.addfile(ti)
            hardlinks += 1
            written += 1

    return {
        "members": written,
        "hardlinks": hardlinks,
        "regular_primaries": inlined,
        "layout": layout,
        "target": target,
        "archive_root": arc.get(target, "."),
    }


def export_to_bytes(
    layer_paths: Sequence[str | os.PathLike[str]],
    directory: Optional[str],
    layout: str = LAYOUT_BASENAME,
) -> Tuple[bytes, dict, MergedLayers]:
    """Convenience helper mostly used by tests and the CLI."""
    model = merge_layers(layer_paths)
    buf = io.BytesIO()
    stats = write_directory_tar(model, directory, buf, layout=layout)
    return buf.getvalue(), stats, model
