#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Union
import os
import mimetypes
import threading
import shutil
from pathlib import Path
import uvicorn

# ======= Configuration =======
HOST = "0.0.0.0"
PORT = 8000

# ======= 하드코딩 템플릿 MD 파일 경로 =======
# 필요에 맞게 실제 존재하는 경로로 바꿔주세요.
TEMPLATE_MD = "./result.md"

# ======= 업로드 유형 필터 =======
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}
DOC_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"}

# ======= FastAPI App =======
app = FastAPI(title="Multi Converter API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======= Pydantic Models =======
class CreateJobRequest(BaseModel):
    filePath: str
    recursive: Optional[bool] = False
    uploadfrom: Optional[str] = "MG"

class CreateJobResponse(BaseModel):
    multiConvertIdx: int
    processedFiels: int  # Keep original typo for compatibility
    outdir: str
    outfile: str

class StatusResponse(BaseModel):
    status: int
    result: str
    outdir: Optional[str] = None
    outfile: Optional[str] = None


# ======= 단순 증가 인덱스 =======
_idx_lock = threading.Lock()
_idx_value = 0

def next_index() -> int:
    global _idx_value
    with _idx_lock:
        _idx_value += 1
        return _idx_value

# ======= 파일 개수 세기 =======
def count_files(base_path: str, recursive: bool) -> int:
    if not os.path.exists(base_path):
        return 0
    if os.path.isfile(base_path):
        return 1 
    total = 0
    if not recursive:
        for name in os.listdir(base_path):
            p = os.path.join(base_path, name)
            if os.path.isfile(p):
                total += 1
    else:
        for root, _, files in os.walk(base_path):
            for name in files:
                p = os.path.join(root, name)
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

# ======= API Endpoints =======
@app.post("/multiConverter", response_model=CreateJobResponse)
async def create_job(request: CreateJobRequest):
    """
    Creates a new file conversion job.
    Only POST method is allowed.
    """
    if not request.filePath:
        raise HTTPException(status_code=400, detail="filePath is required")

    # 파일 개수 (기존 로직 유지)
    try:
        processed = count_files(request.filePath, request.recursive)
    except Exception:
        processed = 0

    # outfiles 생성 + 템플릿 md 복사
    try:
        outdir, outfile = copy_template_md(request.filePath)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to prepare md: {e}")

    idx = next_index()
    with _JOBS_LOCK:
        JOBS[idx] = {"outdir": outdir, "outfile": outfile}

    return CreateJobResponse(
        multiConvertIdx=idx,
        processedFiels=processed,  # 원 코드의 오탈자 유지
        outdir=outdir,
        outfile=outfile
    )

@app.get("/multiConverterResult", response_model=StatusResponse)
async def get_job_result(multiConvertIdx: int = Query(..., description="Job index returned from /multiConverter")):
    """
    Gets the status of a conversion job.
    Only GET method is allowed.
    """
    try:
        idx = int(multiConvertIdx)
    except Exception:
        raise HTTPException(status_code=400, detail="multiConvertIdx must be int")

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

    response = StatusResponse(
        status=status,
        result=result,
        outdir=meta.get("outdir"),
        outfile=meta.get("outfile")
    )
    
    return response

@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "message": "Multi Converter API",
        "endpoints": {
            "POST /multiConverter": "Create a new conversion job",
            "GET /multiConverterResult": "Get job status",
            "GET /docs": "API documentation"
        }
    }

# ======= Health Check =======
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "template_exists": os.path.exists(TEMPLATE_MD)}

# ======= Run Server =======
def run():
    print(f"* Serving FastAPI on http://{HOST}:{PORT}")
    print(f"* API docs available at http://{HOST}:{PORT}/docs")
    print(f"* Endpoints: POST /multiConverter, GET /multiConverterResult")
    uvicorn.run(app, host=HOST, port=PORT)

if __name__ == "__main__":
    run()