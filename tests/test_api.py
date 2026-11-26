"""End-to-end HTTP tests for the FastAPI extraction service."""

from __future__ import annotations

import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def tar_bytes(members: list[tuple]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for spec in members:
            kind = spec[0]
            if kind == "dir":
                ti = tarfile.TarInfo(spec[1])
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tar.addfile(ti)
            elif kind == "file":
                _, name, data = spec
                if isinstance(data, str):
                    data = data.encode()
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
            elif kind == "link":
                ti = tarfile.TarInfo(spec[1])
                ti.type = tarfile.LNKTYPE
                ti.linkname = spec[2]
                tar.addfile(ti)
    return buf.getvalue()


def open_response_tar(resp) -> tarfile.TarFile:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] in (
        "application/x-tar",
        "application/x-tar; charset=utf-8",
    ) or "x-tar" in resp.headers["content-type"]
    return tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:")


# ---------------------------------------------------------------------------

def test_health(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_descriptor(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "service" in r.json()


def test_one_shot_extract_basic(client: TestClient):
    l1 = tar_bytes([
        ("dir", "etc/app"),
        ("file", "etc/app/a", b"v1"),
    ])
    l2 = tar_bytes([
        ("file", "etc/app/a", b"v2"),
        ("file", "etc/app/b", b"b"),
    ])
    r = client.post(
        "/extract",
        data={"path": "/etc/app"},
        files=[
            ("layers", ("bottom.tar", l1, "application/x-tar")),
            ("layers", ("top.tar", l2, "application/x-tar")),
        ],
    )
    tf = open_response_tar(r)
    assert tf.extractfile("app/a").read() == b"v2"
    assert tf.extractfile("app/b").read() == b"b"


def test_api_alias_and_field_names(client: TestClient):
    layer = tar_bytes([("file", "cfg/x", b"x")])
    # common alternative endpoint + field name "files" + "directory"
    r = client.post(
        "/api/extract",
        data={"directory": "/cfg"},
        files=[("files", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    assert tf.extractfile("cfg/x").read() == b"x"


def test_catch_all_arbitrary_path(client: TestClient):
    layer = tar_bytes([("file", "cfg/x", b"x")])
    r = client.post(
        "/some/reference/endpoint",
        data={"path": "cfg"},
        files=[("archive", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    assert tf.extractfile("cfg/x").read() == b"x"


def test_path_as_query_param(client: TestClient):
    layer = tar_bytes([("file", "cfg/x", b"x")])
    r = client.post(
        "/extract?path=/cfg",
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    assert tf.extractfile("cfg/x").read() == b"x"


def test_deleted_directory_returns_404(client: TestClient):
    l1 = tar_bytes([("dir", "cfg"), ("file", "cfg/a", b"a")])
    l2 = tar_bytes([("file", ".wh.cfg", b"")])
    r = client.post(
        "/extract",
        data={"path": "/cfg"},
        files=[
            ("layers", ("l1.tar", l1, "application/x-tar")),
            ("layers", ("l2.tar", l2, "application/x-tar")),
        ],
    )
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "directory_not_found"


def test_whiteout_and_opaque_http(client: TestClient):
    base = tar_bytes([
        ("file", "app/old", b"old"),
        ("file", "app/keep", b"k"),
    ])
    upper = tar_bytes([
        ("file", "app/.wh.old", b""),
    ])
    r = client.post(
        "/extract",
        data={"path": "/app", "layout": "relative"},
        files=[
            ("layers", ("b.tar", base, "application/x-tar")),
            ("layers", ("u.tar", upper, "application/x-tar")),
        ],
    )
    tf = open_response_tar(r)
    ns = set(tf.getnames())
    assert "keep" in ns
    assert "old" not in ns


def test_hardlink_snapshot_and_outside_target_http(client: TestClient):
    l1 = tar_bytes([
        ("file", "outside/f", b"original"),
        ("dir", "inside"),
    ])
    l2 = tar_bytes([("link", "inside/h", "outside/f")])
    l3 = tar_bytes([("file", "outside/f", b"overwritten")])

    r = client.post(
        "/extract",
        data={"path": "/inside"},
        files=[
            ("layers", ("1.tar", l1, "application/x-tar")),
            ("layers", ("2.tar", l2, "application/x-tar")),
            ("layers", ("3.tar", l3, "application/x-tar")),
        ],
    )
    tf = open_response_tar(r)
    member = tf.getmember("inside/h")
    assert member.isfile()
    assert tf.extractfile(member).read() == b"original"


def test_in_package_hardlink_member_type_http(client: TestClient):
    layer = tar_bytes([
        ("file", "d/a", b"zzz"),
        ("link", "d/b", "d/a"),
    ])
    r = client.post(
        "/extract",
        data={"path": "/d"},
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    b = tf.getmember("d/b")
    assert b.islnk()
    assert b.linkname == "d/a"


def test_no_layers_returns_400(client: TestClient):
    r = client.post("/extract", data={"path": "/cfg"})
    assert r.status_code in (400, 422)


def test_session_workflow(client: TestClient):
    s = client.post("/sessions")
    assert s.status_code == 200
    sid = s.json()["session_id"]

    l1 = tar_bytes([("file", "cfg/a", b"a")])
    l2 = tar_bytes([("file", "cfg/b", b"b")])

    u = client.post(
        f"/sessions/{sid}/layers",
        files=[
            ("layers", ("l1.tar", l1, "application/x-tar")),
            ("layers", ("l2.tar", l2, "application/x-tar")),
        ],
    )
    assert u.status_code == 200
    assert u.json()["layers"] == 2

    d = client.get(f"/sessions/{sid}/download", params={"path": "/cfg"})
    tf = open_response_tar(d)
    assert tf.extractfile("cfg/a").read() == b"a"
    assert tf.extractfile("cfg/b").read() == b"b"

    # one-shot /extract can also reference the session
    d2 = client.post("/extract", data={"session_id": sid, "path": "/cfg"})
    tf2 = open_response_tar(d2)
    assert tf2.extractfile("cfg/a").read() == b"a"


def test_flat_upload_alias(client: TestClient):
    layer = tar_bytes([("file", "cfg/x", b"x")])
    r = client.post(
        "/upload",
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    d = client.get(f"/sessions/{sid}/tar?path=/cfg")
    tf = open_response_tar(d)
    assert tf.extractfile("cfg/x").read() == b"x"


def test_raw_tar_body_with_query_path(client: TestClient):
    layer = tar_bytes([("file", "cfg/x", b"raw")])
    r = client.post("/extract?path=/cfg", content=layer, headers={"content-type": "application/x-tar"})
    tf = open_response_tar(r)
    assert tf.extractfile("cfg/x").read() == b"raw"


def test_json_base64_payload(client: TestClient):
    import base64

    layer = tar_bytes([("file", "cfg/x", b"json")])
    r = client.post(
        "/extract",
        json={"path": "/cfg", "layers": [base64.b64encode(layer).decode()]},
    )
    tf = open_response_tar(r)
    assert tf.extractfile("cfg/x").read() == b"json"


def test_layout_full(client: TestClient):
    layer = tar_bytes([("file", "etc/app/x", b"x")])
    r = client.post(
        "/extract",
        data={"path": "/etc/app", "layout": "absolute"},
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    assert "etc/app/x" in set(tf.getnames())


def test_path_embedded_in_url(client: TestClient):
    layer = tar_bytes([("file", "etc/app/x", b"x")])
    r = client.post(
        "/extract/etc/app",
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    tf = open_response_tar(r)
    assert "app/x" in set(tf.getnames())


def test_gzip_output(client: TestClient):
    import gzip

    layer = tar_bytes([("file", "cfg/x", b"gz")])
    r = client.post(
        "/extract",
        params={"path": "/cfg", "compress": "gzip"},
        files=[("layers", ("l.tar", layer, "application/x-tar"))],
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    tf = tarfile.open(fileobj=io.BytesIO(gzip.decompress(r.content)))
    assert tf.extractfile("cfg/x").read() == b"gz"


def test_global_default_upload_then_bare_extract(client: TestClient):
    # Naive workflow: repeated stateless /upload calls, then /extract with
    # only the directory and no session id. Layers accumulate in "default".
    l1 = tar_bytes([("file", "globalcfg/a", b"one")])
    l2 = tar_bytes([
        ("file", "globalcfg/a", b"two"),
        ("file", "globalcfg/b", b"bee"),
    ])
    r1 = client.post("/upload", files=[("layer", ("l1.tar", l1, "application/x-tar"))])
    assert r1.status_code == 200
    r2 = client.post("/layers", files=[("layer", ("l2.tar", l2, "application/x-tar"))])
    assert r2.status_code == 200
    assert r2.json()["layers"] >= 2

    r = client.post("/extract", data={"path": "/globalcfg"})
    tf = open_response_tar(r)
    assert tf.extractfile("globalcfg/a").read() == b"two"
    assert tf.extractfile("globalcfg/b").read() == b"bee"
