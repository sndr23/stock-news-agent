# filepath: src/api/main.py
"""
FastAPI 后端 API
A股资讯监测 Agent
"""
from datetime import datetime
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.agent.graph import run_agent
from src.tools.data_fetchers import get_stock_news, get_announcements


app = FastAPI(
    title="A股资讯监测Agent API",
    description="每日获取股市资讯，噪音清除，按重要度和可信度排序",
    version="4.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateReportRequest(BaseModel):
    thread_id: Optional[str] = "default"


@app.get("/")
async def root():
    """根路径，返回前端页面"""
    html_path = Path(__file__).parent.parent.parent / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return {"message": "A股资讯监测Agent API", "status": "running"}


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/api/market_data")
async def get_market_data():
    """获取原始资讯数据（直接返回，不经过Agent）"""
    try:
        raw_news = get_stock_news.invoke({"data_mode": "live"})
        announcements = get_announcements.invoke({"data_mode": "live"})

        return {
            "status": "success",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "news": raw_news,
                "announcements": announcements
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate_report")
async def generate_report(request: GenerateReportRequest):
    """运行Agent：获取当日全部资讯 → 两阶段过滤 → 评分排名"""
    try:
        result = run_agent(
            data_mode="live",
            thread_id=request.thread_id or "default"
        )

        return {
            "status": "success",
            "thread_id": request.thread_id or "default",
            "ranked_news": result.get("ranked_news", []),
            "ranked_news_count": len(result.get("ranked_news", [])),
            "raw_news_count": len(result.get("raw_news", [])),
            "filtered_news_count": len(result.get("filtered_news", [])),
            "messages": [str(m.content) if hasattr(m, 'content') else str(m) for m in result.get("messages", [])]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def get_config():
    """获取配置"""
    return {
        "data_mode": "live"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
