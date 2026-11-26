"""Tests for the layer merge/export engine semantics."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from app.engine import (
    DirectoryNotFound,
    LAYOUT_BASENAME,
    LAYOUT_FULL,
    LAYOUT_RELATIVE,
    merge_layers,
    write_directory_tar,
)


# ---------------------------------------------------------------------------
# Tar building helpers
# ---------------------------------------------------------------------------

def make_layer(path: Path, members: list[tuple]) -> Path:
    """Build an uncompressed tar from ordered member specs.

    Specs:
      ("dir", "a")
      ("file", "a/f", b"bytes")
      ("link", "a/hard", "a/f")
      ("symlink", "a/ln", "f")
      ("raw", tarinfo_kwargs...) -- not needed yet
    White-outs are just empty files named ".wh.<x>" and opaque markers are
    files named ".wh..wh..opq".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for spec in members:
            kind = spec[0]
            if kind == "dir":
                name = spec[1]
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tar.addfile(ti)
            elif kind == "file":
                _, name, data = spec
                if isinstance(data, str):
                    data = data.encode()
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode = spec[3] if len(spec) > 3 else 0o644
                tar.addfile(ti, io.BytesIO(data))
            elif kind == "link":
                _, name, target = spec
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.LNKTYPE
                ti.linkname = target
                ti.mode = 0o644
                tar.addfile(ti)
            elif kind == "symlink":
                _, name, target = spec
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                tar.addfile(ti)
            else:  # pragma: no cover
                raise ValueError(kind)
    return path


def export(layers, directory, layout=LAYOUT_BASENAME):
    model = merge_layers(layers)
    buf = io.BytesIO()
    stats = write_directory_tar(model, directory, buf, layout=layout)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:"), stats, model


def names(tar: tarfile.TarFile) -> set[str]:
    return set(tar.getnames())


def read(tar: tarfile.TarFile, name: str) -> bytes:
    f = tar.extractfile(name)
    assert f is not None, f"{name} has no readable content"
    return f.read()


# ---------------------------------------------------------------------------
# Basic merging
# ---------------------------------------------------------------------------

def test_layers_merge_bottom_to_top(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "etc/app"),
        ("file", "etc/app/a.conf", b"a-v1\n"),
        ("file", "etc/app/old.conf", b"old\n"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("dir", "etc/app"),
        ("file", "etc/app/a.conf", b"a-v2-longer\n"),
        ("file", "etc/app/b.conf", b"b\n"),
    ])

    tar, stats, _ = export([l1, l2], "/etc/app")
    ns = names(tar)
    assert "app" in ns
    assert "app/a.conf" in ns
    assert "app/b.conf" in ns
    assert "app/old.conf" in ns
    assert read(tar, "app/a.conf") == b"a-v2-longer\n"
    assert read(tar, "app/old.conf") == b"old\n"
    assert read(tar, "app/b.conf") == b"b\n"


def test_leading_dot_slash_and_slash_are_normalized(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("file", "./etc/app/x", b"x"),
    ])
    tar, _, _ = export([l1], "etc/app/")
    assert "app/x" in names(tar)
    assert read(tar, "app/x") == b"x"

    tar2, _, _ = export([l1], "/etc/app", layout=LAYOUT_FULL)
    assert "etc/app/x" in names(tar2)


def test_relative_layout(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "etc/app/sub"),
        ("file", "etc/app/x", b"x"),
        ("file", "etc/app/sub/y", b"y"),
    ])
    tar, _, _ = export([l1], "/etc/app", layout=LAYOUT_RELATIVE)
    ns = names(tar)
    assert "x" in ns
    assert "sub/y" in ns
    assert read(tar, "sub/y") == b"y"


# ---------------------------------------------------------------------------
# White-outs
# ---------------------------------------------------------------------------

def test_whiteout_hides_lower_file(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "app"),
        ("file", "app/gone", b"secret"),
        ("file", "app/keep", b"keep"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("file", "app/.wh.gone", b""),
    ])
    tar, _, _ = export([l1, l2], "/app")
    ns = names(tar)
    assert "app/keep" in ns
    assert "app/gone" not in ns
    assert not any(".wh." in n for n in ns)


def test_whiteout_of_directory_removes_subtree(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "app/stale/deep"),
        ("file", "app/stale/a", b"a"),
        ("file", "app/stale/deep/b", b"b"),
        ("file", "app/keep", b"k"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("file", "app/.wh.stale", b""),
    ])
    tar, _, _ = export([l1, l2], "/app")
    ns = names(tar)
    assert "app/keep" in ns
    assert not any(n.startswith("app/stale") for n in ns)


def test_whited_out_target_directory_returns_not_found(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "config"),
        ("file", "config/a", b"a"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("file", ".wh.config", b""),
    ])
    model = merge_layers([l1, l2])
    with pytest.raises(DirectoryNotFound):
        buf = io.BytesIO()
        write_directory_tar(model, "/config", buf)


def test_whiteout_only_masks_lower_same_layer_file_survives(tmp_path: Path):
    # White-out BEFORE the replacement in the same tar.
    layer = make_layer(tmp_path / "l.tar", [
        ("dir", "app"),
        ("file", "app/.wh.cfg", b""),
        ("file", "app/cfg", b"new-content"),
    ])
    # Lower layer has old cfg that must be masked.
    base = make_layer(tmp_path / "base.tar", [
        ("dir", "app"),
        ("file", "app/cfg", b"old-content"),
    ])
    tar, _, _ = export([base, layer], "/app")
    assert read(tar, "app/cfg") == b"new-content"

    # Same but replacement appears BEFORE the white-out in tar order.
    layer2 = make_layer(tmp_path / "l2.tar", [
        ("dir", "app"),
        ("file", "app/cfg", b"new-content-2"),
        ("file", "app/.wh.cfg", b""),
    ])
    tar2, _, _ = export([base, layer2], "/app")
    assert read(tar2, "app/cfg") == b"new-content-2"


def test_opaque_marker_hides_lower_but_keeps_same_layer(tmp_path: Path):
    base = make_layer(tmp_path / "base.tar", [
        ("dir", "app/d"),
        ("file", "app/lower-only", b"low"),
        ("file", "app/d/deep", b"deep"),
        ("file", "app/keep-too", b"k"),
    ])
    upper = make_layer(tmp_path / "upper.tar", [
        ("dir", "app"),
        ("file", "app/.wh..wh..opq", b""),
        ("file", "app/new-file", b"new"),
        ("dir", "app/sub"),
        ("file", "app/sub/x", b"x"),
    ])
    tar, _, _ = export([base, upper], "/app")
    ns = names(tar)
    assert "app/new-file" in ns
    assert "app/sub/x" in ns
    assert "app/lower-only" not in ns
    assert "app/d/deep" not in ns
    assert "app/keep-too" not in ns  # opaque hides every lower child
    assert not any(".wh." in n or ".wh.." in n for n in ns)
    assert read(tar, "app/new-file") == b"new"


def test_opaque_in_single_layer_without_lower(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [
        ("file", "app/.wh..wh..opq", b""),
        ("file", "app/x", b"x"),
    ])
    tar, _, _ = export([layer], "/app")
    assert "app/x" in names(tar)
    assert read(tar, "app/x") == b"x"


# ---------------------------------------------------------------------------
# Hard links
# ---------------------------------------------------------------------------

def test_hardlink_relationship_preserved_inside_package(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [
        ("dir", "d"),
        ("file", "d/a", b"same-bytes"),
        ("link", "d/b", "d/a"),
    ])
    tar, _, _ = export([layer], "/d")
    a = tar.getmember("d/a")
    b = tar.getmember("d/b")
    assert a.isfile()
    assert b.islnk()
    assert b.linkname == "d/a"
    assert read(tar, "d/b") == b"same-bytes"


def test_hardlink_keeps_creation_time_bytes_after_overwrite(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("file", "a", b"version-1-bytes"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("link", "b", "a"),
        ("link", "c", "a"),
    ])
    l3 = make_layer(tmp_path / "l3.tar", [
        ("file", "a", b"version-2-completely-different"),
    ])

    tar, stats, _ = export([l1, l2, l3], "/")
    assert read(tar, "a") == b"version-2-completely-different"
    assert read(tar, "b") == b"version-1-bytes"
    assert read(tar, "c") == b"version-1-bytes"

    # b and c still form a hard-link cluster in the package.
    b = tar.getmember("b")
    c = tar.getmember("c")
    # one of them is the byte-carrying primary, the other links to it
    members = {m.name: m for m in tar.getmembers()}
    kinds = {n: (m.isfile(), m.islnk(), m.linkname) for n, m in members.items()}
    link_members = [n for n, (isf, isl, _) in kinds.items() if isl]
    assert link_members, f"expected at least one hardlink member: {kinds}"
    # the linked name must be one of the package members
    for ln in link_members:
        target_name = kinds[ln][2]
        assert target_name in members


def test_hardlink_target_outside_package_inlines_bytes(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("dir", "outside"),
        ("file", "outside/original", b"original-bytes"),
        ("dir", "inside"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("link", "inside/link", "outside/original"),
    ])
    tar, _, _ = export([l1, l2], "/inside")
    ns = names(tar)
    # unrelated outside file must not be exported
    assert not any(n.startswith("outside") for n in ns)
    link_member = tar.getmember("inside/link") if "inside/link" in ns else tar.getmember("link")
    # The standalone package must carry real bytes for this member.
    assert link_member.isfile(), "out-of-package link target must be inlined as regular file"
    f = tar.extractfile(link_member)
    assert f.read() == b"original-bytes"


def test_hardlink_to_outside_survives_target_overwrite(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [
        ("file", "outside/original", b"v1"),
    ])
    l2 = make_layer(tmp_path / "l2.tar", [
        ("link", "inside/link", "outside/original"),
    ])
    l3 = make_layer(tmp_path / "l3.tar", [
        ("file", "outside/original", b"v2-overwrite"),
    ])
    tar, _, _ = export([l1, l2, l3], "/inside")
    member = tar.getmember("inside/link")
    assert member.isfile()
    assert read(tar, "inside/link") == b"v1"


def test_output_tar_extracts_to_standalone_hardlinks(tmp_path: Path):
    layers = [
        make_layer(tmp_path / "l1.tar", [
            ("dir", "cfg"),
            ("file", "cfg/a", b"hello-hardlink\n"),
            ("link", "cfg/b", "cfg/a"),
            ("file", "outside.txt", b"outside-bytes"),
            ("link", "cfg/external", "outside.txt"),
        ])
    ]
    model = merge_layers(layers)
    out = tmp_path / "out.tar"
    with open(out, "wb") as fh:
        write_directory_tar(model, "/cfg", fh)

    dest = tmp_path / "extracted"
    dest.mkdir()
    with tarfile.open(out) as tf:
        # Python 3.12 filter safety; fully trusted self-generated archive.
        try:
            tf.extractall(dest, filter="fully_trusted")
        except TypeError:
            tf.extractall(dest)

    a = dest / "cfg" / "a"
    b = dest / "cfg" / "b"
    ext = dest / "cfg" / "external"
    assert a.read_bytes() == b"hello-hardlink\n"
    assert b.read_bytes() == b"hello-hardlink\n"
    assert ext.read_bytes() == b"outside-bytes"
    st_a = a.stat()
    st_b = b.stat()
    assert (st_a.st_ino, st_a.st_dev) == (st_b.st_ino, st_b.st_dev), \
        "a and b should be real hard links after extraction"


# ---------------------------------------------------------------------------
# Filtering / edge cases
# ---------------------------------------------------------------------------

def test_unrelated_files_absent(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [
        ("file", "etc/app/x", b"x"),
        ("file", "etc/other/y", b"y"),
        ("file", "var/log/z", b"z"),
    ])
    tar, _, _ = export([layer], "/etc/app")
    ns = names(tar)
    assert all(n == "app" or n.startswith("app/") for n in ns)
    assert not any("other" in n for n in ns)
    assert not any(n.startswith("var") for n in ns)


def test_missing_directory_raises_not_found(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [("file", "a", b"a")])
    model = merge_layers([layer])
    with pytest.raises(DirectoryNotFound):
        buf = io.BytesIO()
        write_directory_tar(model, "/does/not/exist", buf)


def test_empty_directory_exported(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [("dir", "empty")])
    tar, _, _ = export([layer], "/empty")
    assert "empty" in names(tar)


def test_whiteout_then_recreate_directory(tmp_path: Path):
    base = make_layer(tmp_path / "base.tar", [
        ("dir", "d/sub"),
        ("file", "d/sub/old", b"old"),
    ])
    upper = make_layer(tmp_path / "upper.tar", [
        ("file", ".wh.d", b""),
        ("dir", "d"),
        ("file", "d/new", b"new"),
    ])
    tar, _, _ = export([base, upper], "/d")
    ns = names(tar)
    assert "d/new" in ns
    assert not any(n.startswith("d/sub") for n in ns)
    assert read(tar, "d/new") == b"new"


def test_lazy_blob_reads_correct_offset_after_other_members(tmp_path: Path):
    payload1 = bytes((i * 7 + 3) % 256 for i in range(50000))
    payload2 = bytes((i * 13 + 1) % 256 for i in range(30000))
    layer = make_layer(tmp_path / "big.tar", [
        ("file", "app/f1", payload1),
        ("file", "unrelated/big", b"z" * 200000),
        ("file", "app/f2", payload2),
    ])
    tar, _, model = export([layer], "/app")
    assert read(tar, "app/f1") == payload1
    assert read(tar, "app/f2") == payload2
    # blobs are lazy references into the original tar
    node_f1 = model.entries["app/f1"].node
    assert node_f1.blob.layer_path is not None
    assert node_f1.blob.read_bytes() == payload1


def test_opaque_parent_makes_missing_child_directory_404(tmp_path: Path):
    base = make_layer(tmp_path / "base.tar", [
        ("dir", "a/b"),
        ("file", "a/b/f", b"f"),
    ])
    upper = make_layer(tmp_path / "upper.tar", [
        ("file", "a/.wh..wh..opq", b""),
    ])
    model = merge_layers([base, upper])
    # a itself still exists (opaque dir), but b is gone.
    buf = io.BytesIO()
    with pytest.raises(DirectoryNotFound):
        write_directory_tar(model, "/a/b", buf)
    # requesting a succeeds and is empty (apart from its own entry)
    buf2 = io.BytesIO()
    stats = write_directory_tar(model, "/a", buf2)
    assert stats["members"] >= 1


def test_hardlink_survives_whiteout_of_original_path(tmp_path: Path):
    l1 = make_layer(tmp_path / "l1.tar", [("file", "o", b"bytes")])
    l2 = make_layer(tmp_path / "l2.tar", [("link", "d/h", "o")])
    l3 = make_layer(tmp_path / "l3.tar", [("file", ".wh.o", b"")])
    tar, _, _ = export([l1, l2, l3], "/d")
    member = tar.getmember("d/h")
    assert member.isfile()
    assert read(tar, "d/h") == b"bytes"
    assert "o" not in names(tar)


def test_hardlink_target_with_dot_slash_prefix(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [
        ("file", "d/a", b"abc"),
        ("link", "d/b", "./d/a"),
    ])
    tar, _, _ = export([layer], "/d")
    b = tar.getmember("d/b")
    assert b.islnk()
    assert b.linkname == "d/a"
    assert read(tar, "d/b") == b"abc"


def test_long_names_and_linknames(tmp_path: Path):
    long_dir = "d/" + ("very_long_directory_name_" * 6)
    long_file = long_dir + "/" + ("a_quite_long_filename_" * 6) + ".dat"
    long_link = long_dir + "/" + ("another_long_link_name_" * 6) + ".lnk"
    layer = make_layer(tmp_path / "long.tar", [
        ("file", long_file, b"long-content"),
        ("link", long_link, long_file),
    ])
    tar, _, _ = export([layer], "/d")
    # basename layout keeps the leading "d" root, so arcs equal full paths here.
    link_member = tar.getmember(long_link)
    assert link_member.islnk()
    assert len(link_member.linkname) > 100  # PAX extended header used
    assert tar.extractfile(link_member).read() == b"long-content"
    assert tar.getmember(long_file).isfile()


def test_whiteout_nonexistent_target_is_ok(tmp_path: Path):
    layer = make_layer(tmp_path / "l.tar", [
        ("file", "d/.wh.never-existed", b""),
        ("file", "d/x", b"x"),
    ])
    tar, _, _ = export([layer], "/d")
    assert "d/x" in names(tar)


def test_same_layer_whiteout_replacement_with_reversed_order(tmp_path: Path):
    # replacement first, white-out marker second
    layer = make_layer(tmp_path / "l.tar", [
        ("dir", "d"),
        ("file", "d/f", b"new"),
        ("file", "d/.wh.f", b""),
    ])
    tar, _, _ = export([layer], "/d")
    assert read(tar, "d/f") == b"new"
