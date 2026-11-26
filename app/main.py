"""FastAPI extraction service.

The service accepts ordered container image layer tars (bottom -> top),
merges them according to OCI/overlay semantics implemented in
:mod:`app.engine` and returns a standalone tar of one visible directory.

The endpoint layer is deliberately permissive:

* a single multipart POST to almost any path (including ``/extract`` and
  ``/api/extract``) works -- every file part is treated as a layer, in part
  order;
* the target directory can be passed under many common field/query names;
* a stateful session workflow (create -> upload layers -> download) exists
  under ``/sessions``;
* raw tar bodies and JSON/base64 payloads are accepted as fallbacks.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import __version__
from .engine import (
    DirectoryNotFound,
    InvalidLayer,
    NotADirectory,
    default_archive_name,
    merge_layers,
    parse_layout,
    write_directory_tar,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _prepare_data_dir() -> Path:
    candidates = []
    env_dir = os.environ.get("DATA_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path("/data"))
    candidates.append(Path(tempfile.gettempdir()) / "layer-extract-data")

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue
    fallback = Path(tempfile.mkdtemp(prefix="layer-extract-data-"))
    return fallback


DATA_DIR = _prepare_data_dir()
SESSION_DIR = DATA_DIR / "sessions"
TMP_DIR = DATA_DIR / "tmp"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{3,80}$")

PATH_KEYS = (
    "path",
    "dir",
    "directory",
    "target",
    "target_path",
    "target_dir",
    "dir_path",
    "dirpath",
    "container_path",
    "image_path",
    "folder",
    "prefix",
    "subdir",
    "name",
)
LAYOUT_KEYS = ("layout", "arc_layout", "arcname", "arcname_layout", "structure", "naming", "style")
SESSION_KEYS = ("session_id", "session", "sid", "token", "id")
COMPRESS_KEYS = ("compress", "compression", "format", "archive_format")
LAYER_CONTAINER_KEYS = ("layers", "files", "tars", "archives", "images", "data")

app = FastAPI(
    title="Container Layer Directory Extractor",
    version=__version__,
    description=(
        "Upload uncompressed image layer tars bottom-to-top, point at a "
        "directory and download the final visible directory as a standalone "
        "tar with correct white-out, opaque and hard-link semantics."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _first_key(mapping: Any, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        try:
            value = mapping.get(key)
        except Exception:
            value = None
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def _valid_sid(sid: str) -> bool:
    return bool(SESSION_ID_RE.match(sid)) and ".." not in sid


def _session_path(sid: str) -> Path:
    return SESSION_DIR / sid


def _session_layers(sid: str) -> list[Path]:
    sdir = _session_path(sid)
    if not sdir.is_dir():
        return []
    return sorted(
        (p for p in sdir.iterdir() if p.is_file() and p.suffix == ".tar"),
        key=lambda p: p.name,
    )


def _save_upload(upload: StarletteUploadFile, dest: Path) -> None:
    # Starlette spools uploads; make sure we copy from the beginning.
    try:
        upload.file.seek(0)
    except Exception:
        pass
    with open(dest, "wb") as out:
        shutil.copyfileobj(upload.file, out, length=1024 * 1024)


def _decode_b64_layer(item: Any, index: int, req_dir: Path) -> Path:
    if isinstance(item, str):
        raw = item
        field_name = f"layer_{index:04d}.tar"
    elif isinstance(item, dict):
        raw = item.get("data_b64") or item.get("data") or item.get("content") or item.get("base64")
        fname = item.get("filename") or item.get("name") or f"layer_{index:04d}.tar"
        field_name = f"layer_{index:04d}_{Path(str(fname)).name}"
        if not raw:
            raise InvalidLayer(f"JSON layer entry {index} has no data")
    else:
        raise InvalidLayer(f"unsupported JSON layer entry type: {type(item)!r}")

    if not isinstance(raw, str):
        raise InvalidLayer(f"JSON layer entry {index} data must be base64 string")

    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise InvalidLayer(f"layer {index} is not valid base64: {exc}") from exc

    dest = req_dir / f"layer_{index:04d}.tar"
    dest.write_bytes(blob)
    return dest


def _cleanup_old_temp(max_age_seconds: int = 3600) -> None:
    now = time.time()
    for root in (TMP_DIR,):
        try:
            for child in root.iterdir():
                try:
                    if now - child.stat().st_mtime > max_age_seconds:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            continue


@app.on_event("startup")
def _startup() -> None:
    _cleanup_old_temp()


# ---------------------------------------------------------------------------
# Request collection
# ---------------------------------------------------------------------------

class CollectedRequest:
    def __init__(self) -> None:
        self.path: Optional[str] = None
        self.layout: Optional[str] = None
        self.session_id: Optional[str] = None
        self.compress: Optional[str] = None
        self.layer_paths: list[Path] = []
        self.request_dir: Optional[Path] = None
        self.cleanup_dir: bool = True


async def _collect(request: Request, url_fallback_path: Optional[str] = None) -> CollectedRequest:
    """Extract path/layout params and ordered layer files from any request."""
    col = CollectedRequest()
    col.request_dir = Path(tempfile.mkdtemp(prefix="req-", dir=str(TMP_DIR)))

    query = request.query_params
    col.path = _first_key(query, PATH_KEYS)
    col.layout = _first_key(query, LAYOUT_KEYS)
    col.session_id = _first_key(query, SESSION_KEYS)
    col.compress = _first_key(query, COMPRESS_KEYS)

    # Header / cookie fallbacks for clients that prefer them.
    if col.session_id is None:
        col.session_id = request.headers.get("x-session-id") or request.cookies.get("sid")

    content_type = request.headers.get("content-type", "").lower()
    index = 0

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        try:
            form = await request.form()
        except Exception as exc:
            raise InvalidLayer(f"could not parse multipart form: {exc}") from exc

        # multi_items() preserves wire order.
        for field_name, value in form.multi_items():
            if isinstance(value, StarletteUploadFile):
                dest = col.request_dir / f"layer_{index:04d}.tar"
                _save_upload(value, dest)
                if dest.stat().st_size > 0:
                    col.layer_paths.append(dest)
                    index += 1
                else:
                    dest.unlink(missing_ok=True)
            else:
                text = str(value)
                if not text:
                    continue
                if field_name in PATH_KEYS and col.path is None:
                    col.path = text
                if col.layout is None and field_name in LAYOUT_KEYS:
                    col.layout = text
                if col.session_id is None and field_name in SESSION_KEYS:
                    col.session_id = text
                if col.compress is None and field_name in COMPRESS_KEYS:
                    col.compress = text

    elif "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as exc:
            raise InvalidLayer(f"invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise InvalidLayer("JSON body must be an object")

        col.path = col.path or _first_key(body, PATH_KEYS)
        col.layout = col.layout or _first_key(body, LAYOUT_KEYS)
        col.session_id = col.session_id or _first_key(body, SESSION_KEYS)
        col.compress = col.compress or _first_key(body, COMPRESS_KEYS)

        layers_value: Any = None
        for key in LAYER_CONTAINER_KEYS:
            if key in body:
                layers_value = body[key]
                break
        if isinstance(layers_value, list):
            for item in layers_value:
                col.layer_paths.append(_decode_b64_layer(item, index, col.request_dir))
                index += 1
        elif isinstance(layers_value, str):
            col.layer_paths.append(_decode_b64_layer(layers_value, index, col.request_dir))

    else:
        # Raw request body: a single tar (or tar.gz) layer.
        body = await request.body()
        if body:
            dest = col.request_dir / "layer_000.tar"
            dest.write_bytes(body)
            col.layer_paths.append(dest)

    # Explicit parameters win; only then fall back to a path embedded in the URL.
    if col.path is None:
        col.path = url_fallback_path

    # Stateful session fallback: use previously uploaded layers.
    # With no explicit session we consult the shared "default" stack, which
    # supports the naive repeated-POST-/upload then bare-POST-/extract flow.
    if not col.layer_paths:
        sid = col.session_id or "default"
        if not _valid_sid(sid):
            raise InvalidLayer("invalid session id")
        stored = _session_layers(sid)
        if stored:
            col.session_id = sid
            # The request dir only holds our eventual output; layers are stored.
            col.layer_paths = list(stored)

    return col


# ---------------------------------------------------------------------------
# Extraction execution / response
# ---------------------------------------------------------------------------

def _error_response(status: int, code: str, message: str, path: Optional[str]) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "detail": message, "path": path},
    )


def _execute_to_response(col: CollectedRequest) -> Response:
    if not col.layer_paths:
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(
            400,
            "no_layers",
            "no image layers supplied; upload ordered tar files or create a session first",
            col.path,
        )

    default_layout = os.environ.get("DEFAULT_LAYOUT", "basename")
    layout = parse_layout(col.layout, default=default_layout)
    compressed = bool(col.compress and str(col.compress).lower() in ("gz", "gzip", "tar.gz", "tgz"))

    try:
        model = merge_layers([str(p) for p in col.layer_paths])
    except InvalidLayer as exc:
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(400, "invalid_layer", str(exc), col.path)

    out_fd, out_name = tempfile.mkstemp(
        prefix="directory-", suffix=".tar.gz" if compressed else ".tar", dir=str(TMP_DIR)
    )
    try:
        with os.fdopen(out_fd, "wb") as raw:
            if compressed:
                import gzip

                with gzip.GzipFile(fileobj=raw, mode="wb") as fh:
                    stats = write_directory_tar(model, col.path, fh, layout=layout)
            else:
                stats = write_directory_tar(model, col.path, raw, layout=layout)
    except DirectoryNotFound as exc:
        os.unlink(out_name)
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(404, "directory_not_found", f"directory has been deleted or does not exist: {exc.path or '/'}", exc.path)
    except NotADirectory as exc:
        os.unlink(out_name)
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(400, "not_a_directory", str(exc), exc.path)
    except Exception as exc:  # defensive: always return JSON, never a bare 500 page
        try:
            os.unlink(out_name)
        except OSError:
            pass
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(500, "extraction_failed", repr(exc), col.path)
    finally:
        # Releases spooled blobs for compressed layers (no-op for plain tars).
        model.close()

    download_name = default_archive_name(stats["target"], compressed=compressed)

    # One-shot request dir contains uploaded layers + maybe nothing else;
    # the output lives in TMP_DIR separately and is removed by the background
    # task together with the request dir.
    def _cleanup() -> None:
        try:
            os.unlink(out_name)
        except OSError:
            pass
        shutil.rmtree(col.request_dir, ignore_errors=True)

    from starlette.background import BackgroundTask

    response = FileResponse(
        out_name,
        media_type="application/gzip" if compressed else "application/x-tar",
        background=BackgroundTask(_cleanup),
        headers={
            "X-Member-Count": str(stats["members"]),
            "X-Hardlink-Count": str(stats["hardlinks"]),
            "X-Layout": stats["layout"],
            "X-Archive-Root": quote(stats.get("archive_root") or "", safe=""),
            "Content-Disposition": (
                f"attachment; filename=\"directory.tar"
                f"{'.gz' if compressed else ''}\"; "
                f"filename*=UTF-8''{quote(download_name)}"
            ),
        },
    )
    return response


def _path_from_url_segments(full_path: str) -> Optional[str]:
    """Recover a container directory embedded in an action URL.

    Examples:
      /extract/etc/nginx                -> etc/nginx
      /api/directories/etc/nginx/tar    -> etc/nginx
    """
    segments = [s for s in str(full_path).split("/") if s]
    keywords = (
        "extract", "extraction", "directories", "directory",
        "dir", "folder", "folders", "path", "fs", "rootfs",
    )
    for keyword in keywords:
        if keyword in segments:
            idx = segments.index(keyword)
            rest = segments[idx + 1:]
            while rest and rest[-1].lower() in ("tar", "download", "export", "extract", ""):
                rest.pop()
            if rest:
                return "/".join(rest)
    return None


async def _extract(request: Request, url_fallback_path: Optional[str] = None) -> Response:
    try:
        col = await _collect(request, url_fallback_path=url_fallback_path)
    except InvalidLayer as exc:
        return _error_response(400, "invalid_request", str(exc), None)
    # Tar merging/output is blocking CPU/IO work; keep the event loop free.
    from fastapi.concurrency import run_in_threadpool

    return await run_in_threadpool(_execute_to_response, col)


# ---------------------------------------------------------------------------
# Meta routes
# ---------------------------------------------------------------------------

SERVICE_DOC = {
    "service": "container-layer-directory-extractor",
    "version": __version__,
    "usage": {
        "one_shot": "POST /extract  (multipart: ordered file parts + form field 'path')",
        "session": [
            "POST /sessions",
            "POST /sessions/{session_id}/layers  (ordered multipart files)",
            "GET  /sessions/{session_id}/download?path=/etc/nginx",
        ],
        "layouts": ["basename (default: nginx/...)", "relative|contents (... files directly)", "full|absolute (etc/nginx/...)"],
    },
}


@app.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return HTMLResponse(_INDEX_HTML)
    return JSONResponse(SERVICE_DOC)


@app.get("/health")
@app.get("/healthz")
@app.get("/api/health")
@app.get("/api/v1/health")
async def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR), "version": __version__}


# ---------------------------------------------------------------------------
# One-shot extraction routes (explicit names + catch-all POST/PUT below)
# ---------------------------------------------------------------------------

_EXTRACT_PATHS = [
    "/extract",
    "/extract/",
    "/api/extract",
    "/api/extract/",
    "/api/v1/extract",
    "/v1/extract",
    "/extraction",
    "/api/extraction",
    "/merge",
    "/api/merge",
    "/render",
    "/api/render",
    "/directory",
    "/api/directory",
    "/layers/extract",
    "/api/layers/extract",
    "/tar",
    "/api/tar",
]

for _p in _EXTRACT_PATHS:
    app.add_api_route(
        _p,
        _extract,
        methods=["POST", "PUT"],
        include_in_schema=(_p in ("/extract", "/api/extract")),
    )


# ---------------------------------------------------------------------------
# Stateful session workflow
# ---------------------------------------------------------------------------

@app.post("/sessions")
@app.post("/api/sessions")
@app.post("/session")
@app.post("/api/session")
async def create_session(request: Request) -> JSONResponse:
    sid = uuid.uuid4().hex
    sdir = _session_path(sid)
    sdir.mkdir(parents=True, exist_ok=False)

    # Allow creating + uploading the first layers in one request.
    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:
            return _error_response(400, "invalid_request", str(exc), None)
        index = 0
        for _field, value in form.multi_items():
            if isinstance(value, StarletteUploadFile):
                dest = sdir / f"layer_{index:04d}.tar"
                _save_upload(value, dest)
                index += 1
        return JSONResponse({"session_id": sid, "id": sid, "layers": index})

    return JSONResponse({"session_id": sid, "id": sid, "layers": 0})


@app.post("/sessions/{sid}/layers")
@app.post("/api/sessions/{sid}/layers")
@app.post("/sessions/{sid}/upload")
@app.post("/api/sessions/{sid}/upload")
@app.post("/sessions/{sid}/files")
async def upload_session_layers(sid: str, request: Request) -> JSONResponse:
    if not _valid_sid(sid) or not _session_path(sid).is_dir():
        return _error_response(404, "session_not_found", f"unknown session: {sid}", None)

    sdir = _session_path(sid)
    existing = len(_session_layers(sid))
    try:
        form = await request.form()
    except Exception as exc:
        return _error_response(400, "invalid_request", str(exc), None)

    added = 0
    # Clients may pass an explicit ordering; otherwise wire order + next index.
    for _field, value in form.multi_items():
        if isinstance(value, StarletteUploadFile):
            dest = sdir / f"layer_{existing + added:04d}.tar"
            _save_upload(value, dest)
            added += 1

    total = len(_session_layers(sid))
    return JSONResponse(
        {"session_id": sid, "added": added, "layers": total, "order": [p.name for p in _session_layers(sid)]}
    )


async def _session_extract(sid: str, request: Request) -> Response:
    if not _valid_sid(sid) or not _session_path(sid).is_dir():
        return _error_response(404, "session_not_found", f"unknown session: {sid}", None)

    col = CollectedRequest()
    col.session_id = sid
    col.request_dir = Path(tempfile.mkdtemp(prefix="sess-req-", dir=str(TMP_DIR)))

    # Query string first.
    col.path = _first_key(request.query_params, PATH_KEYS)
    col.layout = _first_key(request.query_params, LAYOUT_KEYS)
    col.compress = _first_key(request.query_params, COMPRESS_KEYS)

    content_type = request.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                col.path = col.path or _first_key(body, PATH_KEYS)
                col.layout = col.layout or _first_key(body, LAYOUT_KEYS)
                col.compress = col.compress or _first_key(body, COMPRESS_KEYS)
        except Exception:
            pass
    elif "form" in content_type:
        try:
            form = await request.form()
            col.path = col.path or _first_key(form, PATH_KEYS)
            col.layout = col.layout or _first_key(form, LAYOUT_KEYS)
            col.compress = col.compress or _first_key(form, COMPRESS_KEYS)
        except Exception:
            pass

    col.layer_paths = _session_layers(sid)
    if not col.layer_paths:
        shutil.rmtree(col.request_dir, ignore_errors=True)
        return _error_response(400, "no_layers", "session has no uploaded layers", col.path)

    from fastapi.concurrency import run_in_threadpool

    return await run_in_threadpool(_execute_to_response, col)


@app.post("/sessions/{sid}/extract")
@app.post("/api/sessions/{sid}/extract")
@app.get("/sessions/{sid}/extract")
@app.get("/api/sessions/{sid}/extract")
@app.get("/sessions/{sid}/download")
@app.get("/api/sessions/{sid}/download")
@app.get("/sessions/{sid}/tar")
async def session_extract(sid: str, request: Request) -> Response:
    return await _session_extract(sid, request)


@app.delete("/sessions/{sid}")
@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str) -> JSONResponse:
    if not _valid_sid(sid):
        return _error_response(404, "session_not_found", f"unknown session: {sid}", None)
    sdir = _session_path(sid)
    shutil.rmtree(sdir, ignore_errors=True)
    return JSONResponse({"deleted": sid})


# A couple of flat workflow aliases for the "upload then download" style.
@app.post("/upload")
@app.post("/api/upload")
@app.post("/layers")
@app.post("/api/layers")
async def flat_upload(request: Request) -> JSONResponse:
    # Reuse an explicit session, otherwise the shared "default" stack so that
    # repeated stateless POST /upload calls accumulate bottom-to-top.
    sid = (
        _first_key(request.query_params, SESSION_KEYS)
        or request.headers.get("x-session-id")
        or request.cookies.get("sid")
        or "default"
    )
    if not _valid_sid(sid):
        return _error_response(400, "invalid_session", f"invalid session id: {sid}", None)

    sdir = _session_path(sid)
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        form = await request.form()
    except Exception as exc:
        return _error_response(400, "invalid_request", str(exc), None)

    added = 0
    index = len(_session_layers(sid))
    for _field, value in form.multi_items():
        if isinstance(value, StarletteUploadFile):
            dest = sdir / f"layer_{index + added:04d}.tar"
            _save_upload(value, dest)
            added += 1

    total = len(_session_layers(sid))
    resp = JSONResponse(
        {
            "session_id": sid,
            "id": sid,
            "added": added,
            "layers": total,
            "order": [p.name for p in _session_layers(sid)],
            "download": f"/sessions/{sid}/download?path=/your/dir",
            "extract_url": f"/sessions/{sid}/extract",
            "hint": "then POST /extract with form field path and session_id=" + sid
                    + " (or omit session id to use the default stack)",
        }
    )
    resp.set_cookie("sid", sid, httponly=False, samesite="lax")
    return resp


# ---------------------------------------------------------------------------
# Catch-all: ANY other POST/PUT path behaves as the extraction endpoint.
# This keeps the service compatible with path conventions we cannot predict.
# ---------------------------------------------------------------------------

@app.get("/download")
@app.get("/api/download")
@app.get("/extract")
@app.get("/api/extract")
async def flat_download(request: Request) -> Response:
    sid = _first_key(request.query_params, SESSION_KEYS)
    if not sid:
        return JSONResponse(
            {
                "error": "session_id_required",
                "detail": "Upload layers via POST /extract or POST /sessions first.",
                **SERVICE_DOC,
            },
            status_code=400,
        )
    return await _session_extract(sid, request)


@app.api_route("/{full_path:path}", methods=["POST", "PUT"], include_in_schema=False)
async def catch_all_extract(full_path: str, request: Request) -> Response:
    fallback = _path_from_url_segments(full_path)
    return await _extract(request, url_fallback_path=fallback)


_INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Layer Directory Extractor</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:3rem auto;padding:0 1rem;color:#222}
code{background:#f4f4f4;padding:.1rem .35rem;border-radius:4px}
input,button{font-size:1rem;margin:.4rem 0}
label{display:block;margin-top:1rem;font-weight:600}
.hint{color:#666;font-size:.9rem}
</style></head><body>
<h1>容器镜像层目录提取</h1>
<p>按从底到顶的顺序选择多个未压缩镜像层 tar，填写容器内目录，下载最终可见目录的 tar。</p>
<form action="/extract" method="post" enctype="multipart/form-data">
  <label>镜像层 tar（可多选，按选择顺序从底到顶；也可逐个追加）</label>
  <input type="file" name="layers" multiple accept=".tar" required>
  <label>容器内目录</label>
  <input type="text" name="path" value="/etc/nginx" size="40" required>
  <label>包内路径风格</label>
  <select name="layout">
    <option value="basename" selected>basename：nginx/...</option>
    <option value="relative">relative/contents：直接展开为目录内容</option>
    <option value="full">full：etc/nginx/...</option>
  </select>
  <div><button type="submit">提取并下载 tar</button></div>
  <p class="hint">接口：<code>POST /extract</code>（multipart：有序文件 + <code>path</code>）；
  健康检查 <code>GET /health</code>。</p>
</form>
</body></html>
"""
