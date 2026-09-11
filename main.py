from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api import endpoints, auth

app = FastAPI()

# CORS: разрешаем локальный frontend и production frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://astrology-ai-frontend-production.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем роутеры
app.include_router(auth.router)
app.include_router(endpoints.router)