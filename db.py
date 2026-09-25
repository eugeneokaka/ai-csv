"""Database connection for v2 — uses psycopg2 with the same Neon URL as the frontend."""

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create chat, prompt and document tables if they don't exist."""
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

        CREATE TABLE IF NOT EXISTS document (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            s3_key TEXT,
            source TEXT NOT NULL DEFAULT 'upload',
            content_type TEXT,
            size_bytes BIGINT,
            checksum TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW() NOT NULL,
            last_accessed_at TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS document_chatId_idx ON document(chat_id);
        CREATE INDEX IF NOT EXISTS document_chatId_filename_idx ON document(chat_id, filename);
    """)
    conn.commit()
    cur.close()
    conn.close()
