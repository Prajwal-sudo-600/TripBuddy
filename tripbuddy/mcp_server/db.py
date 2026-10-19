"""
Database schema and connection management for TripBuddy.
"""

import os
import sqlite3
from typing import Optional

DEFAULT_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "tripbuddy.db")
)


def get_db_path(db_path: Optional[str] = None) -> str:
    """Return the active SQLite database path."""
    if db_path:
        return db_path
    return os.environ.get("TRIPBUDDY_DB_PATH", DEFAULT_DB_PATH)


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create and return a SQLite connection configured with Row factory."""
    path = get_db_path(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Create database tables if they do not exist."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hotels (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    city TEXT NOT NULL,
                    area TEXT NOT NULL,
                    price_per_night REAL NOT NULL,
                    avg_rating REAL NOT NULL,
                    review_count INTEGER NOT NULL,
                    contact_email TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hotel_id INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    rating REAL NOT NULL,
                    text TEXT NOT NULL,
                    date TEXT NOT NULL,
                    FOREIGN KEY(hotel_id) REFERENCES hotels(id)
                );

                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    nationality TEXT NOT NULL,
                    passport_number TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hotel_id INTEGER NOT NULL,
                    check_in TEXT NOT NULL,
                    check_out TEXT NOT NULL,
                    guest_name TEXT NOT NULL,
                    confirmation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(hotel_id) REFERENCES hotels(id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    to_address TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args TEXT NOT NULL,
                    result TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_descriptions (
                    tool_name TEXT PRIMARY KEY,
                    description TEXT NOT NULL
                );
                """
            )
    finally:
        conn.close()
