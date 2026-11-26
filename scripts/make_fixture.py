"""Generate a realistic multi-layer fixture for manual/e2e checks."""
import io
import sys
import tarfile
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def layer(name, members):
    with tarfile.open(out / name, "w") as t:
        for m in members:
            kind = m[0]
            if kind == "dir":
                ti = tarfile.TarInfo(m[1]); ti.type = tarfile.DIRTYPE; ti.mode = 0o755
                t.addfile(ti)
            elif kind == "file":
                _, n, b = m
                b = b.encode() if isinstance(b, str) else b
                ti = tarfile.TarInfo(n); ti.size = len(b); ti.mode = 0o644
                t.addfile(ti, io.BytesIO(b))
            elif kind == "link":
                ti = tarfile.TarInfo(m[1]); ti.type = tarfile.LNKTYPE; ti.linkname = m[2]
                t.addfile(ti)


# Layer 1 (bottom): old config + shared source file
layer("layer1.tar", [
    ("dir", "etc/nginx"),
    ("dir", "etc/nginx/conf.d"),
    ("file", "etc/nginx/nginx.conf", "worker_processes 1;\n"),
    ("file", "etc/nginx/conf.d/old.conf", "# old\n"),
    ("file", "etc/nginx/default.conf", "default v1\n"),
    ("dir", "usr/share/nginx/html"),
    ("file", "usr/share/nginx/html/index.html", "<h1>old</h1>\n"),
])

# Layer 2: hardlink + overwrite source later via layer3
layer("layer2.tar", [
    ("link", "etc/nginx/default-link.conf", "etc/nginx/default.conf"),
])

# Layer 3 (top): whiteout old.conf, opaque conf.d with new file, overwrite default
layer("layer3.tar", [
    ("file", "etc/nginx/conf.d/.wh..wh..opq", ""),
    ("file", "etc/nginx/conf.d/site.conf", "server { }\n"),
    ("file", "etc/nginx/.wh.future-delete", ""),
    ("file", "etc/nginx/default.conf", "default v2 OVERWRITE\n"),
])

# Layer 4: delete whole directory scenario separately
layer("del1.tar", [
    ("dir", "var/lib/appdata"),
    ("file", "var/lib/appdata/x", "x"),
])
layer("del2.tar", [
    ("file", "var/lib/.wh.appdata", ""),
])

print("generated in", out)
