"""Entry point:  uvicorn app.main:app --reload"""
from app.factory import create_app

app = create_app()
