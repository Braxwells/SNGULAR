import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from service.config import settings
from service.pipeline import NotificationPipeline
from service.provider_client import ProviderClient
from service.store import RequestRecord, RequestStore, Status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("notification_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Un único cliente HTTP compartido (pool de conexiones keep-alive).
    http = httpx.AsyncClient(
        base_url=settings.provider_base_url,
        headers={"X-API-Key": settings.provider_api_key},
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
    )
    store = RequestStore()
    pipeline = NotificationPipeline(store, ProviderClient(http))
    app.state.store = store
    app.state.pipeline = pipeline
    try:
        yield
    finally:
        await pipeline.shutdown()
        await http.aclose()


app = FastAPI(title="Notification Service (Technical Test)", lifespan=lifespan)


class CreateRequestBody(BaseModel):
    user_input: str = Field(..., min_length=1, max_length=settings.max_user_input_length)

    @field_validator("user_input")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("user_input must not be blank")
        return value


class CreateRequestResponse(BaseModel):
    id: str


class RequestStatusResponse(BaseModel):
    id: str
    status: Status


def _get_record(request: Request, request_id: str) -> RequestRecord:
    record = request.app.state.store.get(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return record


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.post("/v1/requests", status_code=status.HTTP_201_CREATED, response_model=CreateRequestResponse)
async def create_request(body: CreateRequestBody, request: Request) -> CreateRequestResponse:
    record = request.app.state.store.create(body.user_input)
    return CreateRequestResponse(id=record.id)


@app.post(
    "/v1/requests/{request_id}/process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RequestStatusResponse,
)
async def process_request(request_id: str, request: Request) -> RequestStatusResponse:
    record = _get_record(request, request_id)
    # Idempotente: llamadas repetidas no relanzan el procesamiento.
    request.app.state.pipeline.submit(record)
    return RequestStatusResponse(id=record.id, status=record.status)


@app.get("/v1/requests/{request_id}", response_model=RequestStatusResponse)
async def get_request(request_id: str, request: Request) -> RequestStatusResponse:
    record = _get_record(request, request_id)
    return RequestStatusResponse(id=record.id, status=record.status)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
