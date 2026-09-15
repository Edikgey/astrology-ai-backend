from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api import endpoints, auth, payments, relationships

api_app = FastAPI()

api_app.include_router(auth.router)
api_app.include_router(endpoints.router)
api_app.include_router(payments.router)
api_app.include_router(relationships.router)

# Wrap ServerErrorMiddleware too, so unhandled 500s retain CORS headers.
# Keep the existing explicit local/production origin allowlist.
app = CORSMiddleware(
    app=api_app,
    allow_origins=[
        "http://localhost:3000",
        "https://astrology-ai-frontend-production.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
