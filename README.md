# 容器镜像层目录提取服务（FastAPI）

把旧镜像中某个目录（例如 `/etc/nginx`、`/usr/share/nginx/html`）从一组
**未压缩的镜像层 tar** 中合并出来，下载为一个可以独立解开的 tar，用于把
镜像内的默认配置/资源迁移到挂载卷。

- 按**从底到顶**的顺序接收镜像层；
- 正确处理目录、普通文件、指向“已出现文件”的硬链接；
- 正确处理 OCI/AUFS 删除标记 `.wh.<name>` 与不透明目录标记
  `.wh..wh..opq`（以及 `user.overlay.opaque` xattr）；
- 删除/不透明标记只遮蔽**下层**内容，同层新增文件照常导出；
- 硬链接固化“创建那一刻”的内容：之后原路径即使被上层覆盖，旧硬链接仍
  指向旧 inode 的字节；
- 导出包尽量保留硬链接关系；链接目标不在导出目录内时，该成员直接携带
  原始字节（退化为普通文件），保证 tar 单独解开不报错；
- 被删除的目录返回 `404 directory_not_found`；
- 不会把层内无关文件按原路径解包落盘：只扫描元数据，普通文件字节以
  tar 内偏移引用的方式按需读取，导出结束即清理临时文件。

核心合并逻辑在 `app/engine.py`，**仅依赖 Python 标准库**；HTTP 层在
`app/main.py`。

---

## 1. docker compose 启动

```bash
docker compose up --build
# 或旧命令： docker-compose up --build
```

服务监听 `http://localhost:8000`：

- 交互式上传页：<http://localhost:8000/>
- Swagger 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

compose 会挂载命名卷 `extractor-data:/data`，用于会话层与临时文件。
宿主机端口可用环境变量覆盖：`HOST_PORT=9000 docker compose up`。

本地直接运行（无需 Docker）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. 一次性提取接口（最常用）

`POST /extract`（同时提供 `/api/extract`、`/api/v1/extract` 等别名；
其它任意 `POST/PUT` 路径也会按同样规则处理）。

- `multipart/form-data`
  - 所有文件部分都会按 **multipart 中的先后顺序**当作镜像层（从底到顶）；
    字段名不限，`layers` / `files` / `tars` / `archives` 均可。
  - 普通表单字段 `path`（别名 `dir` / `directory` / `target` /
    `target_dir` / `dir_path` / `folder` 等）：容器内目录。
  - 可选 `layout`：包内路径风格，见下。
  - 可选 `compress=gzip`：返回 `.tar.gz`。
- 目录也可以放在 query 或 URL 路径中：
  `POST /extract?path=/etc/nginx`、`POST /extract/etc/nginx`。
- 也支持：
  - 直接把单个 tar 作为 raw request body，path 走 query；
  - `application/json`：`{"path": "/etc/nginx", "layers": ["<base64 tar>", ...]}`。

### curl 示例

```bash
curl -fS -o nginx.tar \
  -F "path=/etc/nginx" \
  -F "layers=@layer1-bottom.tar" \
  -F "layers=@layer2.tar" \
  -F "layers=@layer3-top.tar" \
  http://localhost:8000/extract

# 解开到挂载卷（示例）
mkdir -p /mnt/volume && tar -xf nginx.tar -C /mnt/volume
```

Python 示例：

```python
import requests

with open("layer1.tar", "rb") as f1, open("layer2.tar", "rb") as f2:
    resp = requests.post(
        "http://localhost:8000/api/extract",
        data={"path": "/etc/nginx"},
        files=[
            ("layers", ("layer1.tar", f1, "application/x-tar")),  # 底层
            ("layers", ("layer2.tar", f2, "application/x-tar")),  # 顶层
        ],
        timeout=600,
    )
resp.raise_for_status()
open("nginx.tar", "wb").write(resp.content)
```

### 返回

- `200`：`application/x-tar`（或 `compress=gzip` 时 `application/gzip`），
  附 `Content-Disposition` 与 `X-Member-Count` / `X-Hardlink-Count` /
  `X-Layout` 头。
- `404`：目录在最终合并视图中不存在（被 white-out 删除且未重建）：
  ```json
  {"error": "directory_not_found", "path": "etc/nginx", "detail": "..."}
  ```
- `400`：tar 无法解析、没有上传任何层、或目标路径不是目录。

### 包内路径风格 `layout`

假设请求目录是 `/etc/nginx`：

| layout | 包内名字示例 | 说明 |
| --- | --- | --- |
| `basename`（默认，别名 `dir`/`docker`） | `nginx/nginx.conf` | 以目录 basename 为顶层，等价 `tar -C /etc -cf nginx.tar nginx` |
| `relative`（别名 `contents`/`flat`） | `nginx.conf`、`conf.d/x` | 直接是目录内容，适合直接解到挂载卷根 |
| `full`（别名 `absolute`/`container`） | `etc/nginx/nginx.conf` | 保留容器内完整路径 |

可通过环境变量 `DEFAULT_LAYOUT=relative` 修改默认风格。

---

## 3. 会话式上传（先逐层上传，再指定目录下载）

```bash
# 1) 创建会话
SID=$(curl -s -X POST http://localhost:8000/sessions | jq -r .session_id)

# 2) 按从底到顶顺序上传（可一次多个，也可多次调用，按序号追加）
curl -F "layers=@layer1.tar" -F "layers=@layer2.tar" \
  http://localhost:8000/sessions/$SID/layers
curl -F "layers=@layer3.tar" \
  http://localhost:8000/sessions/$SID/layers

# 3) 指定目录下载
curl -o nginx.tar \
  "http://localhost:8000/sessions/$SID/download?path=/etc/nginx"

# 也可以用一次性接口引用同一会话
curl -o nginx.tar -F session_id=$SID -F path=/etc/nginx \
  http://localhost:8000/extract

# 清理
curl -X DELETE http://localhost:8000/sessions/$SID
```

相关别名：`POST /api/sessions`、`POST /upload`、`POST /layers`、
`POST /sessions/{id}/upload`、`GET /sessions/{id}/extract|tar` 等。

如果不带任何会话标识直接反复调用 `POST /upload` / `POST /layers`，层会
按顺序累积到共享的 `default` 栈，随后可直接 `POST /extract`（只带 `path`，
不带文件、不带 `session_id`）完成提取。也支持 `X-Session-Id` 请求头与
`sid` Cookie。

---

## 4. 命令行

```bash
python -m app.cli \
  --layer layer1.tar --layer layer2.tar --layer layer3.tar \
  --dir /etc/nginx --out nginx.tar --layout basename
```

目录不存在时退出码为 `3`。

---

## 5. 合并与导出语义（实现说明）

1. 路径统一规范化为根相对 POSIX 路径（去掉 `/`、`./`，折叠 `..`，
   但不逃逸虚拟根）。
2. 每个普通文件成员创建一个新的不可变 `Node`，其字节通过
   `(层 tar 路径, offset, size)` 懒引用；同路径再次出现普通文件
   （覆盖）会创建**新的 Node**。
3. 遇到 tar 硬链接成员（`LNKTYPE`）时，按归档根路径（并兼容相对
   成员目录的写法）找到“当前已出现”的目标 Node，把当前路径绑定到
   **同一个 Node**。因此之后原路径被覆盖（绑定到新 Node）不会影响
   旧硬链接的内容。
4. `.wh.foo` 仅删除/递归删除此前低层提供的 `foo`；当前层（同一层
   tar 内）的条目一律保留，与成员先后顺序无关。
5. `.wh..wh..opq` 清空该目录下来自低层的所有条目，本层条目保留。
6. 最终导出时按 Node 分组硬链接簇：
   - 簇内优先让“最初的普通文件绑定路径”作为携带字节的 regular
     member，其余成员写为 tar hard link（`LNKTYPE`，linkname 按所选
     layout 重写），并保证 regular member 先于 link member 输出；
   - 若整个簇在导出目录内没有原始普通文件（典型：链接目标在目录外，
     或原路径后来被覆盖），则选一个包内成员携带真实字节，其余包内
     别名再链接到它；单个外部链接因此会变成**自带字节的普通文件**，
     满足“包能独立解开”。
7. 只导出目标目录子树；无关条目的字节不会被读出或写出，更不会落到
   服务器磁盘的原始路径上。

---

## 6. 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

测试覆盖：多层覆盖、三种包内路径风格、white-out 文件/递归目录删除、
opaque（含同层不同成员顺序）、硬链接关系、覆盖后的内容快照、包外目标
内联字节、输出 tar 真实解包后的 inode 硬链接、404，以及 HTTP/会话流程。

## 目录结构

```
app/
  engine.py    # 纯标准库：层合并 + white-out/opaque + 硬链接 + 导出 tar
  main.py      # FastAPI 应用与兼容路由
  cli.py       # 命令行
main.py        # `uvicorn main:app` 入口垫片
server.py      # `uvicorn server:app` 入口垫片
compose.yaml / docker-compose.yml
Dockerfile
requirements.txt
tests/
```
