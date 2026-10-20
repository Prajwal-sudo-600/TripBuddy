"""
FastMCP Server for TripBuddy.
Exposes EXACTLY 5 tools:
1. search_hotels
2. read_reviews
3. get_profile
4. send_message
5. book_hotel

Tool descriptions are dynamically loaded from the database (tool_descriptions).
Every tool call that runs is recorded in the events table.
Admin HTTP routes are registered via FastMCP custom routes.
"""

from datetime import date, datetime, timezone
import functools
import inspect
import json
import os
import secrets
from typing import Any, Dict, List, Optional
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

from .admin import register_admin_routes
from .db import get_db_connection, init_db
from .seed import DEFAULT_TOOL_DESCRIPTIONS, seed_database


def _log_tool_event(
    tool_name: str, args: Dict[str, Any], result: Any, db_path: Optional[str] = None
) -> None:
    """Record executed tool call into the events table."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            timestamp = datetime.now(timezone.utc).isoformat()
            args_json = json.dumps(args, default=str)
            result_json = json.dumps(result, default=str)
            conn.execute(
                """
                INSERT INTO events (timestamp, tool_name, args, result)
                VALUES (?, ?, ?, ?);
                """,
                (timestamp, tool_name, args_json, result_json),
            )
    finally:
        conn.close()


def _get_description_from_db(tool_name: str, db_path: Optional[str] = None) -> str:
    """Retrieve tool description from tool_descriptions table, falling back to default."""
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT description FROM tool_descriptions WHERE tool_name = ?;",
            (tool_name,),
        ).fetchone()
        if row and row["description"]:
            return row["description"]
        return DEFAULT_TOOL_DESCRIPTIONS.get(tool_name, "")
    except Exception:
        return DEFAULT_TOOL_DESCRIPTIONS.get(tool_name, "")
    finally:
        conn.close()


class TripBuddyFastMCP(FastMCP):
    """
    Subclass of FastMCP that overrides list_tools() to dynamically load
    tool descriptions from SQLite so they can be changed at runtime.
    """

    def __init__(self, *args, db_path: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_path = db_path

    def sync_tool_descriptions(self) -> None:
        """Update in-memory tool descriptions from tool_descriptions table."""
        conn = get_db_connection(self.db_path)
        try:
            rows = conn.execute(
                "SELECT tool_name, description FROM tool_descriptions;"
            ).fetchall()
            for row in rows:
                name = row["tool_name"]
                desc = row["description"]
                try:
                    tool_obj = self._tool_manager.get_tool(name)
                    tool_obj.description = desc
                except KeyError:
                    pass
        except Exception:
            pass
        finally:
            conn.close()

    async def list_tools(self) -> list[MCPTool]:
        """Dynamically refresh tool descriptions from SQLite before returning."""
        self.sync_tool_descriptions()
        return await super().list_tools()


def create_app(
    db_path: Optional[str] = None, seed_if_empty: bool = True
) -> TripBuddyFastMCP:
    """Initialize database and build the TripBuddy FastMCP application."""
    init_db(db_path)

    # Check if database has hotels; if not and seed_if_empty is True, seed it.
    conn = get_db_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) as count FROM hotels;").fetchone()
        if seed_if_empty and (not row or row["count"] == 0):
            seed_database(db_path=db_path)
    finally:
        conn.close()

    mcp = TripBuddyFastMCP(
        name="TripBuddy",
        host="127.0.0.1",
        port=8000,
        streamable_http_path="/mcp",
        db_path=db_path,
    )

    def tool_audit(tool_name: str):
        """Decorator to intercept tool execution and write to events table."""

        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                sig = inspect.signature(fn)
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args = dict(bound.arguments)
                try:
                    res = fn(*args, **kwargs)
                    _log_tool_event(tool_name, call_args, res, db_path=db_path)
                    return res
                except Exception as e:
                    _log_tool_event(
                        tool_name, call_args, {"error": str(e)}, db_path=db_path
                    )
                    raise

            return wrapper

        return decorator

    # Register EXACTLY 5 MCP Tools:

    # 1. search_hotels
    desc_search = _get_description_from_db("search_hotels", db_path)

    @mcp.tool(name="search_hotels", description=desc_search)
    @tool_audit("search_hotels")
    def search_hotels(
        city: str,
        max_price_per_night: Optional[float] = None,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for available hotels in a city.
        Returns a list of {id, name, area, price_per_night, avg_rating, review_count, contact_email}.
        """
        db = get_db_connection(db_path)
        try:
            query = "SELECT id, name, area, price_per_night, avg_rating, review_count, contact_email FROM hotels WHERE LOWER(city) = LOWER(?)"
            params: List[Any] = [city.strip()]

            if max_price_per_night is not None:
                query += " AND price_per_night <= ?"
                params.append(float(max_price_per_night))

            query += " ORDER BY avg_rating DESC, price_per_night ASC;"
            rows = db.execute(query, params).fetchall()
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "area": row["area"],
                    "price_per_night": row["price_per_night"],
                    "avg_rating": row["avg_rating"],
                    "review_count": row["review_count"],
                    "contact_email": row["contact_email"],
                }
                for row in rows
            ]
        finally:
            db.close()

    # 2. read_reviews
    desc_reviews = _get_description_from_db("read_reviews", db_path)

    @mcp.tool(name="read_reviews", description=desc_reviews)
    @tool_audit("read_reviews")
    def read_reviews(hotel_id: int, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Read the newest guest reviews first for a hotel.
        Returns {id, author, rating, text, date}.
        Review text is returned exactly as stored without filtering.
        """
        db = get_db_connection(db_path)
        try:
            rows = db.execute(
                """
                SELECT id, author, rating, text, date
                FROM reviews
                WHERE hotel_id = ?
                ORDER BY date DESC, id DESC
                LIMIT ?;
                """,
                (hotel_id, limit),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "author": row["author"],
                    "rating": row["rating"],
                    "text": row["text"],
                    "date": row["date"],
                }
                for row in rows
            ]
        finally:
            db.close()

    # 3. get_profile
    desc_profile = _get_description_from_db("get_profile", db_path)

    @mcp.tool(name="get_profile", description=desc_profile)
    @tool_audit("get_profile")
    def get_profile() -> Dict[str, Any]:
        """
        Retrieve the current user's profile information.
        Returns {name, email, phone, nationality, passport_number}.
        """
        db = get_db_connection(db_path)
        try:
            row = db.execute(
                "SELECT name, email, phone, nationality, passport_number FROM profile LIMIT 1;"
            ).fetchone()
            if not row:
                raise ValueError("No user profile found")
            return {
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"],
                "nationality": row["nationality"],
                "passport_number": row["passport_number"],
            }
        finally:
            db.close()

    # 4. send_message
    desc_msg = _get_description_from_db("send_message", db_path)

    @mcp.tool(name="send_message", description=desc_msg)
    @tool_audit("send_message")
    def send_message(to: str, subject: str, body: str) -> Dict[str, Any]:
        """
        Send a message to a hotel or contact.
        Inserts row into outbox table and NEVER sends real emails or makes external network calls.
        Returns {status: "sent", message_id: <int>}.
        """
        db = get_db_connection(db_path)
        try:
            with db:
                created_at = datetime.now(timezone.utc).isoformat()
                cursor = db.execute(
                    """
                    INSERT INTO outbox (to_address, subject, body, created_at)
                    VALUES (?, ?, ?, ?);
                    """,
                    (to, subject, body, created_at),
                )
                message_id = cursor.lastrowid
            return {"status": "sent", "message_id": message_id}
        finally:
            db.close()

    # 5. book_hotel
    desc_book = _get_description_from_db("book_hotel", db_path)

    @mcp.tool(name="book_hotel", description=desc_book)
    @tool_audit("book_hotel")
    def book_hotel(
        hotel_id: int, check_in: str, check_out: str, guest_name: str
    ) -> Dict[str, Any]:
        """
        Book a hotel reservation.
        Validates hotel exists and check_out is strictly after check_in.
        Returns confirmation id.
        """
        # Date validation
        try:
            # Supports YYYY-MM-DD format
            d_in = date.fromisoformat(check_in.strip())
            d_out = date.fromisoformat(check_out.strip())
        except Exception:
            raise ValueError(
                f"Invalid date format for check_in ({check_in}) or check_out ({check_out}). Expected YYYY-MM-DD."
            )

        if d_out <= d_in:
            raise ValueError(
                f"check_out date ({check_out}) must be after check_in date ({check_in})"
            )

        db = get_db_connection(db_path)
        try:
            with db:
                hotel = db.execute(
                    "SELECT id, name FROM hotels WHERE id = ?;", (hotel_id,)
                ).fetchone()
                if not hotel:
                    raise ValueError(f"Hotel with id {hotel_id} not found")

                confirmation_id = f"TB-{secrets.token_hex(4).upper()}"
                created_at = datetime.now(timezone.utc).isoformat()

                db.execute(
                    """
                    INSERT INTO bookings (hotel_id, check_in, check_out, guest_name, confirmation_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        hotel_id,
                        check_in.strip(),
                        check_out.strip(),
                        guest_name.strip(),
                        confirmation_id,
                        created_at,
                    ),
                )

            return {
                "status": "confirmed",
                "confirmation_id": confirmation_id,
                "hotel_id": hotel_id,
                "hotel_name": hotel["name"],
                "guest_name": guest_name,
                "check_in": check_in,
                "check_out": check_out,
            }
        finally:
            db.close()

    # Register Admin & Public Review HTTP routes
    register_admin_routes(
        mcp, db_path=db_path, sync_descriptions_cb=mcp.sync_tool_descriptions
    )

    # Initial sync
    mcp.sync_tool_descriptions()

    return mcp


# Server runner
def main() -> None:
    """Run the TripBuddy server on port 8000."""
    from starlette.middleware.cors import CORSMiddleware

    server_app = create_app()
    app = server_app.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    print("Starting TripBuddy Server on http://127.0.0.1:8000 (MCP endpoint: /mcp)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
