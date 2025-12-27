import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    NODE_ID = os.getenv("NODE_ID", "node-1")
    PORT = int(os.getenv("PORT", "8000"))
    REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    REGION = os.getenv("REGION", "default") 