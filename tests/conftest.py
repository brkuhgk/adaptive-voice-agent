import pytest

from app import supabase_db
from app.config import settings


@pytest.fixture(autouse=True)
def no_real_supabase(monkeypatch):
    """.env may hold real Supabase keys; tests must never write to that database."""
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_key", "")
    monkeypatch.setattr(supabase_db, "_transport", None)
    supabase_db.clear_cache()
