"""FastAPI 入口：事故导排网络检修审计业务 API 与静态页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .flow import NetworkValidationError, audit_network
from .flowplan import plan_low_exposure

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="化工园区事故导排网络检修审计",
    version=__version__,
    description=(
        "录入泄压源、安全焚烧端、汇合节点与带方向/容量/检修标记的管段，"
        "在正常网络及每条可检修管段临时失效后的残余网络上独立求最大流，"
        "判定事故持续排出流量是否始终可达；审计放行后按每条管段的非负整数"
        "单位暴露代价生成低暴露配流单（最小费用流 + 录入顺序字典序决胜）。"
    ),
)


@app.exception_handler(NetworkValidationError)
async def _on_validation_error(_: Request, exc: NetworkValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.message, "field": exc.field},
    )


@app.get("/health")
async def health() -> dict:
    """容器健康检查端点。"""
    return {"status": "ok", "service": "flare-audit", "version": __version__}


@app.post("/api/audit")
async def audit(request: Request) -> dict:
    """对一份导排网络草稿执行检修审计。

    正常网络与每个单管段移除情景**独立**计算最大流；全部达标才放行。
    方向、容量、节点引用等业务输入无效时返回 400。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是合法 JSON", "field": None})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象", "field": None})

    result = audit_network(
        source=payload.get("source"),
        sink=payload.get("sink"),
        nodes=payload.get("nodes", []),
        edges=payload.get("edges", []),
        required_flow=payload.get("required_flow"),
    )
    result["service"] = "flare-audit"
    result["version"] = __version__
    return result


@app.post("/api/plan")
async def plan(request: Request) -> dict:
    """对一份已通过审计的草稿生成低暴露配流单。

    服务端先按既有规则重新审计完整草稿；审计不放行时不生成配流单
    （HTTP 200 + ``passed: false``，响应内嵌完整审计结论与首条失效
    管段、割集证据）。放行后在正常网络与每条可检修管段单独失效后的
    残余网络中，分别分配**恰好等于**必须持续排出量的流量；每种情形
    返回各管段流量、总代价与情形编号。方向、容量、非负整数代价等
    业务输入无效时返回 400。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是合法 JSON", "field": None})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象", "field": None})

    result = plan_low_exposure(
        source=payload.get("source"),
        sink=payload.get("sink"),
        nodes=payload.get("nodes", []),
        edges=payload.get("edges", []),
        required_flow=payload.get("required_flow"),
    )
    result["service"] = "flare-audit"
    result["version"] = __version__
    return result


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
