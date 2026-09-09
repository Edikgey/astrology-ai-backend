from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api import endpoints, auth

app = FastAPI()

# 🔓 Разрешаем все источники (временно)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ← разрешает доступ с любого домена
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 📦 Подключаем роутеры
app.include_router(auth.router)
app.include_router(endpoints.router)