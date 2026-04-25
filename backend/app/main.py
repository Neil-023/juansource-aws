import os

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from .fact_checker import run_fact_check
# from .fact_checkerOLLAMA import run_fact_check as run_fact_check_ollama
from .prompt_guard import enforce_prompt_guard, ensure_anonymous_cookie, initialize_prompt_guard_schema
from .semantic_cache import initialize_semantic_cache_schema

app = FastAPI(title="JuanSource API")


def get_cors_origins() -> List[str]:
    default_origins = [
        "https://juansource.mooo.com",
        "http://juansource.mooo.com",
        "https://neil-023.github.io",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    raw_origins = (os.getenv("CORS_ALLOW_ORIGINS") or "").strip()
    if not raw_origins:
        return default_origins
    parsed_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return parsed_origins or default_origins

# Enable CORS so React (running on a different port) can talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.api_route("/health", methods=["GET", "HEAD"])
@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "online", "server": "oracle-amd-1gb"}

# Pydantic model to define the structure of the request body
class ClaimRequest(BaseModel):
    claim: str

class FactCheckResponse(BaseModel):
    classification: str
    reasoning: str
    evidence: List[str]
    raw: str


@app.on_event("startup")
async def startup_event():
    try:
        initialize_semantic_cache_schema()
        print("Semantic cache schema initialized.")
    except Exception as exc:
        # Keep API booting even if DB/cache is temporarily unavailable.
        print(f"Semantic cache initialization skipped: {exc}")

    try:
        initialize_prompt_guard_schema()
        print("Prompt guard schema initialized.")
    except Exception as exc:
        print(f"Prompt guard initialization skipped: {exc}")

@app.post("/fact-check", response_model=FactCheckResponse, dependencies=[Depends(enforce_prompt_guard)])
@app.post("/api/fact-check", response_model=FactCheckResponse, dependencies=[Depends(enforce_prompt_guard)])
async def fact_check_endpoint(request: ClaimRequest):
    result = run_fact_check(request.claim)
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.get("/session/bootstrap")
@app.get("/api/session/bootstrap")
async def session_bootstrap(request: Request, response: Response):
    anonymous_id = ensure_anonymous_cookie(request, response)
    return {"status": "ok", "anonymous_id": anonymous_id}

# @app.post("/fact-check-ollama", response_model=FactCheckResponse)
# async def fact_check_ollama_endpoint(request: ClaimRequest):
#     result = run_fact_check_ollama(request.claim)
#     if "error" in result:
#         raise HTTPException(status_code=500, detail=result["error"])
#     return result
