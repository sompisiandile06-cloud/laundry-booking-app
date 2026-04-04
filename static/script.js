/**
 * ResidenceLaundry — Frontend JavaScript
 * =========================================
 * Handles:
 *   - Fetching machine data from the API and rendering cards (index.html)
 *   - Populating the machine dropdown on the booking form (book.html)
 *   - Submitting bookings via the REST API (book.html)
 *   - Filter tabs by machine type
 *   - Auto-refresh every 30 seconds
 */

"use strict"; // Catch common mistakes early

// ---------------------------------------------------------------------------
// Shared constants
// ---------------------------------------------------------------------------

const API_BASE = "";  // empty = same origin (Flask serves both API and HTML)

// Machine duration in minutes — mirrors the backend CYCLE_DURATIONS
const CYCLE_DURATIONS = {
    Washer: 45,
    Dryer:  60,
};

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

/**
 * Format an ISO datetime string into a human-friendly time string.
 * e.g. "2025-06-15T09:45:00" → "09:45 AM"
 */
function formatTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Format an ISO datetime string into date + time.
 * e.g. "2025-06-15T09:45:00" → "15 Jun, 09:45 AM"
 */
function formatDateTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleString([], {
        day:    "2-digit",
        month:  "short",
        hour:   "2-digit",
        minute: "2-digit",
    });
}

/**
 * Show an element by removing the 'hidden' CSS class.
 */
function show(el) { el.classList.remove("hidden"); }

/**
 * Hide an element by adding the 'hidden' CSS class.
 */
function hide(el) { el.classList.add("hidden"); }

// ---------------------------------------------------------------------------
// ============================  HOME PAGE  ===================================
// ---------------------------------------------------------------------------

/**
 * Fetches all machines from the API and (re-)renders the machine grid.
 * Called on page load and every 30 seconds for auto-refresh.
 */
async function loadMachines() {
    const grid        = document.getElementById("machines-grid");
    const loadingEl   = document.getElementById("loading");
    const errorBanner = document.getElementById("error-banner");

    // Only run this function if these elements exist (i.e. we're on index.html)
    if (!grid) return;

    try {
        const response = await fetch(`${API_BASE}/machines`);

        if (!response.ok) {
            throw new Error(`Server error: ${response.status}`);
        }

        const machines = await response.json();

        // Hide loading indicator and error banner, show the grid
        hide(loadingEl);
        hide(errorBanner);
        show(grid);

        // Remember which filter is active so we can re-apply it
        const activeFilter = document.querySelector(".filter-btn.active")?.dataset.filter || "all";

        // Render all machine cards
        renderMachineCards(machines, activeFilter);

    } catch (err) {
        console.error("Failed to load machines:", err);
        hide(loadingEl);
        show(errorBanner);
    }
}

/**
 * Builds and inserts machine card HTML into the grid.
 *
 * @param {Array}  machines     - Array of machine objects from the API
 * @param {string} filterType   - "all", "Washer", or "Dryer"
 */
function renderMachineCards(machines, filterType) {
    const grid = document.getElementById("machines-grid");
    grid.innerHTML = ""; // clear previous cards

    // Apply filter
    const filtered = filterType === "all"
        ? machines
        : machines.filter(m => m.type === filterType);

    if (filtered.length === 0) {
        grid.innerHTML = `<p style="color:var(--text-muted); grid-column:1/-1;">No machines found.</p>`;
        return;
    }

    filtered.forEach((machine, index) => {
        const isAvailable = machine.status === "Available";
        const cardEl      = document.createElement("div");

        cardEl.className = `machine-card ${isAvailable ? "available" : "busy"}`;
        // Stagger the fade-up animation using a delay
        cardEl.style.animationDelay = `${index * 40}ms`;

        // Build the status section differently based on availability
        const statusHtml = isAvailable
            ? `<span class="status-badge available">
                   <span class="status-dot"></span> Available
               </span>
               <p class="machine-detail">Cycle: <strong>${machine.duration_minutes} minutes</strong></p>`
            : `<span class="status-badge busy">
                   <span class="status-dot"></span> Busy
               </span>
               <p class="machine-detail">Free at: <strong>${formatTime(machine.busy_until)}</strong></p>
               <p class="machine-detail">Booked by: <strong>${machine.booked_by}</strong></p>`;

        // Build the book button (disabled if machine is busy)
        const bookBtnHtml = `
            <button
                class="btn-book"
                onclick="goToBook(${machine.id})"
                ${isAvailable ? "" : "disabled"}
                title="${isAvailable ? "Book this machine" : "Machine is currently busy"}"
            >
                ${isAvailable ? "Book Now" : "Unavailable"}
            </button>`;

        const typeIcon = machine.type === "Washer" ? "🫧" : "💨";

        cardEl.innerHTML = `
            <div class="machine-type-badge">${typeIcon} ${machine.type}</div>
            <h2 class="machine-name">${machine.name}</h2>
            ${statusHtml}
            <div class="card-footer">
                ${bookBtnHtml}
            </div>
        `;

        grid.appendChild(cardEl);
    });
}

/**
 * Navigate to the booking form, pre-selecting the chosen machine.
 * Uses URL query params so book.html can read the selection.
 */
function goToBook(machineId) {
    window.location.href = `/book?machine=${machineId}`;
}

/**
 * Set up the filter buttons on the home page.
 */
function initFilterButtons() {
    const buttons = document.querySelectorAll(".filter-btn");
    if (!buttons.length) return;

    buttons.forEach(btn => {
        btn.addEventListener("click", () => {
            // Update active state
            buttons.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // Re-fetch & re-render (simplest approach — avoids stale data)
            loadMachines();
        });
    });
}

// ---------------------------------------------------------------------------
// ============================  BOOKING PAGE  ================================
// ---------------------------------------------------------------------------

/**
 * Populates the machine <select> dropdown on book.html.
 * Also pre-selects a machine if `?machine=<id>` is in the URL.
 */
async function loadMachineDropdown() {
    const select = document.getElementById("machine-select");
    if (!select) return;  // Not on the booking page

    try {
        const response = await fetch(`${API_BASE}/machines`);
        const machines = await response.json();

        select.innerHTML = '<option value="">— Select a machine —</option>';

        machines.forEach(machine => {
            const opt       = document.createElement("option");
            opt.value       = machine.id;
            opt.textContent = `${machine.name} (${machine.type}) — ${machine.duration_minutes} min`;
            opt.dataset.type = machine.type; // store type so we can show duration hint
            // Disable machines that are currently busy
            if (machine.status === "Busy") {
                opt.textContent += " [Busy]";
                opt.disabled     = true;
            }
            select.appendChild(opt);
        });

        // Pre-select machine from URL query param (e.g. ?machine=3)
        const params      = new URLSearchParams(window.location.search);
        const machineParam = params.get("machine");
        if (machineParam) {
            select.value = machineParam;
            updateDurationHint(); // show hint for pre-selected machine
        }

    } catch (err) {
        console.error("Failed to load machines for dropdown:", err);
        select.innerHTML = '<option value="">⚠ Could not load machines</option>';
    }
}

/**
 * Update the duration hint text below the start-time input whenever the
 * selected machine changes.
 */
function updateDurationHint() {
    const select = document.getElementById("machine-select");
    const hint   = document.getElementById("duration-hint");
    if (!select || !hint) return;

    const selectedOption = select.options[select.selectedIndex];
    if (!selectedOption || !selectedOption.dataset.type) {
        hint.textContent = "Select a machine to see cycle duration.";
        return;
    }

    const type     = selectedOption.dataset.type;
    const duration = CYCLE_DURATIONS[type];
    hint.textContent = `${type} cycle = ${duration} minutes. End time will be calculated automatically.`;
}

/**
 * Set a sensible minimum for the datetime-local input.
 * We set it to "now rounded up to the next 15 minutes" for usability.
 */
function initDateTimePicker() {
    const input = document.getElementById("start-time");
    if (!input) return;

    const now    = new Date();
    // Round up to next 15-minute mark
    const mins   = now.getMinutes();
    const rounded = Math.ceil(mins / 15) * 15;
    now.setMinutes(rounded, 0, 0);

    // Format as "YYYY-MM-DDTHH:MM" for the datetime-local input
    const localISO = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 16);

    input.min   = localISO;
    input.value = localISO; // pre-fill with the next available slot
}

/**
 * Reads form fields, validates them, sends POST /book, and shows feedback.
 * Called from the "Confirm Booking" button's onclick.
 */
async function submitBooking() {
    const studentName = document.getElementById("student-name").value.trim();
    const roomNumber  = document.getElementById("room-number").value.trim();
    const machineId   = document.getElementById("machine-select").value;
    const startTime   = document.getElementById("start-time").value;

    const successMsg  = document.getElementById("success-msg");
    const errorMsg    = document.getElementById("error-msg");
    const submitBtn   = document.getElementById("submit-btn");
    const btnLabel    = document.getElementById("btn-label");
    const btnSpinner  = document.getElementById("btn-spinner");

    // ---- Client-side validation ----
    if (!studentName || !roomNumber || !machineId || !startTime) {
        showBookingError("Please fill in all fields before submitting.");
        return;
    }

    // Keep the local time as-is — datetime-local gives "YYYY-MM-DDTHH:MM"
    // which is exactly what the backend's datetime.fromisoformat() expects.
    // Do NOT use .toISOString() here — that converts to UTC and breaks the
    // "must be in the future" check on the server.
    const isoStartTime = startTime;

    // ---- Show loading state ----
    hide(successMsg);
    hide(errorMsg);
    submitBtn.disabled = true;
    hide(btnLabel);
    show(btnSpinner);

    // ---- Send POST request ----
    try {
        const response = await fetch(`${API_BASE}/book`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({
                student_name: studentName,
                room_number:  roomNumber,
                machine_id:   parseInt(machineId, 10),
                start_time:   isoStartTime,
            }),
        });

        const data = await response.json();

        if (response.ok) {
            // Booking succeeded (HTTP 201)
            showBookingSuccess(data);
            clearForm();
        } else {
            // Booking failed — show the error message from the server
            showBookingError(data.error || "Unknown error. Please try again.");
        }

    } catch (err) {
        console.error("Network error:", err);
        showBookingError("Network error. Is the server running?");
    } finally {
        // Restore the button regardless of outcome
        submitBtn.disabled = false;
        show(btnLabel);
        hide(btnSpinner);
    }
}

/**
 * Display a success message with booking details.
 * @param {Object} data - The response from POST /book
 */
function showBookingSuccess(data) {
    const successMsg = document.getElementById("success-msg");
    const details    = document.getElementById("success-details");
    details.textContent =
        `${data.machine} booked from ${formatDateTime(data.start_time)} ` +
        `to ${formatDateTime(data.end_time)}. Booking ID: #${data.booking_id}`;
    show(successMsg);
    // Scroll to the message so mobile users see it
    successMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/**
 * Display an error message from the server or from client-side validation.
 * @param {string} message
 */
function showBookingError(message) {
    const errorMsg  = document.getElementById("error-msg");
    const errorText = document.getElementById("error-details");
    errorText.textContent = message;
    show(errorMsg);
    errorMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/**
 * Reset the form fields after a successful booking.
 */
function clearForm() {
    document.getElementById("student-name").value = "";
    document.getElementById("room-number").value  = "";
    document.getElementById("machine-select").value = "";
    initDateTimePicker(); // reset to next available time
    updateDurationHint();
}

// ---------------------------------------------------------------------------
// Initialization — runs when the DOM is ready
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {

    // ---- Home page setup ----
    if (document.getElementById("machines-grid")) {
        loadMachines();
        initFilterButtons();

        // Auto-refresh machine statuses every 30 seconds
        setInterval(loadMachines, 30_000);
    }

    // ---- Booking page setup ----
    if (document.getElementById("machine-select")) {
        loadMachineDropdown();
        initDateTimePicker();

        // Update the duration hint whenever the machine selection changes
        document.getElementById("machine-select")
            .addEventListener("change", updateDurationHint);
    }

});
