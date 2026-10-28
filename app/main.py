from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router
from app.db import initialize

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize()
    yield

app=FastAPI(title="YottaFlex Demand Forecasting & Replenishment API",
            description="Synthetic retail demand forecasting, constrained replenishment and validation.",
            version="1.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=False,allow_methods=["*"],allow_headers=["*"])
app.include_router(router)

@app.get("/")
def root():
    return {"app":"YottaFlex Demand Forecasting & Replenishment","status":"ready",
            "docs":"/docs","workflow":["generate","forecast","replenishment","validate"]}
