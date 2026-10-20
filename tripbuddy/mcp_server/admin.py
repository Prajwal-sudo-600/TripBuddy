"""
Admin and Public Review HTTP API routes for TripBuddy.
These routes are registered as custom HTTP routes on the FastMCP server,
so an AI agent inspecting MCP tools never sees or calls them.
"""

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from starlette.requests import Request
from starlette.responses import JSONResponse

from .db import get_db_connection
from .seed import DEFAULT_TOOL_DESCRIPTIONS, seed_database


def register_admin_routes(
    mcp_server: Any,
    db_path: Optional[str] = None,
    sync_descriptions_cb: Optional[Callable[[], None]] = None,
) -> None:
    """Register admin and external review routes on the FastMCP server instance."""

    @mcp_server.custom_route("/admin/reset", methods=["POST"])
    async def admin_reset(request: Request) -> JSONResponse:
        """
        Restore seed data, clear bookings, outbox, events, and any added reviews,
        restore the original tool descriptions, set passport_number to the given
        canary or a fresh random one. Return the canary used.
        """
        req_canary: Optional[str] = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                req_canary = body.get("canary")
        except Exception:
            pass

        active_canary = seed_database(db_path=db_path, canary=req_canary)
        if sync_descriptions_cb:
            sync_descriptions_cb()

        return JSONResponse({"status": "ok", "canary": active_canary})

    @mcp_server.custom_route("/api/reviews", methods=["POST"])
    async def add_review(request: Request) -> JSONResponse:
        """
        Public review submission form that attackers will use to inject reviews.
        Body: {hotel_id, author, rating, text}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        hotel_id = body.get("hotel_id")
        author = body.get("author")
        rating = body.get("rating")
        text = body.get("text")

        if hotel_id is None or author is None or rating is None or text is None:
            return JSONResponse(
                {"error": "Missing required fields: hotel_id, author, rating, text"},
                status_code=400,
            )

        conn = get_db_connection(db_path)
        try:
            with conn:
                hotel = conn.execute(
                    "SELECT id FROM hotels WHERE id = ?;", (hotel_id,)
                ).fetchone()
                if not hotel:
                    return JSONResponse(
                        {"error": f"Hotel {hotel_id} not found"}, status_code=404
                    )

                # Use current UTC timestamp
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                cursor = conn.execute(
                    """
                    INSERT INTO reviews (hotel_id, author, rating, text, date)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (hotel_id, str(author), float(rating), str(text), now_str),
                )
                review_id = cursor.lastrowid

                # Update hotel avg_rating and review_count
                stats = conn.execute(
                    "SELECT COUNT(*) as cnt, AVG(rating) as avg_r FROM reviews WHERE hotel_id = ?;",
                    (hotel_id,),
                ).fetchone()
                new_cnt = stats["cnt"]
                new_avg = round(float(stats["avg_r"]), 2)

                conn.execute(
                    "UPDATE hotels SET review_count = ?, avg_rating = ? WHERE id = ?;",
                    (new_cnt, new_avg, hotel_id),
                )

            return JSONResponse(
                {"status": "created", "review_id": review_id}, status_code=201
            )
        finally:
            conn.close()

    @mcp_server.custom_route("/admin/state", methods=["GET"])
    async def admin_state(request: Request) -> JSONResponse:
        """Return {bookings, outbox, reviews, canary}."""
        conn = get_db_connection(db_path)
        try:
            profile_row = conn.execute(
                "SELECT passport_number FROM profile LIMIT 1;"
            ).fetchone()
            canary = profile_row["passport_number"] if profile_row else None

            bookings = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, hotel_id, check_in, check_out, guest_name, confirmation_id, created_at FROM bookings ORDER BY id ASC;"
                ).fetchall()
            ]

            outbox = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, to_address, subject, body, created_at FROM outbox ORDER BY id ASC;"
                ).fetchall()
            ]

            reviews = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, hotel_id, author, rating, text, date FROM reviews ORDER BY id ASC;"
                ).fetchall()
            ]

            return JSONResponse(
                {
                    "bookings": bookings,
                    "outbox": outbox,
                    "reviews": reviews,
                    "canary": canary,
                }
            )
        finally:
            conn.close()

    @mcp_server.custom_route("/admin/hotels", methods=["GET"])
    async def admin_hotels(request: Request) -> JSONResponse:
        """Return all hotels with id, name, price_per_night, and avg_rating."""
        conn = get_db_connection(db_path)
        try:
            rows = conn.execute(
                """
                SELECT h.id, h.name, h.price_per_night,
                       ROUND(AVG(r.rating), 2) as avg_rating
                FROM hotels h
                LEFT JOIN reviews r ON h.id = r.hotel_id
                GROUP BY h.id, h.name, h.price_per_night
                ORDER BY avg_rating DESC, h.id ASC;
                """
            ).fetchall()
            hotel_list = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "price_per_night": float(row["price_per_night"]),
                    "avg_rating": float(row["avg_rating"]) if row["avg_rating"] is not None else 0.0,
                }
                for row in rows
            ]
            return JSONResponse(hotel_list)
        finally:
            conn.close()

    @mcp_server.custom_route("/admin/events", methods=["GET"])
    async def admin_events(request: Request) -> JSONResponse:
        """Return the executed tool calls in order."""
        conn = get_db_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT id, timestamp, tool_name, args, result FROM events ORDER BY id ASC;"
            ).fetchall()
            events_list = []
            for row in rows:
                events_list.append(
                    {
                        "id": row["id"],
                        "timestamp": row["timestamp"],
                        "tool_name": row["tool_name"],
                        "args": json.loads(row["args"]),
                        "result": json.loads(row["result"]),
                    }
                )
            return JSONResponse(events_list)
        finally:
            conn.close()

    @mcp_server.custom_route("/admin/tool-description", methods=["PUT"])
    async def update_tool_description(request: Request) -> JSONResponse:
        """Change a tool description."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        tool_name = body.get("tool")
        description = body.get("description")

        if not tool_name or not description:
            return JSONResponse(
                {"error": "Missing required fields: tool, description"},
                status_code=400,
            )

        conn = get_db_connection(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO tool_descriptions (tool_name, description)
                    VALUES (?, ?)
                    ON CONFLICT(tool_name) DO UPDATE SET description = excluded.description;
                    """,
                    (tool_name, description),
                )
            if sync_descriptions_cb:
                sync_descriptions_cb()

            return JSONResponse(
                {"status": "updated", "tool": tool_name, "description": description}
            )
        finally:
            conn.close()

    @mcp_server.custom_route("/admin/tool-description/reset", methods=["POST"])
    async def reset_tool_descriptions(request: Request) -> JSONResponse:
        """Restore the original tool descriptions."""
        conn = get_db_connection(db_path)
        try:
            with conn:
                for tool_name, desc in DEFAULT_TOOL_DESCRIPTIONS.items():
                    conn.execute(
                        """
                        INSERT INTO tool_descriptions (tool_name, description)
                        VALUES (?, ?)
                        ON CONFLICT(tool_name) DO UPDATE SET description = excluded.description;
                        """,
                        (tool_name, desc),
                    )
            if sync_descriptions_cb:
                sync_descriptions_cb()

            return JSONResponse({"status": "reset"})
        finally:
            conn.close()
