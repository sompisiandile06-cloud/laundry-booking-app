/**
 * ResidenceLaundry — Frontend JavaScript (v3)
 * =============================================
 * Changes from v2:
 *   - Machine cards now show "Next available: HH:MM" for busy machines
 *   - "Book Next Slot" button auto-fills the booking form with the next free time
 *   - Out-of-service machines shown with a distinct style
 *   - Conflict error response now includes next_available, shown to the user
 */

"use strict";

const API_BASE = "";

const CYCLE_DURATIONS = { Washer: 45, Dryer: 60 };

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function formatTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Convert a datetime to "YYYY-MM-DDTHH:MM" for datetime-local inputs */
function toLocalInputValue(date) {
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 16);
}

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

// ---------------------------------------------------------------------------
// HOME PAGE
// ---------------------------------------------------------------------------

async function loadMachines() {
    const grid        = document.getElementById("machines-grid");
    const loadingEl   = document.getElementById("loading");
    const errorBanner = document.getElementById("error-banner");
    if (!grid) return;

    try {
        const response = await fetch(`${API_BASE}/machines`);
        if (!response.ok) throw new Error(`Server error: ${response.status}`);
        const machines = await response.json();

        hide(loadingEl);
        hide(errorBanner);
        show(grid);

        const activeFilter = document.querySelector(".filter-btn.active")?.dataset.filter || "all";
        renderMachineCards(machines, activeFilter);
    } catch (err) {
        console.error("Failed to load machines:", err);
        hide(loadingEl);
        show(errorBanner);
    }
}

function renderMachineCards(machines, filterType) {
    const grid = document.getElementById("machines-grid");
    grid.innerHTML = "";

    const filtered = filterType === "all" ? machines : machines.filter(m => m.type === filterType);

    if (filtered.length === 0) {
        grid.innerHTML = `<p style="color:var(--text-muted);grid-column:1/-1;">No machines found.</p>`;
        return;
    }

    filtered.forEach((machine, index) => {
        const isAvailable = machine.status === "Available";
        const isOOS       = machine.status === "Out of Service";
        const isBusy      = machine.status === "Busy";

        const cardEl = document.createElement("div");
        cardEl.className = `machine-card ${isAvailable ? "available" : isBusy ? "busy" : "out-of-service"}`;
        cardEl.style.animationDelay = `${index * 40}ms`;

        const typeIcon = machine.type === "Washer" ? "🫧" : "💨";

        // Build the next-available line shown on busy machines
        const nextSlotTime = machine.next_available ? formatTime(machine.next_available) : null;

        let statusHtml = "";
        if (isAvailable) {
            statusHtml = `
                <span class="status-badge available"><span class="status-dot"></span> Available</span>
                <p class="machine-detail">Cycle: <strong>${machine.duration_minutes} min</strong></p>`;
        } else if (isBusy) {
            statusHtml = `
                <span class="status-badge busy"><span class="status-dot"></span> Busy</span>
                <p class="machine-detail">Free at: <strong>${formatTime(machine.busy_until)}</strong></p>
                <p class="machine-detail">Booked by: <strong>${machine.booked_by}</strong></p>
                <p class="machine-detail next-slot-hint">Next slot: <strong>${nextSlotTime}</strong></p>`;
        } else {
            statusHtml = `<span class="status-badge oos"><span class="status-dot"></span> Out of Service</span>`;
        }

        // Button logic:
        // Available → "Book Now" (goes to book page, machine pre-selected, current time)
        // Busy      → "Book Next Slot" (goes to book page, machine + next slot pre-filled)
        // OOS       → disabled
        let btnHtml = "";
        if (isAvailable) {
            btnHtml = `<button class="btn-book" onclick="goToBook(${machine.id}, null)">Book Now</button>`;
        } else if (isBusy) {
            btnHtml = `<button class="btn-book btn-book-next" onclick="goToBook(${machine.id}, '${machine.next_available}')">Book Next Slot</button>`;
        } else {
            btnHtml = `<button class="btn-book" disabled>Out of Service</button>`;
        }

        cardEl.innerHTML = `
            <div class="machine-type-badge">${typeIcon} ${machine.type}</div>
            <h2 class="machine-name">${machine.name}</h2>
            ${statusHtml}
            <div class="card-footer">${btnHtml}</div>
        `;

        grid.appendChild(cardEl);
    });
}

/**
 * Navigate to the booking form.
 * If nextSlot is provided (ISO string), it pre-fills the start time too.
 */
function goToBook(machineId, nextSlot) {
    let url = `/book?machine=${machineId}`;
    if (nextSlot) {
        // Format the ISO string into the value format for datetime-local
        const date = new Date(nextSlot);
        url += `&time=${toLocalInputValue(date)}`;
    }
    window.location.href = url;
}

function initFilterButtons() {
    const buttons = document.querySelectorAll(".filter-btn");
    if (!buttons.length) return;
    buttons.forEach(btn => {
        btn.addEventListener("click", () => {
            buttons.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            loadMachines();
        });
    });
}

// ---------------------------------------------------------------------------
// BOOKING PAGE
// ---------------------------------------------------------------------------

async function loadMachineDropdown() {
    const select = document.getElementById("machine-select");
    if (!select) return;

    try {
        const response = await fetch(`${API_BASE}/machines`);
        const machines = await response.json();

        select.innerHTML = '<option value="">— Select a machine —</option>';

        machines.forEach(machine => {
            const opt        = document.createElement("option");
            opt.value        = machine.id;
            opt.dataset.type = machine.type;
            opt.dataset.next = machine.next_available || "";
            opt.textContent  = `${machine.name} (${machine.type}) — ${machine.duration_minutes} min`;
            if (machine.status === "Busy") {
                opt.textContent += ` — Next: ${formatTime(machine.next_available)}`;
            }
            select.appendChild(opt);
        });

        // Pre-select machine from URL
        const params = new URLSearchParams(window.location.search);
        const machineParam = params.get("machine");
        const timeParam    = params.get("time");

        if (machineParam) {
            select.value = machineParam;
            updateDurationHint();
        }

        // Pre-fill time from URL (set by "Book Next Slot" button)
        if (timeParam) {
            const input = document.getElementById("start-time");
            if (input) input.value = timeParam;
        }

    } catch (err) {
        console.error("Failed to load machines for dropdown:", err);
        select.innerHTML = '<option value="">⚠ Could not load machines</option>';
    }
}

function updateDurationHint() {
    const select = document.getElementById("machine-select");
    const hint   = document.getElementById("duration-hint");
    if (!select || !hint) return;

    const opt = select.options[select.selectedIndex];
    if (!opt || !opt.dataset.type) {
        hint.textContent = "Select a machine to see cycle duration.";
        return;
    }
    const duration = CYCLE_DURATIONS[opt.dataset.type];
    hint.textContent = `${opt.dataset.type} cycle = ${duration} minutes. End time calculated automatically.`;
}

function initDateTimePicker() {
    const input = document.getElementById("start-time");
    if (!input) return;

    // Only set default if not already set by URL param
    if (!input.value) {
        const now     = new Date();
        const mins    = now.getMinutes();
        const rounded = Math.ceil(mins / 15) * 15;
        now.setMinutes(rounded, 0, 0);
        input.value = toLocalInputValue(now);
    }

    const now    = new Date();
    input.min    = toLocalInputValue(now);
}

async function submitBooking() {
    const studentName = document.getElementById("student-name").value.trim();
    const roomNumber  = document.getElementById("room-number").value.trim();
    const machineId   = document.getElementById("machine-select").value;
    const startTime   = document.getElementById("start-time").value;

    const successMsg = document.getElementById("success-msg");
    const errorMsg   = document.getElementById("error-msg");
    const submitBtn  = document.getElementById("submit-btn");
    const btnLabel   = document.getElementById("btn-label");
    const btnSpinner = document.getElementById("btn-spinner");

    if (!studentName || !roomNumber || !machineId || !startTime) {
        showBookingError("Please fill in all fields before submitting.");
        return;
    }

    hide(successMsg);
    hide(errorMsg);
    submitBtn.disabled = true;
    hide(btnLabel);
    show(btnSpinner);

    try {
        // getTimezoneOffset() returns minutes BEHIND UTC.
        // e.g. SA (UTC+2) returns -120. We send this to the server
        // so it can convert the local time to UTC before saving.
        const utcOffset = new Date().getTimezoneOffset();

        const response = await fetch(`${API_BASE}/book`, {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({
                student_name: studentName,
                room_number:  roomNumber,
                machine_id:   parseInt(machineId, 10),
                start_time:   startTime,  // local time — server converts using utc_offset
                utc_offset:   utcOffset,  // e.g. -120 for UTC+2 (South Africa)
            }),
        });

        const data = await response.json();

        if (response.ok) {
            showBookingSuccess(data);
            clearForm();
        } else {
            // If the server returned next_available, offer to auto-fill it
            let errorText = data.error || "Unknown error. Please try again.";
            if (data.next_available) {
                const nextTime = formatDateTime(data.next_available);
                errorText += ` Next available slot: ${nextTime}.`;
                // Auto-fill the next available time into the input
                const input = document.getElementById("start-time");
                const date  = new Date(data.next_available);
                if (input) input.value = toLocalInputValue(date);
            }
            showBookingError(errorText);
        }
    } catch (err) {
        console.error("Network error:", err);
        showBookingError("Network error. Is the server running?");
    } finally {
        submitBtn.disabled = false;
        show(btnLabel);
        hide(btnSpinner);
    }
}

function showBookingSuccess(data) {
    const successMsg = document.getElementById("success-msg");
    const details    = document.getElementById("success-details");
    details.textContent =
        `${data.machine} booked from ${formatDateTime(data.start_time)} ` +
        `to ${formatDateTime(data.end_time)}. Booking ID: #${data.booking_id}`;
    show(successMsg);
    successMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showBookingError(message) {
    const errorMsg  = document.getElementById("error-msg");
    const errorText = document.getElementById("error-details");
    errorText.textContent = message;
    show(errorMsg);
    errorMsg.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearForm() {
    document.getElementById("student-name").value    = "";
    document.getElementById("room-number").value     = "";
    document.getElementById("machine-select").value  = "";
    initDateTimePicker();
    updateDurationHint();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {

    if (document.getElementById("machines-grid")) {
        loadMachines();
        initFilterButtons();
        setInterval(loadMachines, 30_000);
    }

    if (document.getElementById("machine-select")) {
        loadMachineDropdown();
        initDateTimePicker();
        document.getElementById("machine-select")
            .addEventListener("change", updateDurationHint);
    }

});
