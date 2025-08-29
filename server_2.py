#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import mimetypes
import threading
import shutil
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8000
PATH_CREATE = "/multiConverter"
PATH_RESULT = "/multiConverterResult"

# ======= 하드코딩 템플릿 MD 파일 경로 =======
# 필요에 맞게 실제 존재하는 경로로 바꿔주세요.
TEMPLATE_MD = "./result.md"

# ======= 업로드 유형 필터 =======
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}
DOC_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"}


def file_matches(path: str, uploadform: str) -> bool:
    if uploadform is None or str(uploadform).lower() == "all":
        return True
    ext = os.path.splitext(path)[1].lower()
    uf = str(uploadform).lower()
    if uf == "image":
        return ext in IMAGE_EXT
    if uf == "document":
        if ext in DOC_EXT:
            return True
        mime, _ = mimetypes.guess_type(path)
        return (mime or "").startswith("text/")
    return True


# ======= 단순 증가 인덱스 =======
_idx_lock = threading.Lock()
_idx_value = 0


def next_index() -> int:
    global _idx_value
    with _idx_lock:
        _idx_value += 1
        return _idx_value


# ======= 파일 개수 세기 =======
def count_files(base_path: str, recursive: bool, uploadform: str) -> int:
    if not os.path.exists(base_path):
        return 0
    if os.path.isfile(base_path):
        return 1 if file_matches(base_path, uploadform) else 0
    total = 0
    if not recursive:
        for name in os.listdir(base_path):
            p = os.path.join(base_path, name)
            if os.path.isfile(p) and file_matches(p, uploadform):
                total += 1
    else:
        for root, _, files in os.walk(base_path):
            for name in files:
                p = os.path.join(root, name)
                if file_matches(p, uploadform):
                    total += 1
    return total


# ======= 상태 저장소 (0 -> 3 -> 1 -> 0 ...) =======
class StatusStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = {}  # idx -> 호출 횟수

    def next_status(self, idx: int) -> int:
        with self._lock:
            cnt = self._calls.get(idx, 0) + 1
            self._calls[idx] = cnt
            # 패턴: 0,3,1 반복
            mod = (cnt - 1) % 3
            if mod == 0:
                return 0
            elif mod == 1:
                return 3
            else:
                return 1


STATUS_STORE = StatusStore()

# ======= 작업 메타 저장소 =======
# idx -> {"outdir": str, "outfile": str}
JOBS = {}
_JOBS_LOCK = threading.Lock()


def resolve_outdir(file_path: str) -> Path:
    p = Path(file_path)
    if p.is_file():
        return p.parent / "outfiles"
    return p / "outfiles"


def copy_template_md(file_path: str) -> tuple[str, str]:
    """
    TEMPLATE_MD 를 outfiles 디렉토리에 복사.
    - file_path 가 '파일'이면: 목적지 파일명 = <원본파일명_확장자제외>.md
    - file_path 가 '디렉토리'면: 목적지 파일명 = index.md
    반환: (outdir_str, outfile_str)
    """
    if not os.path.exists(TEMPLATE_MD):
        raise FileNotFoundError(f"TEMPLATE_MD not found: {TEMPLATE_MD}")

    outdir = resolve_outdir(file_path)
    outdir.mkdir(parents=True, exist_ok=True)

    p = Path(file_path)
    if p.is_file():
        stem = p.stem  # 확장자 제외 파일명
        dest_name = f"{stem}.md"
    else:
        dest_name = "index.md"

    dest_path = outdir / dest_name
    shutil.copy2(TEMPLATE_MD, dest_path)

    return (str(outdir), str(dest_path))


# ======= HTTP Handler =======
class Handler(BaseHTTPRequestHandler):
    def _set_headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        if self.path.startswith(PATH_CREATE) or self.path.startswith(PATH_RESULT):
            self._set_headers(200)
        else:
            self._set_headers(404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == PATH_CREATE:
            # Only POST allowed for /multiConverter
            self._set_headers(405)
            self.wfile.write(b'{"error":"Method Not Allowed. Use POST for /multiConverter"}')
            return
        elif parsed.path == PATH_RESULT:
            params = parse_qs(parsed.query)
            resp = self._handle_result(params=params, body=None)
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error":"not found"}')
            return
        self._set_headers(resp.pop("_status", 200))
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        params = parse_qs(parsed.query)

        if parsed.path == PATH_CREATE:
            resp = self._handle_create(params=params, body=body)
        elif parsed.path == PATH_RESULT:
            # Only GET allowed for /multiConverterResult
            self._set_headers(405)
            self.wfile.write(b'{"error":"Method Not Allowed. Use GET for /multiConverterResult"}')
            return
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error":"not found"}')
            return
        self._set_headers(resp.pop("_status", 200))
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    # ---------- /multiConverter ----------
    def _handle_create(self, params, body):
        def pick(name, default=None):
            if isinstance(body, dict) and name in body:
                return body.get(name, default)
            if name in params:
                return params[name][0]
            return default

        filePath = pick("filePath")
        recursive = pick("recursive", False)
        uploadform = pick("uploadform", "all")

        if isinstance(recursive, str):
            recursive = recursive.lower() in ("true", "1", "yes", "y")

        if not filePath:
            return {"_status": 400, "error": "filePath is required"}

        # 파일 개수 (기존 로직 유지)
        try:
            processed = count_files(filePath, recursive, uploadform)
        except Exception:
            processed = 0

        # outfiles 생성 + 템플릿 md 복사
        try:
            outdir, outfile = copy_template_md(filePath)
        except FileNotFoundError as e:
            return {"_status": 500, "error": str(e)}
        except Exception as e:
            return {"_status": 500, "error": f"failed to prepare md: {e}"}

        idx = next_index()
        with _JOBS_LOCK:
            JOBS[idx] = {"outdir": outdir, "outfile": outfile}

        return {
            "_status": 200,
            "multiConvertIdx": idx,
            "processedFiels": processed,  # 원 코드의 오탈자 유지
            "outdir": outdir,
            "outfile": outfile,
        }

    # ---------- /multiConverterResult ----------
    def _handle_result(self, params, body):
        def pick(name, default=None):
            if isinstance(body, dict) and name in body:
                return body.get(name, default)
            if name in params:
                return params[name][0]
            return default

        mci = pick("multiConvertIdx")
        if mci is None:
            return {"_status": 400, "error": "multiConvertIdx is required"}
        try:
            idx = int(mci)
        except Exception:
            return {"_status": 400, "error": "multiConvertIdx must be int"}

        status = STATUS_STORE.next_status(idx)
        with _JOBS_LOCK:
            meta = JOBS.get(idx, {})

        if status == 0:
            result = "등록완료"
        elif status == 1:
            # outfiles 경로 요청 시 경로 정보 반환(추가로 outfile도 함께 제공)
            result = meta.get("outdir", "outfiles 경로")
        elif status == 2:
            result = "실패사유"
        elif status == 3:
            result = "진행률"
        else:
            result = "RUNNING"

        resp = {"_status": 200, "status": status, "result": result}
        if meta:
            resp.update({"outdir": meta.get("outdir"), "outfile": meta.get("outfile")})
        return resp


def run():
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"* Serving on http://{HOST}:{PORT}{PATH_CREATE} and {PATH_RESULT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
