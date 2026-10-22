from fastapi import FastAPI, Response, status, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import asyncio
import random
import time
import re
import logging
from typing import List, Literal
from responses import generate_ai_response
from influxdb import InfluxDBClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Provider")

app = FastAPI(
    title="AI Engineer Challenge - Mock Provider",
    description="Simulates an external environment for IA extraction and notifications.",
    version="1.1.0"
)

influx_client = InfluxDBClient(host='influxdb', port=8086, database='k6')

@app.middleware("http")
async def report_provider_hits(request: Request, call_next):
    response = await call_next(request)
    if "extract" in request.url.path:
        try:
            line = f"provider_hits,endpoint=extract value=1"
            influx_client.write_points([line], protocol='line')
        except:
            pass
    return response

API_KEY = "test-dev-2026"
api_key_header = APIKeyHeader(name="X-API-Key")

async def validate_api_key(api_key: str = Depends(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key"
        )
    return api_key

class Notification(BaseModel):
    to: str = Field(..., example="user@example.com")
    message: str = Field(..., example="Your verification code is 1234")
    type: Literal["email", "sms"] = Field(..., example="email")

class NotificationResponse(BaseModel):
    status: str = Field(..., example="delivered")
    provider_id: str = Field(..., example="p-1234")

class ChatMessage(BaseModel):
    role: str = Field(default="assistant", example="assistant")
    content: str = Field(..., example="Hello!")

class AIRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., example=[
        {"role": "system", "content": "You are an extractor."},
        {"role": "user", "content": "Send email to test@test.com"}
    ])

class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class AIResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{random.randint(1000, 9999)}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "ai-engine-v1"
    choices: List[ChatChoice]

class ErrorResponse(BaseModel):
    error: str = Field(..., example="Rate limit exceeded")


FAIL_RATE = 0.0
LATENCY_MIN = 0.1
LATENCY_MAX = 0.5
RATE_LIMIT_THRESHOLD = 50
MAX_CONCURRENT_REQUESTS = 50

semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
request_counts: List[float] = []

@app.post(
    "/v1/notify",
    tags=["Notifications"],
    summary="Send notification",
    description="""
Sends a notification to the provider. 
- Requires 'X-API-Key' header. 
- This endpoint can return 429 (rate limit) or 500 (random error) to test client resilience.
""",
    response_model=NotificationResponse,
    responses={
        200: {"model": NotificationResponse},
        401: {
            "model": ErrorResponse, 
            "description": "Unauthorized - Missing or invalid API Key",
            "content": {"application/json": {"example": {"error": "Invalid API Key"}}}
        },
        429: {
            "model": ErrorResponse, 
            "description": "Rate limit exceeded",
            "content": {"application/json": {"example": {"error": "Rate limit exceeded"}}}
        },
        500: {
            "model": ErrorResponse, 
            "description": "Random server error",
            "content": {"application/json": {"example": {"error": "External server error"}}}
        }
    }
)
async def notify(
    notification: Notification, 
    response: Response,
    priority: Literal["low", "normal", "high"] = "normal",
    trace_id: str | None = None,
    api_key: str = Depends(validate_api_key)
):
    global request_counts
    
    async with semaphore:
        now = time.time()
        request_counts = [t for t in request_counts if now - t < 10]
        if len(request_counts) >= RATE_LIMIT_THRESHOLD:
            logger.warning("429 Rate Limit Exceeded")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded"
            )
        request_counts.append(now)
        await asyncio.sleep(random.uniform(LATENCY_MIN, LATENCY_MAX))
        if random.random() < FAIL_RATE:
            logger.error("500 Random Failure triggered")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="External server error"
            )
        return {
            "status": "delivered", 
            "provider_id": f"p-{random.randint(1000, 9999)}"
        }

@app.post("/v1/ai/extract", tags=["AI"], response_model=AIResponse)
async def ai_extract(request: AIRequest, api_key: str = Depends(validate_api_key)):
    """
    Simulates a stochastic LLM response.
    - Expects a list of messages (system, user, assistant).
    - Extracts email or phone from the last 'user' message.
    - Limits types to 'email' or 'sms'.
    - If entities are missing, returns plain text explanation.
    - Otherwise, applies a distribution of successful/noisy/failed responses.
    """
    await asyncio.sleep(random.uniform(1.5, 3.0))
    
    user_messages = [m.content for m in request.messages if m.role == "user"]
    if not user_messages:
        return {
            "choices": [{"message": {"content": "Error: No user message found in the request."}}]
        }
    
    prompt = user_messages[-1].lower()
    
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', prompt)
    phone_match = re.search(r'\b\d{3}-?\d{3}-?\d{3,4}\b', prompt)
    
    target = email_match.group(0) if email_match else (phone_match.group(0) if phone_match else None)
    
    notif_type = None
    if "email" in prompt or (email_match and "sms" not in prompt):
        notif_type = "email"
    elif "sms" in prompt or "teléfono" in prompt or phone_match:
        notif_type = "sms"

    if not target or not notif_type:
        missing = "destination (email/phone)" if not target else "notification type (email/sms)"
        if not target and not notif_type: missing = "both destination and type"
        
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": f"I cannot process the request. I was unable to identify the {missing} in your prompt."
                }
            }]
        }

    msg = prompt.split(":")[-1].strip() if ":" in prompt else prompt[:50]
    
    content = generate_ai_response(target, msg, notif_type)

    return {
        "choices": [{"message": {"role": "assistant", "content": content}}]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)
