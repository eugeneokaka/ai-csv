"""Database connection for v2 — uses psycopg2 with the same Neon URL as the frontend."""

import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_b6SUnrYcDgj7@ep-sweet-cake-b4ceoidv-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require",
)


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create chat and prompt tables if they don't exist."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New Chat',
            opencode_session_id TEXT,
            created_at TIMESTAMP DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW() NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_userId_idx ON chat(user_id);

        CREATE TABLE IF NOT EXISTS prompt (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            question TEXT,
            answer TEXT,
            code TEXT,
            files JSONB,
            duration_s FLOAT,
            attempts INTEGER,
            created_at TIMESTAMP DEFAULT NOW() NOT NULL
        );
        CREATE INDEX IF NOT EXISTS prompt_chatId_idx ON prompt(chat_id);
    """)
    conn.commit()
    cur.close()
    conn.close()
