"""
Laundry Booking System — Backend (Flask)
=========================================
A REST API that handles machine listings, booking creation,
conflict detection, and status computation.
"""

import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Path to the SQLite database file (sits next to app.py)
DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

# Cycle durations (in minutes) for each machine type
CYCLE_DURATIONS = {
    "Washer": 45,
    "Dryer": 60,
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    """Open a new database connection and configure it to return dict-like rows."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # lets us access columns by name
    conn.execute("PRAGMA foreign_keys = ON")  # enforce foreign-key constraints
    return conn


def init_db():
    """
    Create tables and seed the machines table with initial data.
    Safe to call multiple times — uses IF NOT EXISTS / INSERT OR IGNORE.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # ---- machines table ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS machines (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL,
            type TEXT    NOT NULL CHECK(type IN ('Washer', 'Dryer'))
        )
    """)

    # ---- bookings table ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            student_name TEXT    NOT NULL,
            room_number  TEXT    NOT NULL,
            machine_id   INTEGER NOT NULL REFERENCES machines(id),
            start_time   TEXT    NOT NULL,   -- ISO-8601 datetime string
            end_time     TEXT    NOT NULL,   -- ISO-8601 datetime string
            status       TEXT    NOT NULL DEFAULT 'Active'
                             CHECK(status IN ('Active', 'Completed'))
        )
    """)

    # ---- seed machines (only if the table is empty) ----
    cur.execute("SELECT COUNT(*) FROM machines")
    if cur.fetchone()[0] == 0:
        machines_seed = [
            ("Washer 1", "Washer"),
            ("Washer 2", "Washer"),
            ("Washer 3", "Washer"),
            ("Dryer 1",  "Dryer"),
            ("Dryer 2",  "Dryer"),
        ]
        cur.executemany(
            "INSERT INTO machines (name, type) VALUES (?, ?)",
            machines_seed,
        )

    conn.commit()
    conn.close()
    print("✅ Database initialised.")


# ---------------------------------------------------------------------------
# Business logic helpers
# ---------------------------------------------------------------------------

def compute_machine_status(machine_id: int, now: datetime) -> dict:
    """
    Look up the current Active booking for a machine (if any) and return a
    status dictionary.

    Returns:
        { "status": "Available" }
        OR
        { "status": "Busy", "busy_until": "<ISO string>", "booked_by": "..." }
    """
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT student_name, room_number, end_time
        FROM   bookings
        WHERE  machine_id = ?
          AND  status     = 'Active'
          AND  end_time   > ?
        ORDER  BY end_time ASC
        LIMIT  1
        """,
        (machine_id, now.isoformat()),
    ).fetchone()
    conn.close()

    if row is None:
        return {"status": "Available"}

    return {
        "status":     "Busy",
        "busy_until": row["end_time"],
        "booked_by":  f"{row['student_name']} (Room {row['room_number']})",
    }


def has_booking_conflict(machine_id: int, new_start: datetime, new_end: datetime) -> bool:
    """
    Returns True if the requested time slot overlaps with any existing Active
    booking for the machine.

    Overlap condition (standard interval overlap):
        new_start < existing_end  AND  new_end > existing_start
    """
    conn = get_db_connection()
    conflict = conn.execute(
        """
        SELECT id FROM bookings
        WHERE  machine_id = ?
          AND  status     = 'Active'
          AND  start_time < ?    -- existing starts before new ends
          AND  end_time   > ?    -- existing ends   after new starts
        LIMIT  1
        """,
        (machine_id, new_end.isoformat(), new_start.isoformat()),
    ).fetchone()
    conn.close()

    return conflict is not None


def expire_old_bookings():
    """
    Mark bookings as Completed when their end_time has passed.
    Called at the start of each request so statuses stay accurate.
    """
    now = datetime.now().isoformat()
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE bookings
        SET    status = 'Completed'
        WHERE  status = 'Active'
          AND  end_time <= ?
        """,
        (now,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    """Serve the main machine-status page."""
    return render_template("index.html")


@app.route("/book")
def book_page():
    """Serve the booking form page."""
    return render_template("book.html")


# ---------------------------------------------------------------------------
# Routes — REST API
# ---------------------------------------------------------------------------

@app.route("/machines", methods=["GET"])
def get_machines():
    """
    GET /machines
    Returns every machine with its current status and cycle duration.
    """
    expire_old_bookings()   # keep statuses fresh

    now = datetime.now()
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM machines ORDER BY type, name").fetchall()
    conn.close()

    machines = []
    for row in rows:
        machine = dict(row)
        machine["duration_minutes"] = CYCLE_DURATIONS[row["type"]]
        machine.update(compute_machine_status(row["id"], now))
        machines.append(machine)

    return jsonify(machines)


@app.route("/bookings", methods=["GET"])
def get_bookings():
    """
    GET /bookings
    Returns all bookings (most recent first) joined with machine names.
    """
    expire_old_bookings()

    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT b.id, b.student_name, b.room_number, b.start_time,
               b.end_time, b.status, m.name AS machine_name, m.type AS machine_type
        FROM   bookings b
        JOIN   machines m ON b.machine_id = m.id
        ORDER  BY b.start_time DESC
        """
    ).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])


@app.route("/book", methods=["POST"])
def create_booking():
    """
    POST /book
    Expected JSON body:
        {
            "student_name": "Alice Smith",
            "room_number":  "204",
            "machine_id":   1,
            "start_time":   "2025-06-15T09:00"
        }

    Validates input, checks for conflicts, and inserts the booking.
    """
    expire_old_bookings()

    data = request.get_json()

    # ---- 1. Validate that required fields are present ----
    required = ["student_name", "room_number", "machine_id", "start_time"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    student_name = data["student_name"].strip()
    room_number  = str(data["room_number"]).strip()
    machine_id   = data["machine_id"]

    # ---- 2. Parse and validate start_time ----
    try:
        start_time = datetime.fromisoformat(data["start_time"])
    except ValueError:
        return jsonify({"error": "Invalid start_time format. Use ISO-8601 (e.g. 2025-06-15T09:00)"}), 400

    # Bookings must be in the future
    if start_time < datetime.now():
        return jsonify({"error": "Start time must be in the future."}), 400

    # ---- 3. Look up the machine to get its type ----
    conn = get_db_connection()
    machine = conn.execute(
        "SELECT * FROM machines WHERE id = ?", (machine_id,)
    ).fetchone()
    conn.close()

    if machine is None:
        return jsonify({"error": f"Machine with id={machine_id} does not exist."}), 404

    # ---- 4. Calculate end_time based on machine type ----
    duration    = timedelta(minutes=CYCLE_DURATIONS[machine["type"]])
    end_time    = start_time + duration

    # ---- 5. Check for booking conflicts ----
    if has_booking_conflict(machine_id, start_time, end_time):
        return jsonify({
            "error": (
                f"{machine['name']} is already booked during that time. "
                f"Please choose a different time slot."
            )
        }), 409   # HTTP 409 Conflict

    # ---- 6. Insert the booking ----
    conn = get_db_connection()
    cur  = conn.execute(
        """
        INSERT INTO bookings
            (student_name, room_number, machine_id, start_time, end_time, status)
        VALUES (?, ?, ?, ?, ?, 'Active')
        """,
        (student_name, room_number, machine_id,
         start_time.isoformat(), end_time.isoformat()),
    )
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "message":    "Booking confirmed!",
        "booking_id": booking_id,
        "machine":    machine["name"],
        "start_time": start_time.isoformat(),
        "end_time":   end_time.isoformat(),
    }), 201   # HTTP 201 Created


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()                        # create tables + seed data if needed

    # Railway injects a PORT environment variable — we read it here.
    # If running locally, it falls back to port 5000.
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
