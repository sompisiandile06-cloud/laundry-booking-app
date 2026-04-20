/**
 * ResidenceLaundry — Admin Dashboard JavaScript
 * ===============================================
 * Handles:
 *   - Loading and displaying stats cards
 *   - Loading and rendering the machines management grid
 *   - Loading, filtering, and displaying the bookings table
 *   - Cancelling bookings
 *   - Toggling machines in/out of service
 *   - Auto-refresh every 30 seconds
 */

"use strict";

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function formatDateTime(isoString) {
    const date = new Date(isoString);
    return date.toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

// Store all bookings in memory so filter tabs don't need a new API call
let allBookings = [];

// ---------------------------------------------------------------------------
// Stats Cards
// ---------------------------------------------------------------------------

async function loadStats() {
    try {
        const resp  = await fetch("/admin/stats");
        const stats = await resp.json();

        document.getElementById("stats-grid").innerHTML = `
            <div class="stat-card">
                <p class="stat-label">Bookings Today</p>
                <p class="stat-value">${stats.bookings_today}</p>
            </div>
            <div class="stat-card">
                <p class="stat-label">Currently Active</p>
                <p class="stat-value">${stats.active_now}</p>
            </div>
            <div class="stat-card">
                <p class="stat-label">Upcoming (24h)</p>
                <p class="stat-value">${stats.upcoming_24h}</p>
            </div>
            <div class="stat-card">
                <p class="stat-label">Top Machine (7d)</p>
                <p class="stat-value stat-value-sm">${stats.top_machine}</p>
            </div>
            <div class="stat-card">
                <p class="stat-label">Machines Online</p>
                <p class="stat-value">${stats.active_machines} <span class="stat-of">/ ${stats.total_machines}</span></p>
            </div>
        `;
    } catch (err) {
        console.error("Failed to load stats:", err);
    }
}

// ---------------------------------------------------------------------------
// Machines Management
// ---------------------------------------------------------------------------

async function loadAdminMachines() {
    const grid = document.getElementById("admin-machines-grid");
    if (!grid) return;

    try {
        const resp     = await fetch("/admin/machines");
        const machines = await resp.json();

        grid.innerHTML = "";

        machines.forEach((machine, index) => {
            const isActive = machine.is_active;
            const cardEl   = document.createElement("div");
            cardEl.className = `machine-card ${isActive ? (machine.status === "Busy" ? "busy" : "available") : "out-of-service"}`;
            cardEl.style.animationDelay = `${index * 40}ms`;

            const typeIcon = machine.type === "Washer" ? "🫧" : "💨";

            let statusHtml = "";
            if (!isActive) {
                statusHtml = `<span class="status-badge oos"><span class="status-dot"></span> Out of Service</span>`;
            } else if (machine.status === "Busy") {
                statusHtml = `<span class="status-badge busy"><span class="status-dot"></span> Busy</span>
                              <p class="machine-detail">Free at: <strong>${new Date(machine.busy_until).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</strong></p>`;
            } else {
                statusHtml = `<span class="status-badge available"><span class="status-dot"></span> Available</span>`;
            }

            cardEl.innerHTML = `
                <div class="machine-type-badge">${typeIcon} ${machine.type}</div>
                <h2 class="machine-name">${machine.name}</h2>
                ${statusHtml}
                <div class="card-footer" style="flex-direction:column; gap:8px;">
                    <button
                        class="btn-book ${isActive ? "btn-deactivate" : "btn-activate"}"
                        onclick="toggleMachine(${machine.id}, ${!isActive})"
                        style="width:100%;"
                    >
                        ${isActive ? "Mark Out of Service" : "Bring Back Online"}
                    </button>
                    <button
                        class="btn-book btn-delete-machine"
                        onclick="deleteMachine(${machine.id}, '${machine.name}')"
                        style="width:100%;"
                    >
                        Delete Machine
                    </button>
                </div>
            `;
            grid.appendChild(cardEl);
        });
    } catch (err) {
        console.error("Failed to load admin machines:", err);
    }
}

async function toggleMachine(machineId, newActiveState) {
    const label = newActiveState ? "bring back online" : "mark as out of service";
    if (!confirm(`Are you sure you want to ${label} this machine?`)) return;

    try {
        const resp = await fetch(`/admin/machines/${machineId}`, {
            method:  "PATCH",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ is_active: newActiveState }),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(data.message, "success");
            loadAdminMachines(); // refresh the grid
            loadStats();
        } else {
            showToast(data.error || "Failed to update machine.", "error");
        }
    } catch (err) {
        showToast("Network error. Please try again.", "error");
    }
}

// ---------------------------------------------------------------------------
// Bookings Table
// ---------------------------------------------------------------------------

async function loadBookings() {
    const loadingEl  = document.getElementById("bookings-loading");
    const tableWrap  = document.getElementById("bookings-table-wrap");
    const emptyEl    = document.getElementById("bookings-empty");

    show(loadingEl);
    hide(tableWrap);
    hide(emptyEl);

    try {
        const resp = await fetch("/admin/bookings");
        allBookings = await resp.json();

        hide(loadingEl);

        // Apply whichever filter tab is currently active
        const activeTab = document.querySelector(".booking-filter-tabs .filter-btn.active");
        const status    = activeTab ? activeTab.dataset.status : "all";
        renderBookingsTable(status);

    } catch (err) {
        console.error("Failed to load bookings:", err);
        hide(loadingEl);
    }
}

function filterBookings(status) {
    // Update active tab styling
    document.querySelectorAll(".booking-filter-tabs .filter-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.status === status);
    });
    renderBookingsTable(status);
}

function renderBookingsTable(status) {
    const tableWrap = document.getElementById("bookings-table-wrap");
    const emptyEl   = document.getElementById("bookings-empty");
    const tbody     = document.getElementById("bookings-tbody");

    const filtered = status === "all"
        ? allBookings
        : allBookings.filter(b => b.status === status);

    if (filtered.length === 0) {
        hide(tableWrap);
        show(emptyEl);
        return;
    }

    show(tableWrap);
    hide(emptyEl);

    tbody.innerHTML = filtered.map(booking => {
        const statusClass = booking.status === "Active"
            ? "status-active"
            : booking.status === "Cancelled"
            ? "status-cancelled"
            : "status-completed";

        // Only show cancel button for Active bookings
        const actionBtn = booking.status === "Active"
            ? `<button class="btn-cancel" onclick="cancelBooking(${booking.id})">Cancel</button>`
            : `<span class="no-action">—</span>`;

        return `
            <tr>
                <td>#${booking.id}</td>
                <td>${booking.student_name}</td>
                <td>${booking.room_number}</td>
                <td>${booking.machine_name}</td>
                <td>${formatDateTime(booking.start_time)}</td>
                <td>${formatDateTime(booking.end_time)}</td>
                <td><span class="booking-status ${statusClass}">${booking.status}</span></td>
                <td>${actionBtn}</td>
            </tr>
        `;
    }).join("");
}

async function cancelBooking(bookingId) {
    if (!confirm(`Cancel booking #${bookingId}? This cannot be undone.`)) return;

    try {
        const resp = await fetch(`/admin/bookings/${bookingId}`, { method: "DELETE" });
        const data = await resp.json();

        if (resp.ok) {
            showToast(data.message, "success");
            loadBookings(); // refresh table
            loadStats();    // refresh counts
        } else {
            showToast(data.error || "Failed to cancel booking.", "error");
        }
    } catch (err) {
        showToast("Network error. Please try again.", "error");
    }
}

// ---------------------------------------------------------------------------
// Toast notifications (non-blocking feedback)
// ---------------------------------------------------------------------------

function showToast(message, type = "success") {
    // Remove any existing toast
    const existing = document.getElementById("toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.id        = "toast";
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    // Auto-remove after 3 seconds
    setTimeout(() => toast.remove(), 3000);
}


// ---------------------------------------------------------------------------
// Add Machine
// ---------------------------------------------------------------------------

async function addMachine() {
    const nameInput = document.getElementById("new-machine-name");
    const typeInput = document.getElementById("new-machine-type");
    const feedback  = document.getElementById("add-machine-feedback");

    const name = nameInput.value.trim();
    const type = typeInput.value;

    if (!name) {
        feedback.textContent = "Please enter a machine name.";
        feedback.className   = "add-machine-feedback feedback-error";
        return;
    }

    try {
        const resp = await fetch("/admin/machines", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ name, type }),
        });
        const data = await resp.json();

        if (resp.ok) {
            feedback.textContent = "✓ " + data.message;
            feedback.className   = "add-machine-feedback feedback-success";
            nameInput.value      = "";   // clear the name field
            typeInput.value      = "Washer"; // reset to default
            loadAdminMachines(); // refresh the machine grid
            loadStats();
        } else {
            feedback.textContent = "✕ " + data.error;
            feedback.className   = "add-machine-feedback feedback-error";
        }
    } catch (err) {
        feedback.textContent = "Network error. Please try again.";
        feedback.className   = "add-machine-feedback feedback-error";
    }
}

// ---------------------------------------------------------------------------
// Delete Machine (called from machine card)
// ---------------------------------------------------------------------------

async function deleteMachine(machineId, machineName) {
    if (!confirm(
        `Permanently delete ${machineName}?\n\n` +
        `This cannot be undone. All past bookings for this machine will remain in history.`
    )) return;

    try {
        const resp = await fetch(`/admin/machines/${machineId}`, { method: "DELETE" });
        const data = await resp.json();

        if (resp.ok) {
            showToast(data.message, "success");
            loadAdminMachines();
            loadStats();
        } else {
            showToast(data.error, "error");
        }
    } catch (err) {
        showToast("Network error. Please try again.", "error");
    }
}

// ---------------------------------------------------------------------------
// Clear Booking History
// ---------------------------------------------------------------------------

async function clearHistory(type) {
    // Build a clear confirmation message depending on what is being cleared
    const labels = {
        "Completed": "all completed bookings",
        "Cancelled": "all cancelled bookings",
        "all":       "all completed AND cancelled bookings",
    };
    const label = labels[type] || "past bookings";

    if (!confirm(
        `Are you sure you want to permanently delete ${label}?\n\n` +
        `Active bookings will NOT be affected. This cannot be undone.`
    )) return;

    const url = type === "all"
        ? "/admin/bookings/clear-history"
        : `/admin/bookings/clear-history?status=${type}`;

    try {
        const resp = await fetch(url, { method: "DELETE" });
        const data = await resp.json();

        if (resp.ok) {
            showToast(data.message, "success");
            loadBookings(); // refresh the table
        } else {
            showToast(data.error || "Failed to clear history.", "error");
        }
    } catch (err) {
        showToast("Network error. Please try again.", "error");
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
    loadStats();
    loadAdminMachines();
    loadBookings();

    // Auto-refresh everything every 30 seconds
    setInterval(() => {
        loadStats();
        loadAdminMachines();
        loadBookings();
    }, 30_000);
});
