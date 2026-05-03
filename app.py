"""
Laundry Booking System — Backend (Flask + PostgreSQL)
======================================================
v3 — Adds:
  - Admin dashboard with password protection
  - GET  /admin/stats          → overview numbers
  - GET  /admin/bookings        → full booking history with filters
  - DELETE /admin/bookings/<id> → cancel a booking
  - PATCH /admin/machines/<id>  → toggle machine active/inactive
  - GET  /machines/<id>/next-slot → next available time slot
"""

import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Secret key is required for session (stores admin login state).
# On Railway, set this as an environment variable called SECRET_KEY.
# Falls back to a default for local development only.
app.secret_key = os.environ.get("SECRET_KEY", "laundry-dev-secret-change-in-production")

# Admin password — set ADMIN_PASSWORD environment variable on Railway.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

DATABASE_URL = os.environ.get("DATABASE_URL")

CYCLE_DURATIONS = {
    "Washer": 45,
    "Dryer":  60,
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    """Open a PostgreSQL connection with dict-like row access."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    """
    Create all tables and seed machines.
    v3 adds: is_active column to machines table.
    """
    conn = get_db_connection()
    cur  = conn.cursor()

    # machines — added is_active so admin can disable broken machines
    cur.execute("""
        CREATE TABLE IF NOT EXISTS machines (
            id        SERIAL  PRIMARY KEY,
            name      TEXT    NOT NULL,
            type      TEXT    NOT NULL CHECK(type IN ('Washer', 'Dryer')),
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    # bookings table — pin_hash stores bcrypt hash of the student's 4-digit PIN
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id           SERIAL    PRIMARY KEY,
            student_name TEXT      NOT NULL,
            room_number  TEXT      NOT NULL,
            machine_id   INTEGER   NOT NULL REFERENCES machines(id),
            start_time   TIMESTAMP NOT NULL,
            end_time     TIMESTAMP NOT NULL,
            status       TEXT      NOT NULL DEFAULT 'Active'
                             CHECK(status IN ('Active', 'Completed', 'Cancelled')),
            pin_hash     TEXT      NOT NULL DEFAULT ''
        )
    """)

    # Add pin_hash column if upgrading from an older version of the schema
    cur.execute("""
        ALTER TABLE bookings ADD COLUMN IF NOT EXISTS pin_hash TEXT NOT NULL DEFAULT ''
    """)

    # Add is_active column if upgrading from an older version of the schema
    cur.execute("""
        ALTER TABLE machines ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE
    """)

    # Add Cancelled to the status check if upgrading
    # (PostgreSQL doesn't support ALTER CHECK directly — we drop and re-add)
    cur.execute("""
        ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_status_check
    """)
    cur.execute("""
        ALTER TABLE bookings ADD CONSTRAINT bookings_status_check
            CHECK(status IN ('Active', 'Completed', 'Cancelled'))
    """)

    # Seed machines only if table is empty
    cur.execute("SELECT COUNT(*) FROM machines")
    if cur.fetchone()["count"] == 0:
        cur.executemany(
            "INSERT INTO machines (name, type) VALUES (%s, %s)",
            [
                ("Washer 1", "Washer"),
                ("Washer 2", "Washer"),
                ("Washer 3", "Washer"),
                ("Dryer 1",  "Dryer"),
                ("Dryer 2",  "Dryer"),
            ],
        )

    conn.commit()
    cur.close()
    conn.close()
    print("✅ PostgreSQL database initialised (v3).")


# ---------------------------------------------------------------------------
# Admin auth helper
# ---------------------------------------------------------------------------

def admin_required(f):
    """
    Decorator that protects admin routes.
    Redirects to /admin/login if the admin is not logged in.
    Usage: add @admin_required above any admin route function.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Business logic helpers
# ---------------------------------------------------------------------------

def expire_old_bookings():
    """
    Mark Active bookings whose end_time has passed as Completed.
    We compare end_time against NOW() AT TIME ZONE 'Africa/Johannesburg'
    so the comparison always uses SA local time regardless of where
    the server is running.
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE bookings SET status = 'Completed'
        WHERE  status = 'Active'
          AND  end_time <= (NOW() AT TIME ZONE 'Africa/Johannesburg')
    """)
    conn.commit()
    cur.close()
    conn.close()


def get_machine_queue(machine_id: int) -> dict:
    """
    Return the full booking queue for a machine — everyone who has
    an active booking going forward, ordered by start time.
    Also returns the overall status (Available or Busy).
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT id, student_name, room_number, start_time, end_time
        FROM   bookings
        WHERE  machine_id = %s AND status = 'Active'
          AND  end_time > (NOW() AT TIME ZONE 'Africa/Johannesburg')
        ORDER  BY start_time ASC
        """,
        (machine_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return {"status": "Available", "queue": []}

    # Build the queue list — each entry has id, name, room, start, end
    # The id is needed so students can cancel their specific booking
    queue = []
    for row in rows:
        queue.append({
            "booking_id":   row["id"],
            "student_name": row["student_name"],
            "room_number":  row["room_number"],
            "start_time":   row["start_time"].isoformat(),
            "end_time":     row["end_time"].isoformat(),
        })

    # The first entry in the queue is the person currently using the machine
    return {
        "status":     "Busy",
        "busy_until": rows[0]["end_time"].isoformat(),
        "booked_by":  f"{rows[0]['student_name']} (Room {rows[0]['room_number']})",
        "queue":      queue,
    }


def get_next_available_slot(machine_id: int, machine_type: str) -> datetime:
    """
    Find the earliest datetime this machine is free.
    Works by looking at all future Active bookings sorted by start time,
    then finding the first gap big enough for one cycle.

    Returns: a datetime object for the next free start time.
    """
    duration = timedelta(minutes=CYCLE_DURATIONS[machine_type])
    # Use SA local time (UTC+2) to match stored booking times
    now      = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)

    conn = get_db_connection()
    cur  = conn.cursor()
    # Get all future/current active bookings sorted by start time
    cur.execute(
        """
        SELECT start_time, end_time FROM bookings
        WHERE  machine_id = %s AND status = 'Active' AND end_time > %s
        ORDER  BY start_time ASC
        """,
        (machine_id, now),
    )
    bookings = cur.fetchall()
    cur.close()
    conn.close()

    # If there are no active bookings, the machine is free right now
    if not bookings:
        return now

    # Walk through bookings and look for a gap between consecutive slots
    # First check: can we fit a slot before the first booking starts?
    candidate = now
    for booking in bookings:
        b_start = booking["start_time"]
        b_end   = booking["end_time"]

        # If our candidate slot ends before this booking starts — it fits!
        if candidate + duration <= b_start:
            return candidate

        # Otherwise push candidate to after this booking ends
        if b_end > candidate:
            candidate = b_end

    # No gap found between bookings — next slot is after the last booking ends
    return candidate


def has_booking_conflict(machine_id: int, new_start: datetime, new_end: datetime) -> bool:
    """Return True if the slot overlaps any existing Active booking."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT id FROM bookings
        WHERE  machine_id = %s AND status = 'Active'
          AND  start_time < %s AND end_time > %s
        LIMIT  1
        """,
        (machine_id, new_end, new_start),
    )
    conflict = cur.fetchone()
    cur.close()
    conn.close()
    return conflict is not None


# ---------------------------------------------------------------------------
# Routes — Public Pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/book")
def book_page():
    return render_template("book.html")



@app.route("/cancel", methods=["POST"])
def cancel_booking():
    """
    POST /cancel
    Allows a student to cancel their own booking by providing their
    name, room number, and the booking id shown in their queue slot.

    After cancelling, all subsequent bookings on the same machine are
    shifted earlier to fill the gap so no time is wasted.

    Expected JSON body:
        { "booking_id": 5, "student_name": "Thabo Nkosi", "room_number": "204" }
    """
    expire_old_bookings()
    data = request.get_json()

    required = ["booking_id", "student_name", "room_number"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    booking_id   = int(data["booking_id"])
    student_name = data["student_name"].strip().lower()
    room_number  = str(data["room_number"]).strip()

    conn = get_db_connection()
    cur  = conn.cursor()

    # ---- 1. Find the booking and verify it belongs to this student ----
    cur.execute(
        """
        SELECT b.*, m.type AS machine_type
        FROM   bookings b JOIN machines m ON b.machine_id = m.id
        WHERE  b.id = %s AND b.status = 'Active'
        """,
        (booking_id,),
    )
    booking = cur.fetchone()

    if booking is None:
        cur.close()
        conn.close()
        return jsonify({"error": "Booking not found or already cancelled."}), 404

    # Case-insensitive name check and exact room check for security
    if (booking["student_name"].lower() != student_name or
            booking["room_number"] != room_number):
        cur.close()
        conn.close()
        return jsonify({"error": "Name or room number does not match this booking. Please check and try again."}), 403

    machine_id      = booking["machine_id"]
    cancelled_start = booking["start_time"]
    cancelled_end   = booking["end_time"]

    # ---- 2. Cancel the booking ----
    cur.execute(
        "UPDATE bookings SET status = %s WHERE id = %s",
        ("Cancelled", booking_id),
    )

    # ---- 3. Shift all subsequent bookings forward to fill the gap ----
    # Fetch every active booking on this machine that starts at or after
    # the cancelled slot start, ordered by start time.
    cur.execute(
        """
        SELECT id, start_time, end_time
        FROM   bookings
        WHERE  machine_id = %s AND status = 'Active'
          AND  start_time >= %s
        ORDER  BY start_time ASC
        """,
        (machine_id, cancelled_start),
    )
    subsequent = cur.fetchall()

    # Walk through each booking and close the gap left by the cancellation.
    # Each booking keeps its own duration but starts where the previous one ended.
    next_start = cancelled_start
    for b in subsequent:
        b_duration = b["end_time"] - b["start_time"]
        new_start  = next_start
        new_end    = new_start + b_duration
        cur.execute(
            "UPDATE bookings SET start_time = %s, end_time = %s WHERE id = %s",
            (new_start, new_end, b["id"]),
        )
        next_start = new_end

    conn.commit()
    cur.close()
    conn.close()

    shifted_count = len(subsequent)
    return jsonify({
        "message":       "Your booking has been cancelled successfully.",
        "shifted_count": shifted_count,
        "note":          (
            f"{shifted_count} booking(s) were shifted earlier to fill the gap."
            if shifted_count else
            "No other bookings needed shifting."
        ),
    })

# ---------------------------------------------------------------------------
# Routes — Admin Pages
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Show login form (GET) or process login (POST)."""
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Incorrect password. Try again."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    """Clear the admin session and redirect to login."""
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    """Serve the admin dashboard page."""
    return render_template("admin.html")


# ---------------------------------------------------------------------------
# Routes — Public REST API
# ---------------------------------------------------------------------------

@app.route("/machines", methods=["GET"])
def get_machines():
    """GET /machines — all active machines with current status and next slot."""
    expire_old_bookings()
    # Use SA local time (UTC+2) to match stored booking times
    now  = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    conn = get_db_connection()
    cur  = conn.cursor()
    # Only show active machines to students
    cur.execute("SELECT * FROM machines WHERE is_active = TRUE ORDER BY type, name")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    machines = []
    for row in rows:
        machine = dict(row)
        machine["duration_minutes"] = CYCLE_DURATIONS[row["type"]]
        # Get full queue (includes status, busy_until, booked_by, queue list)
        machine.update(get_machine_queue(row["id"]))
        # Calculate next available slot so the frontend can show it
        next_slot = get_next_available_slot(row["id"], row["type"])
        machine["next_available"] = next_slot.isoformat()
        machines.append(machine)

    return jsonify(machines)


@app.route("/machines/<int:machine_id>/next-slot", methods=["GET"])
def get_next_slot(machine_id):
    """GET /machines/<id>/next-slot — returns next free start time for one machine."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM machines WHERE id = %s", (machine_id,))
    machine = cur.fetchone()
    cur.close()
    conn.close()

    if machine is None:
        return jsonify({"error": "Machine not found"}), 404

    next_slot = get_next_available_slot(machine_id, machine["type"])
    return jsonify({
        "machine_id":     machine_id,
        "machine_name":   machine["name"],
        "next_available": next_slot.isoformat(),
    })


@app.route("/bookings", methods=["GET"])
def get_bookings():
    """GET /bookings — all bookings most recent first."""
    expire_old_bookings()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT b.id, b.student_name, b.room_number,
               b.start_time, b.end_time, b.status,
               m.name AS machine_name, m.type AS machine_type
        FROM   bookings b JOIN machines m ON b.machine_id = m.id
        ORDER  BY b.start_time DESC
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    bookings = []
    for row in rows:
        b = dict(row)
        b["start_time"] = b["start_time"].isoformat()
        b["end_time"]   = b["end_time"].isoformat()
        bookings.append(b)
    return jsonify(bookings)


@app.route("/book", methods=["POST"])
def create_booking():
    """POST /book — validate, conflict-check, and create a booking."""
    expire_old_bookings()
    data = request.get_json()

    required = ["student_name", "room_number", "machine_id", "start_time", "pin"]
    missing  = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    student_name = data["student_name"].strip()
    room_number  = str(data["room_number"]).strip()
    machine_id   = data["machine_id"]

    # Validate PIN — must be exactly 4 digits
    pin = str(data["pin"]).strip()
    if not pin.isdigit() or len(pin) != 4:
        return jsonify({"error": "PIN must be exactly 4 digits (e.g. 1234)."}), 400

    # Hash the PIN using bcrypt — we never store the plain PIN
    pin_hash = bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        # Save the student's local time (SA time) directly.
        # All comparisons in the DB use Africa/Johannesburg timezone
        # so everything stays consistent without any conversion.
        start_time = datetime.fromisoformat(data["start_time"])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid start_time format. Use ISO-8601."}), 400

    # Compare against SA local time — matches how we store and query
    sa_now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None) + timedelta(hours=2)
    if start_time < sa_now:
        return jsonify({"error": "Start time must be in the future."}), 400

    conn    = get_db_connection()
    cur     = conn.cursor()
    cur.execute("SELECT * FROM machines WHERE id = %s AND is_active = TRUE", (machine_id,))
    machine = cur.fetchone()
    cur.close()
    conn.close()

    if machine is None:
        return jsonify({"error": "Machine not found or is currently out of service."}), 404

    # num_loads lets students book multiple consecutive loads (1, 2, or 3)
    # Default is 1 if not provided (backwards compatible)
    num_loads = int(data.get("num_loads", 1))
    if num_loads not in (1, 2, 3):
        return jsonify({"error": "num_loads must be 1, 2, or 3."}), 400

    duration = timedelta(minutes=CYCLE_DURATIONS[machine["type"]] * num_loads)
    end_time = start_time + duration

    if has_booking_conflict(machine_id, start_time, end_time):
        # Find the next available slot and include it in the error response
        next_slot = get_next_available_slot(machine_id, machine["type"])
        return jsonify({
            "error":          f"{machine['name']} is already booked during that time.",
            "next_available": next_slot.isoformat(),
        }), 409

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute(
        """
        INSERT INTO bookings (student_name, room_number, machine_id, start_time, end_time, status, pin_hash)
        VALUES (%s, %s, %s, %s, %s, 'Active', %s) RETURNING id
        """,
        (student_name, room_number, machine_id, start_time, end_time, pin_hash),
    )
    booking_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "message":    "Booking confirmed!",
        "booking_id": booking_id,
        "machine":    machine["name"],
        "start_time": start_time.isoformat(),
        "end_time":   end_time.isoformat(),
        "note":       "Keep your 4-digit PIN safe — you will need it to cancel this booking.",
    }), 201



@app.route("/cancel", methods=["POST"])
def cancel_booking():
    """
    POST /cancel
    Allows a student to cancel their own booking by verifying:
      - Full name (case-insensitive)
      - Room number (exact match)
      - 4-digit PIN (checked against bcrypt hash)

    On success, all subsequent bookings on the same machine are
    shifted earlier to fill the gap so no time is wasted.

    Expected JSON body:
        {
            "booking_id":   5,
            "student_name": "Thabo Nkosi",
            "room_number":  "204",
            "pin":          "7391"
        }
    """
    expire_old_bookings()
    data = request.get_json()

    required = ["booking_id", "student_name", "room_number", "pin"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    booking_id   = int(data["booking_id"])
    student_name = data["student_name"].strip().lower()
    room_number  = str(data["room_number"]).strip()
    pin          = str(data["pin"]).strip()

    conn = get_db_connection()
    cur  = conn.cursor()

    # ---- 1. Find the booking ----
    cur.execute(
        """
        SELECT b.*, m.type AS machine_type
        FROM   bookings b JOIN machines m ON b.machine_id = m.id
        WHERE  b.id = %s AND b.status = 'Active'
        """,
        (booking_id,),
    )
    booking = cur.fetchone()

    if booking is None:
        cur.close()
        conn.close()
        return jsonify({"error": "Booking not found or already cancelled."}), 404

    # ---- 2. Verify name matches (case-insensitive) ----
    if booking["student_name"].lower() != student_name:
        cur.close()
        conn.close()
        return jsonify({"error": "Name does not match this booking. Please check and try again."}), 403

    # ---- 3. Verify room number matches ----
    if booking["room_number"] != room_number:
        cur.close()
        conn.close()
        return jsonify({"error": "Room number does not match this booking. Please check and try again."}), 403

    # ---- 4. Verify PIN using bcrypt ----
    stored_hash = booking["pin_hash"]
    if not stored_hash:
        # Old booking created before PIN feature — cannot verify
        cur.close()
        conn.close()
        return jsonify({"error": "This booking has no PIN set. Please ask the admin to cancel it."}), 400

    pin_correct = bcrypt.checkpw(pin.encode("utf-8"), stored_hash.encode("utf-8"))
    if not pin_correct:
        cur.close()
        conn.close()
        return jsonify({"error": "Incorrect PIN. Please try again."}), 403

    # ---- 5. All checks passed — cancel the booking ----
    machine_id      = booking["machine_id"]
    cancelled_start = booking["start_time"]

    cur.execute(
        "UPDATE bookings SET status = 'Cancelled' WHERE id = %s",
        (booking_id,),
    )

    # ---- 6. Shift subsequent bookings forward to close the gap ----
    cur.execute(
        """
        SELECT id, start_time, end_time
        FROM   bookings
        WHERE  machine_id = %s AND status = 'Active'
          AND  start_time >= %s
        ORDER  BY start_time ASC
        """,
        (machine_id, cancelled_start),
    )
    subsequent = cur.fetchall()

    next_start = cancelled_start
    for b in subsequent:
        b_duration = b["end_time"] - b["start_time"]
        new_start  = next_start
        new_end    = new_start + b_duration
        cur.execute(
            "UPDATE bookings SET start_time = %s, end_time = %s WHERE id = %s",
            (new_start, new_end, b["id"]),
        )
        next_start = new_end

    conn.commit()
    cur.close()
    conn.close()

    shifted_count = len(subsequent)
    return jsonify({
        "message": "Your booking has been cancelled successfully.",
        "note":    (
            f"{shifted_count} booking(s) were shifted earlier to fill the gap."
            if shifted_count else
            "No other bookings needed shifting."
        ),
    })

# ---------------------------------------------------------------------------
# Routes — Admin REST API (all protected by @admin_required)
# ---------------------------------------------------------------------------

@app.route("/admin/stats", methods=["GET"])
@admin_required
def admin_stats():
    """
    GET /admin/stats
    Returns overview numbers for the dashboard header cards.
    """
    expire_old_bookings()
    conn = get_db_connection()
    cur  = conn.cursor()

    # Total bookings today — using SA timezone
    cur.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE  start_time::date = (NOW() AT TIME ZONE 'Africa/Johannesburg')::date
    """)
    bookings_today = cur.fetchone()["count"]

    # Currently active (busy right now) — using SA timezone
    cur.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE  status = 'Active'
          AND  start_time <= (NOW() AT TIME ZONE 'Africa/Johannesburg')
          AND  end_time   >  (NOW() AT TIME ZONE 'Africa/Johannesburg')
    """)
    active_now = cur.fetchone()["count"]

    # Total machines and how many are active
    cur.execute("SELECT COUNT(*) FROM machines")
    total_machines = cur.fetchone()["count"]

    cur.execute("SELECT COUNT(*) FROM machines WHERE is_active = TRUE")
    active_machines = cur.fetchone()["count"]

    # Most booked machine this week
    cur.execute("""
        SELECT m.name, COUNT(b.id) AS total
        FROM   bookings b JOIN machines m ON b.machine_id = m.id
        WHERE  b.start_time >= (NOW() AT TIME ZONE 'Africa/Johannesburg') - INTERVAL '7 days'
        GROUP  BY m.name ORDER BY total DESC LIMIT 1
    """)
    top_row = cur.fetchone()
    top_machine = top_row["name"] if top_row else "N/A"

    # Upcoming bookings (next 24 hours)
    cur.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE  status = 'Active'
          AND  start_time > (NOW() AT TIME ZONE 'Africa/Johannesburg')
          AND  start_time <= (NOW() AT TIME ZONE 'Africa/Johannesburg') + INTERVAL '24 hours'
    """)
    upcoming = cur.fetchone()["count"]

    cur.close()
    conn.close()

    return jsonify({
        "bookings_today":  bookings_today,
        "active_now":      active_now,
        "total_machines":  total_machines,
        "active_machines": active_machines,
        "top_machine":     top_machine,
        "upcoming_24h":    upcoming,
    })


@app.route("/admin/bookings", methods=["GET"])
@admin_required
def admin_get_bookings():
    """
    GET /admin/bookings
    Returns all bookings with optional ?status= filter.
    e.g. /admin/bookings?status=Active
    """
    expire_old_bookings()

    status_filter = request.args.get("status")  # optional query param

    conn = get_db_connection()
    cur  = conn.cursor()

    if status_filter and status_filter in ("Active", "Completed", "Cancelled"):
        cur.execute(
            """
            SELECT b.id, b.student_name, b.room_number,
                   b.start_time, b.end_time, b.status,
                   m.name AS machine_name, m.type AS machine_type
            FROM   bookings b JOIN machines m ON b.machine_id = m.id
            WHERE  b.status = %s
            ORDER  BY b.start_time DESC
            """,
            (status_filter,),
        )
    else:
        cur.execute(
            """
            SELECT b.id, b.student_name, b.room_number,
                   b.start_time, b.end_time, b.status,
                   m.name AS machine_name, m.type AS machine_type
            FROM   bookings b JOIN machines m ON b.machine_id = m.id
            ORDER  BY b.start_time DESC
            """
        )

    rows = cur.fetchall()
    cur.close()
    conn.close()

    bookings = []
    for row in rows:
        b = dict(row)
        b["start_time"] = b["start_time"].isoformat()
        b["end_time"]   = b["end_time"].isoformat()
        bookings.append(b)

    return jsonify(bookings)


@app.route("/admin/bookings/<int:booking_id>", methods=["DELETE"])
@admin_required
def admin_cancel_booking(booking_id):
    """
    DELETE /admin/bookings/<id>
    Cancels an Active booking. Sets status to 'Cancelled' rather than
    deleting the row so there is always a full audit trail.
    """
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,))
    booking = cur.fetchone()

    if booking is None:
        cur.close()
        conn.close()
        return jsonify({"error": "Booking not found."}), 404

    if booking["status"] != "Active":
        cur.close()
        conn.close()
        return jsonify({"error": f"Cannot cancel a booking with status '{booking['status']}'."}), 400

    cur.execute(
        "UPDATE bookings SET status = 'Cancelled' WHERE id = %s",
        (booking_id,),
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": f"Booking #{booking_id} has been cancelled."})


@app.route("/admin/machines", methods=["GET"])
@admin_required
def admin_get_machines():
    """GET /admin/machines — all machines including inactive ones."""
    expire_old_bookings()
    now  = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM machines ORDER BY type, name")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    machines = []
    for row in rows:
        machine = dict(row)
        machine["duration_minutes"] = CYCLE_DURATIONS[row["type"]]
        if row["is_active"]:
            machine.update(get_machine_queue(row["id"]))
        else:
            machine["status"] = "Out of Service"
            machine["queue"]  = []
        machines.append(machine)
    return jsonify(machines)


@app.route("/admin/machines/<int:machine_id>", methods=["PATCH"])
@admin_required
def admin_toggle_machine(machine_id):
    """
    PATCH /admin/machines/<id>
    Toggles a machine between active and inactive (out of service).
    Body: { "is_active": true } or { "is_active": false }
    """
    data      = request.get_json()
    is_active = data.get("is_active")

    if is_active is None or not isinstance(is_active, bool):
        return jsonify({"error": "Body must include is_active: true or false"}), 400

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT id FROM machines WHERE id = %s", (machine_id,))
    if cur.fetchone() is None:
        cur.close()
        conn.close()
        return jsonify({"error": "Machine not found."}), 404

    cur.execute(
        "UPDATE machines SET is_active = %s WHERE id = %s",
        (is_active, machine_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    status_word = "activated" if is_active else "marked as out of service"
    return jsonify({"message": f"Machine #{machine_id} has been {status_word}."})



@app.route("/admin/machines", methods=["POST"])
@admin_required
def admin_add_machine():
    """
    POST /admin/machines
    Add a brand new machine to the laundry room.
    Body: { "name": "Washer 4", "type": "Washer" }
    """
    data  = request.get_json()
    name  = str(data.get("name", "")).strip()
    mtype = str(data.get("type", "")).strip()

    if not name:
        return jsonify({"error": "Machine name is required."}), 400
    if mtype not in ("Washer", "Dryer"):
        return jsonify({"error": "Type must be Washer or Dryer."}), 400

    conn = get_db_connection()
    cur  = conn.cursor()

    # Prevent duplicate names
    cur.execute("SELECT id FROM machines WHERE LOWER(name) = LOWER(%s)", (name,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify({"error": f"A machine named '{name}' already exists."}), 409

    cur.execute(
        "INSERT INTO machines (name, type, is_active) VALUES (%s, %s, TRUE) RETURNING id",
        (name, mtype),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "message": f"{name} ({mtype}) added successfully.",
        "id":      new_id,
    }), 201


@app.route("/admin/machines/<int:machine_id>", methods=["DELETE"])
@admin_required
def admin_delete_machine(machine_id):
    """
    DELETE /admin/machines/<id>
    Permanently remove a machine from the system.
    Blocked if the machine has active bookings to protect students.
    """
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("SELECT * FROM machines WHERE id = %s", (machine_id,))
    machine = cur.fetchone()
    if machine is None:
        cur.close()
        conn.close()
        return jsonify({"error": "Machine not found."}), 404

    # Check for active bookings before allowing deletion
    cur.execute(
        "SELECT COUNT(*) FROM bookings WHERE machine_id = %s AND status = 'Active'",
        (machine_id,),
    )
    active_count = cur.fetchone()["count"]
    if active_count > 0:
        cur.close()
        conn.close()
        return jsonify({
            "error": (
                f"Cannot delete {machine['name']} — it has {active_count} active booking(s). "
                f"Cancel them first or wait for them to complete."
            )
        }), 400

    cur.execute("DELETE FROM machines WHERE id = %s", (machine_id,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": f"{machine['name']} has been permanently removed."})


@app.route("/admin/bookings/clear-history", methods=["DELETE"])
@admin_required
def admin_clear_history():
    """
    DELETE /admin/bookings/clear-history
    Permanently delete past bookings (Completed and/or Cancelled).
    Active bookings are NEVER touched.
    Optional ?status= param to clear only one type:
        /admin/bookings/clear-history?status=Completed
        /admin/bookings/clear-history?status=Cancelled
    Without ?status= it clears both Completed and Cancelled.
    """
    status_filter = request.args.get("status")

    conn = get_db_connection()
    cur  = conn.cursor()

    if status_filter and status_filter in ("Completed", "Cancelled"):
        cur.execute(
            "DELETE FROM bookings WHERE status = %s RETURNING id",
            (status_filter,),
        )
    else:
        # Clear both completed and cancelled
        cur.execute(
            "DELETE FROM bookings WHERE status IN ('Completed', 'Cancelled') RETURNING id"
        )

    deleted_count = len(cur.fetchall())
    conn.commit()
    cur.close()
    conn.close()

    label = status_filter if status_filter else "Completed and Cancelled"
    return jsonify({
        "message": f"Cleared {deleted_count} {label} booking(s) from history.",
        "deleted": deleted_count,
    })

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
