/* ══════════════════════════════════════════════════════════════════════
   Feature #4 — Bulk Assignment JavaScript
   WebSocket connection, queue management, recommendations, assignment
   ══════════════════════════════════════════════════════════════════════ */

// ── State ─────────────────────────────────────────────────────────────

var _bulkUserId = null;
var _bulkWs = null;
var _bulkQueue = [];          // Array of QueueTicketSummary objects
var _bulkLocks = {};          // ticket_id → user_id
var _bulkRecs = {};           // ticket_id → TicketRecommendation
var _bulkOverrides = {};      // ticket_id → { tier_queue_guid, tier_queue_name, priority }
var _bulkSelected = new Set();
var _bulkBusy = false;
var _bulkStreamingLoad = false;  // true while WebSocket streaming queue load is in progress
var _bulkLockPending = new Set();  // ticket IDs with in-flight lock/unlock requests

// Support group lists for manual assignment: ticket_type → [{name, guid}, ...]
var _bulkSupportGroups = {};
var _bulkSgLoading = {};  // ticket_type → true while loading

// Online presence: list of user IDs currently connected
var _bulkOnlineUsers = [];

// Per-user color assignments: user_id → color hex (from server)
var _bulkUserColors = {};

// Blue color for the current user (Penn Medicine accent)
var _USER_COLOR_SELF = '#4A90D9';
// Green color for unlocked tickets
var _USER_COLOR_UNLOCKED = '#27ae60';

// ── Toast Batching (debounce rapid-fire events into single notifications) ──
// Key: "user_id|event_type" → { count: N, timer: setTimeout_id }
var _toastBatchBuffer = {};
var _TOAST_BATCH_DELAY_MS = 200;

/**
 * Batch rapid-fire toasts from the same user + event type into a single
 * notification. E.g., 10 unlock events within 200ms become:
 * "Test User 1 unlocked 10 tickets"
 */
function _batchedToast(userId, eventType, toastType) {
    var key = userId + '|' + eventType;
    var entry = _toastBatchBuffer[key];

    if (entry) {
        // Already buffering — increment count and reset timer
        entry.count++;
        clearTimeout(entry.timer);
    } else {
        entry = { count: 1, timer: null };
        _toastBatchBuffer[key] = entry;
    }

    entry.timer = setTimeout(function () {
        var count = entry.count;
        delete _toastBatchBuffer[key];

        // Build the coalesced message
        var verb = eventType === 'lock' ? 'locked' :
                   eventType === 'unlock' ? 'unlocked' :
                   eventType === 'assign' ? 'assigned' : eventType;
        var noun = count === 1 ? 'ticket' : 'tickets';
        _showToast(userId + ' ' + verb + ' ' + count + ' ' + noun, toastType || 'info');
    }, _TOAST_BATCH_DELAY_MS);
}

// ── Auto-Init (uses authenticated session) ────────────────────────────

function _bulkAutoInit() {
    // Read the authenticated user's display name injected by the server template
    var userId = window.BULK_USER_ID;
    if (!userId) {
        console.error('BULK_USER_ID not set — cannot initialize bulk assignment');
        return;
    }

    _bulkUserId = userId;
    document.getElementById('bulkUserDisplay').textContent = userId;

    _connectWebSocket();
    // Queue will be loaded via WebSocket streaming after connection opens
    // (see _bulkWs.onopen handler which sends load_queue action)
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _bulkAutoInit);
} else {
    _bulkAutoInit();
}

// ── WebSocket ─────────────────────────────────────────────────────────

function _connectWebSocket() {
    if (_bulkWs) {
        try { _bulkWs.close(); } catch (e) { /* ignore */ }
    }

    var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = protocol + '//' + location.host + '/bulk/ws?user_id=' + encodeURIComponent(_bulkUserId);

    _bulkWs = new WebSocket(url);

    _bulkWs.onopen = function () {
        _setWsStatus(true);
        _showToast('Connected to queue', 'info');
        // Start streaming queue load via WebSocket
        _bulkStreamingLoadQueue();
    };

    _bulkWs.onclose = function () {
        _setWsStatus(false);
        // Auto-reconnect after 3 seconds
        setTimeout(function () {
            if (_bulkUserId) _connectWebSocket();
        }, 3000);
    };

    _bulkWs.onerror = function () {
        _setWsStatus(false);
    };

    _bulkWs.onmessage = function (evt) {
        try {
            var data = JSON.parse(evt.data);
            _handleWsEvent(data);
        } catch (e) {
            console.warn('Invalid WS message:', evt.data);
        }
    };
}

function _setWsStatus(connected) {
    var dot = document.getElementById('bulkWsDot');
    var label = document.getElementById('bulkWsStatus');
    if (connected) {
        dot.className = 'bulk-status-dot connected';
        label.textContent = 'Connected';
    } else {
        dot.className = 'bulk-status-dot disconnected';
        label.textContent = 'Disconnected';
    }
}

function _handleWsEvent(data) {
    var event = data.event;

    if (event === 'state_sync') {
        _bulkLocks = data.locks || {};
        if (data.user_colors) {
            _bulkUserColors = data.user_colors;
        }
        if (data.users) {
            _bulkOnlineUsers = data.users;
            _renderPresence();
        }
        _renderQueue();
        _updateCounts();
    } else if (event === 'lock') {
        _bulkLocks[data.ticket_id] = data.user_id;
        _updateTicketRow(data.ticket_id);
        _updateCounts();
        if (data.user_id !== _bulkUserId) {
            _batchedToast(data.user_id, 'lock', 'info');
        }
    } else if (event === 'unlock') {
        delete _bulkLocks[data.ticket_id];
        _updateTicketRow(data.ticket_id);
        _updateCounts();
        if (data.user_id !== _bulkUserId) {
            _batchedToast(data.user_id, 'unlock', 'info');
        }
    } else if (event === 'assign') {
        // Remove ticket from queue
        delete _bulkLocks[data.ticket_id];
        _bulkQueue = _bulkQueue.filter(function (t) { return t.id !== data.ticket_id; });
        _bulkSelected.delete(data.ticket_id);
        delete _bulkRecs[data.ticket_id];
        delete _bulkOverrides[data.ticket_id];
        _renderQueue();
        _updateCounts();
        if (data.user_id !== _bulkUserId) {
            _batchedToast(data.user_id, 'assign', 'info');
        }

    // ── Recommendation Progress Events ────────────────────────────
    } else if (event === 'rec_start') {
        // A user started a recommendation batch — mark tickets as pending
        var ticketIds = data.ticket_ids || [];
        ticketIds.forEach(function (tid) {
            _bulkRecPending.add(tid);
        });
        _bulkRecProcessing = null;
        _applyRecProgressStyles();

    } else if (event === 'rec_processing') {
        // A specific ticket is now being processed
        _bulkRecPending.delete(data.ticket_id);
        _bulkRecProcessing = data.ticket_id;
        _applyRecProgressStyles();

    } else if (event === 'rec_result') {
        // A specific ticket's recommendation is done
        _bulkRecPending.delete(data.ticket_id);
        if (_bulkRecProcessing === data.ticket_id) {
            _bulkRecProcessing = null;
        }
        _applyRecProgressStyles();

    } else if (event === 'rec_complete') {
        // Entire batch is done — clear all progress states
        _bulkRecPending.clear();
        _bulkRecProcessing = null;
        _applyRecProgressStyles();

    // ── Queue Streaming Events ────────────────────────────────────
    } else if (event === 'queue_loading_start') {
        _bulkStreamingLoad = true;
        _bulkQueue = [];
        _bulkSelected.clear();
        _bulkRecs = {};
        _bulkOverrides = {};
        var body = document.getElementById('bulkQueueBody');
        body.innerHTML = '<div class="bulk-loading-stream">' +
            '<div class="bulk-spinner"></div>' +
            '<span class="bulk-loading-text">Fetching tickets from Athena…</span>' +
            '<span class="bulk-loading-counter" id="bulkStreamCount">0 tickets loaded</span>' +
            '</div>';

    } else if (event === 'queue_ticket') {
        var ticket = data.ticket;
        var count = data.count;
        if (ticket) {
            _bulkQueue.push(ticket);
            // Update the loading counter
            var counter = document.getElementById('bulkStreamCount');
            if (counter) {
                counter.textContent = count + ' ticket' + (count !== 1 ? 's' : '') + ' loaded';
            }
            // Incrementally render: append row to existing table or create table on first ticket
            _appendTicketToTable(ticket);
        }

    } else if (event === 'queue_loading_complete') {
        _bulkStreamingLoad = false;
        // Merge lock state from the complete event
        var serverLocks = data.locks || {};
        for (var k in serverLocks) {
            _bulkLocks[k] = serverLocks[k];
        }
        // Remove the streaming loading indicator if still present
        var loadingEl = document.querySelector('.bulk-loading-stream');
        if (loadingEl) loadingEl.remove();
        // Full re-render to apply lock state and sort
        _bulkQueue.sort(function (a, b) {
            return (a.created_date || '').localeCompare(b.created_date || '');
        });
        _renderQueue();
        _updateCounts();
        document.getElementById('bulkQueueBadge').textContent = _bulkQueue.length + ' tickets';
        _showToast('Queue loaded: ' + data.total + ' tickets', 'success');

    } else if (event === 'queue_loading_error') {
        _bulkStreamingLoad = false;
        var body = document.getElementById('bulkQueueBody');
        body.innerHTML = '<div class="bulk-empty-state"><div class="empty-icon">⚠️</div>' +
            '<div class="empty-text">Failed to load queue</div>' +
            '<div class="empty-hint">' + _escapeHtml(data.message || 'Unknown error') + '</div></div>';
        _showToast('Failed to load queue: ' + (data.message || 'Unknown error'), 'error');

    // ── Presence Events ───────────────────────────────────────────
    } else if (event === 'presence_join') {
        _bulkOnlineUsers = data.users || [];
        if (data.user_colors) {
            _bulkUserColors = data.user_colors;
        }
        _renderPresence();
        if (data.user_id !== _bulkUserId) {
            _showToast(data.user_id + ' joined', 'info');
        }

    } else if (event === 'presence_leave') {
        _bulkOnlineUsers = data.users || [];
        if (data.user_colors) {
            _bulkUserColors = data.user_colors;
        }
        _renderPresence();
        if (data.user_id !== _bulkUserId) {
            _showToast(data.user_id + ' left', 'info');
        }

    // ── Queue Auto-Refresh Events ─────────────────────────────────
    } else if (event === 'queue_refresh') {
        _handleQueueRefresh(data);
    }
}

/**
 * Handle an incremental queue_refresh event from the server.
 * Removes departed tickets and appends new ones without a full re-render.
 */
function _handleQueueRefresh(data) {
    var removed = data.removed || [];
    var added = data.added || [];
    var serverLocks = data.locks || {};

    if (removed.length === 0 && added.length === 0) return;

    // ── Removals ──────────────────────────────────────────────────
    removed.forEach(function (tid) {
        // Animate and remove DOM rows
        _removeTicketRows(tid);
        // Clean up in-memory state
        _bulkQueue = _bulkQueue.filter(function (t) { return t.id !== tid; });
        _bulkSelected.delete(tid);
        delete _bulkLocks[tid];
        delete _bulkRecs[tid];
        delete _bulkOverrides[tid];
    });

    // ── Additions ─────────────────────────────────────────────────
    added.forEach(function (ticket) {
        // Avoid duplicates (defensive)
        if (_findTicket(ticket.id)) return;
        _bulkQueue.push(ticket);
    });

    // ── Update lock state ─────────────────────────────────────────
    for (var k in serverLocks) {
        _bulkLocks[k] = serverLocks[k];
    }

    // If there were additions, re-sort and do a full re-render
    // (simpler than inserting rows in sorted order with detail/rec rows)
    if (added.length > 0) {
        _bulkQueue.sort(function (a, b) {
            return (a.created_date || '').localeCompare(b.created_date || '');
        });
        _renderQueue();
        // Highlight newly added rows
        added.forEach(function (ticket) {
            var row = document.querySelector('tr[data-ticket-id="' + ticket.id + '"]');
            if (row) row.classList.add('bulk-row-added');
        });
    }

    _updateCounts();
    document.getElementById('bulkQueueBadge').textContent = _bulkQueue.length + ' tickets';

    // Toast summary
    var parts = [];
    if (added.length > 0) parts.push('+' + added.length + ' added');
    if (removed.length > 0) parts.push('-' + removed.length + ' removed');
    _showToast('Queue updated: ' + parts.join(', '), 'info');
}

/**
 * Remove all DOM rows for a ticket (main row + detail row + rec row)
 * with a brief fade-out animation.
 */
function _removeTicketRows(ticketId) {
    // Main row
    var row = document.querySelector('tr[data-ticket-id="' + ticketId + '"]');
    if (row) {
        // Detail row is the next sibling
        var next = row.nextElementSibling;
        if (next && next.classList.contains('bulk-detail-row')) {
            var recRow = next.nextElementSibling;
            if (recRow && recRow.classList.contains('bulk-rec-row')) {
                recRow.classList.add('bulk-row-removing');
                setTimeout(function () { recRow.remove(); }, 300);
            }
            next.classList.add('bulk-row-removing');
            setTimeout(function () { next.remove(); }, 300);
        }
        row.classList.add('bulk-row-removing');
        setTimeout(function () { row.remove(); }, 300);
    }
}

// ── Queue Fetching ────────────────────────────────────────────────────

/**
 * Stream queue load via WebSocket — sends load_queue action and
 * receives tickets one-by-one via queue_ticket events.
 */
function _bulkStreamingLoadQueue() {
    if (_bulkStreamingLoad) return;
    if (!_bulkWs || _bulkWs.readyState !== WebSocket.OPEN) {
        // Fallback to REST if WebSocket not connected
        bulkRefreshQueue();
        return;
    }
    _bulkWs.send(JSON.stringify({ action: 'load_queue' }));
}

/**
 * Refresh queue — uses WebSocket streaming if connected, REST fallback otherwise.
 * The Refresh button calls this function.
 */
function bulkRefreshQueue() {
    // Try WebSocket streaming first
    if (_bulkWs && _bulkWs.readyState === WebSocket.OPEN && !_bulkStreamingLoad) {
        _bulkStreamingLoadQueue();
        return;
    }

    // REST fallback
    if (_bulkBusy) return;
    _setBusy(true);

    var body = document.getElementById('bulkQueueBody');
    body.innerHTML = '<div class="bulk-loading"><div class="bulk-spinner"></div><span>Loading queue…</span></div>';

    fetch('/bulk/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            tier_queue_name: 'Validation',
            statuses: ['Active', 'Work in Progress']
        })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        _bulkQueue = data.tickets || [];
        // Merge lock state from response
        var serverLocks = data.locks || {};
        for (var k in serverLocks) {
            _bulkLocks[k] = serverLocks[k];
        }
        _renderQueue();
        _updateCounts();
        document.getElementById('bulkQueueBadge').textContent = _bulkQueue.length + ' tickets';
        _showToast('Queue loaded: ' + _bulkQueue.length + ' tickets', 'success');
    })
    .catch(function (err) {
        body.innerHTML = '<div class="bulk-empty-state"><div class="empty-icon">⚠️</div>' +
            '<div class="empty-text">Failed to load queue</div>' +
            '<div class="empty-hint">' + _escapeHtml(err.message) + '</div></div>';
        _showToast('Failed to load queue: ' + err.message, 'error');
    })
    .finally(function () {
        _setBusy(false);
    });
}

// ── Claim Batch ───────────────────────────────────────────────────────

function bulkClaimBatch() {
    if (_bulkBusy || !_bulkUserId) return;
    var batchSize = parseInt(document.getElementById('bulkBatchSize').value) || 10;
    _setBusy(true);

    // Send local queue ticket IDs so the server can skip re-fetching from Athena
    var ticketIds = _bulkQueue.map(function (t) { return t.id; });

    fetch('/bulk/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: _bulkUserId, batch_size: batchSize, ticket_ids: ticketIds })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var claimed = data.claimed_ticket_ids || [];
        // Update local lock state
        claimed.forEach(function (tid) {
            _bulkLocks[tid] = _bulkUserId;
            _bulkSelected.add(tid);
        });
        _renderQueue();
        _updateCounts();
        _showToast('Claimed ' + claimed.length + ' tickets', 'success');
    })
    .catch(function (err) {
        _showToast('Claim failed: ' + err.message, 'error');
    })
    .finally(function () {
        _setBusy(false);
    });
}

// ── Unlock My Locks ───────────────────────────────────────────────────

function bulkUnlockMine() {
    if (_bulkBusy || !_bulkUserId) return;

    var myTickets = [];
    for (var tid in _bulkLocks) {
        if (_bulkLocks[tid] === _bulkUserId) myTickets.push(tid);
    }
    if (myTickets.length === 0) {
        _showToast('No locks to release', 'info');
        return;
    }

    _setBusy(true);

    fetch('/bulk/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket_ids: myTickets, user_id: _bulkUserId })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var unlocked = data.unlocked || [];
        unlocked.forEach(function (tid) {
            delete _bulkLocks[tid];
        });
        _bulkSelected.clear();
        _bulkRecs = {};
        _bulkOverrides = {};
        _renderQueue();
        _updateCounts();
        _showToast('Released ' + unlocked.length + ' locks', 'success');
    })
    .catch(function (err) {
        _showToast('Unlock failed: ' + err.message, 'error');
    })
    .finally(function () {
        _setBusy(false);
    });
}

// ── Get Recommendations ───────────────────────────────────────────────

// Track which tickets are pending/processing for visual feedback
var _bulkRecPending = new Set();   // ticket IDs waiting to be processed
var _bulkRecProcessing = null;     // ticket ID currently being processed

function bulkGetRecommendations() {
    if (_bulkBusy || !_bulkUserId) return;

    // Get selected tickets that are locked by me
    var ticketIds = _getMySelectedTicketIds();
    if (ticketIds.length === 0) {
        _showToast('Select tickets you have locked first', 'warning');
        return;
    }

    _setBusy(true);
    _showToast('Generating recommendations for ' + ticketIds.length + ' tickets…', 'info');

    // Mark all selected tickets as pending (visual feedback)
    _bulkRecPending = new Set(ticketIds);
    _bulkRecProcessing = null;
    _renderQueue();

    var topKDocs = parseInt(document.getElementById('bulkTopKDocs').value) || 5;
    var topKTickets = parseInt(document.getElementById('bulkTopKTickets').value) || 5;
    var maxTokens = parseInt(document.getElementById('bulkMaxTokens').value) || 2048;

    fetch('/bulk/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            ticket_ids: ticketIds,
            user_id: _bulkUserId,
        })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var recs = data.recommendations || [];
        recs.forEach(function (rec) {
            _bulkRecs[rec.ticket_id] = rec;
            // Pre-populate overrides with AI recommendation
            if (rec.success && rec.recommendation) {
                _bulkOverrides[rec.ticket_id] = {
                    tier_queue_guid: rec.recommendation.support_group_guid,
                    tier_queue_name: rec.recommendation.support_group_name,
                    priority: rec.recommendation.priority
                };
            }
        });
        // Clear pending/processing state
        _bulkRecPending.clear();
        _bulkRecProcessing = null;
        _renderQueue();
        _showToast('Recommendations: ' + data.total + ' generated, ' + data.failed + ' failed', 'success');
    })
    .catch(function (err) {
        // Clear pending/processing state on error
        _bulkRecPending.clear();
        _bulkRecProcessing = null;
        _renderQueue();
        _showToast('Recommendations failed: ' + err.message, 'error');
    })
    .finally(function () {
        _setBusy(false);
    });
}

// ── Assign Selected ───────────────────────────────────────────────────

function bulkAssignSelected() {
    if (_bulkBusy || !_bulkUserId) return;

    var assignments = [];
    _bulkSelected.forEach(function (tid) {
        if (_bulkLocks[tid] !== _bulkUserId) return;

        var ticket = _findTicket(tid);
        if (!ticket) return;

        var override = _bulkOverrides[tid];
        if (!override || !override.tier_queue_guid) {
            // Skip tickets without a recommendation/override
            return;
        }

        assignments.push({
            ticket_id: tid,
            entity_id: ticket.entity_id,
            tier_queue_guid: override.tier_queue_guid,
            tier_queue_name: override.tier_queue_name || '',
            priority: override.priority || null
        });
    });

    if (assignments.length === 0) {
        _showToast('No tickets ready to assign. Get recommendations first.', 'warning');
        return;
    }

    _setBusy(true);
    _showToast('Assigning ' + assignments.length + ' tickets…', 'info');

    fetch('/bulk/assign', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assignments: assignments, user_id: _bulkUserId })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var results = data.results || [];
        var successIds = [];
        var failMsgs = [];

        results.forEach(function (r) {
            if (r.success) {
                successIds.push(r.ticket_id);
                // Remove from local state
                _bulkQueue = _bulkQueue.filter(function (t) { return t.id !== r.ticket_id; });
                _bulkSelected.delete(r.ticket_id);
                delete _bulkLocks[r.ticket_id];
                delete _bulkRecs[r.ticket_id];
                delete _bulkOverrides[r.ticket_id];
            } else {
                failMsgs.push(r.ticket_id + ': ' + (r.error || 'Unknown error'));
            }
        });

        _renderQueue();
        _updateCounts();

        if (successIds.length > 0) {
            _showToast('Assigned ' + successIds.length + ' tickets ✓', 'success');
        }
        if (failMsgs.length > 0) {
            _showToast('Failed: ' + failMsgs.join('; '), 'error');
        }
    })
    .catch(function (err) {
        _showToast('Assignment failed: ' + err.message, 'error');
    })
    .finally(function () {
        _setBusy(false);
    });
}

// ── Settings Toggle ───────────────────────────────────────────────────

function bulkToggleSettings() {
    var panel = document.getElementById('bulkSettingsPanel');
    panel.style.display = panel.style.display === 'none' ? '' : 'none';
}

// ── Recommendation Progress Styling ───────────────────────────────────

/**
 * Apply rec-pending / rec-processing CSS classes to ticket rows
 * without doing a full re-render. This is called from WebSocket
 * event handlers for smooth, incremental visual updates.
 */
function _applyRecProgressStyles() {
    var rows = document.querySelectorAll('tr[data-ticket-id]');
    rows.forEach(function (row) {
        var tid = row.getAttribute('data-ticket-id');

        // Remove previous progress classes
        row.classList.remove('rec-pending', 'rec-processing');

        if (_bulkRecProcessing === tid) {
            row.classList.add('rec-processing');
            // Ensure the processing row is visible (scroll into view)
            row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else if (_bulkRecPending.has(tid)) {
            row.classList.add('rec-pending');
        }

        // Update the inline status indicator in the lock column
        var lockCell = row.querySelector('.col-lock');
        if (lockCell) {
            // Find or create the rec-status indicator
            var indicator = lockCell.querySelector('.rec-status-indicator');
            if (_bulkRecProcessing === tid) {
                if (!indicator) {
                    indicator = document.createElement('span');
                    indicator.className = 'rec-status-indicator';
                    lockCell.appendChild(indicator);
                }
                indicator.innerHTML = '<span class="rec-spinner"></span>';
                indicator.title = 'Generating recommendation…';
            } else if (_bulkRecPending.has(tid)) {
                if (!indicator) {
                    indicator = document.createElement('span');
                    indicator.className = 'rec-status-indicator';
                    lockCell.appendChild(indicator);
                }
                indicator.innerHTML = '⏳';
                indicator.title = 'Waiting for recommendation…';
            } else {
                // Remove indicator if no longer pending/processing
                if (indicator) indicator.remove();
            }
        }
    });
}

// ── Incremental Rendering (Streaming) ─────────────────────────────────

/**
 * Append a single ticket row to the table during streaming load.
 * Creates the table structure on the first ticket, then appends rows.
 * During streaming, lock state is not yet available, so all tickets
 * appear as unlocked. A full re-render happens on queue_loading_complete.
 */
function _appendTicketToTable(ticket) {
    var container = document.getElementById('bulkQueueBody');

    // On first ticket, create the table structure
    var table = container.querySelector('.bulk-table');
    if (!table) {
        // Remove loading indicator
        var loadingEl = container.querySelector('.bulk-loading-stream');
        // Keep the loading indicator but move it above the table
        var html = '<table class="bulk-table bulk-table-streaming"><thead><tr>' +
            '<th class="col-expand"></th>' +
            '<th class="col-checkbox"></th>' +
            '<th class="col-lock">Lock</th>' +
            '<th>Ticket ID</th>' +
            '<th>Type</th>' +
            '<th>Title</th>' +
            '<th>Status</th>' +
            '<th>Priority</th>' +
            '<th>Affected User</th>' +
            '<th>Created</th>' +
            '</tr></thead><tbody id="bulkStreamTbody"></tbody></table>';

        if (loadingEl) {
            // Insert table after the loading indicator
            loadingEl.insertAdjacentHTML('afterend', html);
        } else {
            container.innerHTML = html;
        }
    }

    var tbody = document.getElementById('bulkStreamTbody');
    if (!tbody) return;

    // Build a simple row (no lock state during streaming)
    var typeClass = ticket.ticket_type === 'incident' ? 'ir' : 'sr';
    var typeLabel = ticket.ticket_type === 'incident' ? 'IR' : 'SR';

    var rowHtml = '<tr class="bulk-stream-row" data-ticket-id="' + _escapeHtml(ticket.id) + '">' +
        '<td class="col-expand"></td>' +
        '<td class="col-checkbox"></td>' +
        '<td class="col-lock"><span class="lock-color-dot" style="background-color:' + _USER_COLOR_UNLOCKED + ';" title="Unlocked"></span></td>' +
        '<td class="ticket-id-cell">' + _escapeHtml(ticket.id) + '</td>' +
        '<td><span class="ticket-type-badge ' + typeClass + '">' + typeLabel + '</span></td>' +
        '<td class="title-cell" title="' + _escapeHtml(ticket.title || '') + '">' + _escapeHtml(ticket.title || '—') + '</td>' +
        '<td>' + _escapeHtml(ticket.status || '—') + '</td>' +
        '<td>' + _escapeHtml(String(ticket.priority || '—')) + '</td>' +
        '<td>' + _escapeHtml(ticket.affected_user || '—') + '</td>' +
        '<td class="date-cell">' + _formatDate(ticket.created_date) + '</td>' +
        '</tr>';

    tbody.insertAdjacentHTML('beforeend', rowHtml);
}

// ── Rendering ─────────────────────────────────────────────────────────

function _renderQueue() {
    var container = document.getElementById('bulkQueueBody');

    // Preserve scroll position across full re-render
    var savedScrollX = window.scrollX;
    var savedScrollY = window.scrollY;

    if (_bulkQueue.length === 0) {
        container.innerHTML =
            '<div class="bulk-empty-state">' +
                '<div class="empty-icon">✅</div>' +
                '<div class="empty-text">Queue is empty</div>' +
                '<div class="empty-hint">All tickets have been processed or the queue has no matching tickets</div>' +
            '</div>';
        window.scrollTo(savedScrollX, savedScrollY);
        return;
    }

    var html = '<table class="bulk-table"><thead><tr>' +
        '<th class="col-expand"></th>' +
        '<th class="col-checkbox"><input type="checkbox" id="bulkSelectAllCb" onchange="bulkToggleSelectAllMine(this.checked)" title="Select/deselect all my locked tickets"></th>' +
        '<th class="col-lock">Lock</th>' +
        '<th>Ticket ID</th>' +
        '<th>Type</th>' +
        '<th>Title</th>' +
        '<th>Status</th>' +
        '<th>Priority</th>' +
        '<th>Affected User</th>' +
        '<th>Created</th>' +
        '</tr></thead><tbody>';

    _bulkQueue.forEach(function (ticket, idx) {
        var lockOwner = _bulkLocks[ticket.id] || null;
        var isMyLock = lockOwner === _bulkUserId;
        var isOtherLock = lockOwner && !isMyLock;
        var isSelected = _bulkSelected.has(ticket.id);
        var hasRec = !!_bulkRecs[ticket.id];

        // Zebra striping class for visual row grouping
        var zebraClass = (idx % 2 === 0) ? 'bulk-row-even' : 'bulk-row-odd';

        var rowClass = zebraClass;
        if (isMyLock) rowClass += ' locked-by-me';
        else if (isOtherLock) rowClass += ' locked-by-other';

        // Add recommendation progress classes
        if (_bulkRecProcessing === ticket.id) rowClass += ' rec-processing';
        else if (_bulkRecPending.has(ticket.id)) rowClass += ' rec-pending';

        html += '<tr class="' + rowClass + '" data-ticket-id="' + _escapeHtml(ticket.id) + '">';

        // Expand button
        html += '<td class="col-expand">' +
            '<button class="bulk-expand-btn" onclick="bulkToggleExpand(this)" title="Show details">▶</button>' +
            '</td>';

        // Checkbox — shown on ALL tickets
        var isPending = _bulkLockPending.has(ticket.id);
        html += '<td class="col-checkbox">';
        if (isPending) {
            // Lock/unlock in progress — show spinner
            html += '<span class="bulk-checkbox-pending" title="Locking…"><span class="rec-spinner"></span></span>';
        } else if (isOtherLock) {
            // Locked by another user — disabled checkbox with their color
            var otherColor = _bulkUserColors[lockOwner] || '#e74c3c';
            html += '<input type="checkbox" disabled title="Locked by ' + _escapeHtml(lockOwner) + '"' +
                ' style="accent-color:' + otherColor + '; opacity:0.5; cursor:not-allowed;">';
        } else {
            // Unlocked or my lock — enabled checkbox
            html += '<input type="checkbox" ' + (isSelected ? 'checked' : '') +
                ' onchange="bulkToggleSelect(\'' + _escapeHtml(ticket.id) + '\', this.checked)">';
        }
        html += '</td>';

        // Lock indicator
        html += '<td class="col-lock">';
        if (isMyLock) {
            html += '<span class="lock-color-dot" style="background-color:' + _USER_COLOR_SELF + ';" title="Locked by you"></span>';
        } else if (isOtherLock) {
            var ownerColor = _bulkUserColors[lockOwner] || '#e74c3c';
            html += '<span class="lock-color-dot" style="background-color:' + ownerColor + ';" title="Locked by ' + _escapeHtml(lockOwner) + '"></span>';
        } else {
            html += '<span class="lock-color-dot" style="background-color:' + _USER_COLOR_UNLOCKED + ';" title="Unlocked"></span>';
        }
        html += '</td>';

        // Ticket ID
        html += '<td class="ticket-id-cell">' + _escapeHtml(ticket.id) + '</td>';

        // Type
        var typeClass = ticket.ticket_type === 'incident' ? 'ir' : 'sr';
        var typeLabel = ticket.ticket_type === 'incident' ? 'IR' : 'SR';
        html += '<td><span class="ticket-type-badge ' + typeClass + '">' + typeLabel + '</span></td>';

        // Title
        html += '<td class="title-cell" title="' + _escapeHtml(ticket.title || '') + '">' +
            _escapeHtml(ticket.title || '—') + '</td>';

        // Status
        html += '<td>' + _escapeHtml(ticket.status || '—') + '</td>';

        // Priority
        html += '<td>' + _escapeHtml(String(ticket.priority || '—')) + '</td>';

        // Affected User
        html += '<td>' + _escapeHtml(ticket.affected_user || '—') + '</td>';

        // Created Date
        html += '<td class="date-cell">' + _formatDate(ticket.created_date) + '</td>';

        html += '</tr>';

        // Detail row (hidden by default)
        html += _renderDetailRow(ticket);

        // Recommendation row (if exists)
        if (hasRec) {
            html += _renderRecRow(ticket.id);
        }
    });

    html += '</tbody></table>';
    container.innerHTML = html;

    // Restore scroll position after DOM replacement
    window.scrollTo(savedScrollX, savedScrollY);
}

function _renderDetailRow(ticket) {
    var colspan = 10;
    var isMyLock = _bulkLocks[ticket.id] === _bulkUserId;
    var html = '<tr class="bulk-detail-row" style="display:none;">' +
        '<td colspan="' + colspan + '">' +
        '<div class="assignment-ticket-body">';

    // Section: Affected User
    if (ticket.affected_user) {
        html += '<div class="ticket-section">' +
            '<div class="ticket-section-header">👤 Affected User</div>' +
            '<div class="ticket-section-grid">';
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Name:</span>' +
            '<span class="ticket-field-value ticket-field-highlight">' + _escapeHtml(ticket.affected_user) + '</span>' +
            '</div>';
        if (ticket.assigned_user) {
            html += '<div class="ticket-field">' +
                '<span class="ticket-field-label">Assigned To:</span>' +
                '<span class="ticket-field-value">' + _escapeHtml(ticket.assigned_user) + '</span>' +
                '</div>';
        }
        html += '</div></div>';
    }

    // Section: Location
    if (ticket.location) {
        html += '<div class="ticket-section">' +
            '<div class="ticket-section-header">📍 Location</div>' +
            '<div class="ticket-section-grid">';
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Location:</span>' +
            '<span class="ticket-field-value ticket-field-location">' + _escapeHtml(ticket.location) + '</span>' +
            '</div>';
        html += '</div></div>';
    }

    // Section: Status & Routing
    html += '<div class="ticket-section">' +
        '<div class="ticket-section-header">📊 Status & Routing</div>' +
        '<div class="ticket-section-grid">';

    // Type
    html += '<div class="ticket-field">' +
        '<span class="ticket-field-label">Type:</span>' +
        '<span class="ticket-field-value">';
    if (ticket.ticket_type === 'incident') {
        html += '<span class="ticket-type-badge ticket-type-ir">Incident</span>';
    } else {
        html += '<span class="ticket-type-badge ticket-type-sr">Service Request</span>';
    }
    html += '</span></div>';

    // Status
    if (ticket.status) {
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Status:</span>' +
            '<span class="ticket-field-value"><span class="ticket-status-badge">' + _escapeHtml(ticket.status) + '</span></span>' +
            '</div>';
    }

    // Priority
    if (ticket.priority) {
        var priClass = 'ticket-priority-badge priority-' + String(ticket.priority).toLowerCase();
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Priority:</span>' +
            '<span class="ticket-field-value"><span class="' + priClass + '">' + _escapeHtml(String(ticket.priority)) + '</span></span>' +
            '</div>';
    }

    // Current Group
    if (ticket.tier_queue) {
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Current Group:</span>' +
            '<span class="ticket-field-value ticket-field-group">' + _escapeHtml(ticket.tier_queue) + '</span>' +
            '</div>';
    }

    html += '</div></div>';  // close ticket-section-grid + ticket-section

    // Section: Creation Info
    if (ticket.created_date) {
        html += '<div class="ticket-section">' +
            '<div class="ticket-section-header">🕐 Creation Info</div>' +
            '<div class="ticket-section-grid">';
        html += '<div class="ticket-field">' +
            '<span class="ticket-field-label">Created:</span>' +
            '<span class="ticket-field-value">' + _formatDate(ticket.created_date) + '</span>' +
            '</div>';
        html += '</div></div>';
    }

    // Section: Title
    if (ticket.title) {
        html += '<div class="ticket-section">' +
            '<div class="ticket-section-header">📝 Title</div>' +
            '<div class="ticket-title-text">' + _escapeHtml(ticket.title) + '</div>' +
            '</div>';
    }

    // Section: Description
    if (ticket.description) {
        html += '<div class="ticket-section">' +
            '<div class="ticket-section-header">📄 Description</div>' +
            '<div class="ticket-description-full">' + _escapeHtml(ticket.description) + '</div>' +
            '</div>';
    }

    html += '</div>';  // close assignment-ticket-body

    // Manual assign form — only for tickets locked by the current user
    if (isMyLock) {
        var override = _bulkOverrides[ticket.id] || {};
        var isIR = ticket.ticket_type === 'incident';
        var sgValue = override.tier_queue_name || '';
        var priValue = override.priority != null ? String(override.priority) : '';

        html += '<div class="manual-assign-form" data-ticket-id="' + _escapeHtml(ticket.id) + '"' +
            ' data-ticket-type="' + _escapeHtml(ticket.ticket_type) + '"' +
            ' data-entity-id="' + _escapeHtml(ticket.entity_id) + '">';
        html += '<h4 class="manual-assign-title">✏️ Manual Assignment</h4>';
        html += '<div class="manual-assign-fields">';

        // Support Group dropdown with search
        html += '<div class="manual-assign-field">' +
            '<label>Support Group</label>' +
            '<div class="sg-dropdown-wrapper" id="sgWrapper_' + _escapeHtml(ticket.id) + '">' +
            '<button type="button" class="sg-dropdown-toggle" ' +
            'id="sgToggle_' + _escapeHtml(ticket.id) + '" ' +
            'onclick="bulkSgToggle(\'' + _escapeHtml(ticket.id) + '\', \'' + _escapeHtml(ticket.ticket_type) + '\')">' +
            '<span class="sg-dropdown-toggle-text">' + (sgValue ? _escapeHtml(sgValue) : 'Select support group…') + '</span>' +
            '<span class="sg-dropdown-chevron">▼</span>' +
            '</button>' +
            '<input type="hidden" class="sg-dropdown-guid" ' +
            'id="sgGuid_' + _escapeHtml(ticket.id) + '" ' +
            'value="' + _escapeHtml(override.tier_queue_guid || '') + '">' +
            '<div class="sg-dropdown-panel" id="sgPanel_' + _escapeHtml(ticket.id) + '">' +
            '<input type="text" class="sg-dropdown-search" ' +
            'id="sgSearch_' + _escapeHtml(ticket.id) + '" ' +
            'placeholder="Search support groups…" ' +
            'autocomplete="off" ' +
            'oninput="bulkSgFilter(\'' + _escapeHtml(ticket.id) + '\')">' +
            '<div class="sg-dropdown-list" id="sgList_' + _escapeHtml(ticket.id) + '"></div>' +
            '</div>' +
            '</div>' +
            '</div>';

        // Priority dropdown
        html += '<div class="manual-assign-field">' +
            '<label>Priority</label>' +
            '<select class="manual-assign-priority" id="priSelect_' + _escapeHtml(ticket.id) + '" ' +
            'onchange="bulkPriorityChange(\'' + _escapeHtml(ticket.id) + '\', this.value)">';
        html += '<option value="">— Keep current —</option>';

        if (isIR) {
            // IR: numeric priorities 1-3
            for (var p = 1; p <= 3; p++) {
                var sel = (priValue === String(p)) ? ' selected' : '';
                html += '<option value="' + p + '"' + sel + '>' + p + '</option>';
            }
        } else {
            // SR: named priorities
            var srPriorities = ['Low', 'Medium', 'High', 'Immediate'];
            srPriorities.forEach(function (pri) {
                var sel = (priValue === pri) ? ' selected' : '';
                html += '<option value="' + pri + '"' + sel + '>' + pri + '</option>';
            });
        }

        html += '</select></div>';

        // Assign Now button
        html += '<div class="manual-assign-field manual-assign-actions">' +
            '<button class="btn btn-success btn-sm" ' +
            'onclick="bulkManualAssign(\'' + _escapeHtml(ticket.id) + '\')" ' +
            'id="btnManualAssign_' + _escapeHtml(ticket.id) + '" ' +
            'title="Assign this ticket immediately">' +
            '⚡ Assign Now</button>' +
            '</div>';

        html += '</div>';  // close manual-assign-fields
        html += '</div>';  // close manual-assign-form
    }

    html += '</td></tr>';
    return html;
}

function bulkToggleExpand(btn) {
    var row = btn.closest('tr');
    var detailRow = row.nextElementSibling;
    if (detailRow && detailRow.classList.contains('bulk-detail-row')) {
        var isVisible = detailRow.style.display !== 'none';
        detailRow.style.display = isVisible ? 'none' : 'table-row';
        btn.textContent = isVisible ? '▶' : '▼';
        btn.classList.toggle('expanded', !isVisible);
    }
}

function _renderRecRow(ticketId) {
    var rec = _bulkRecs[ticketId];
    if (!rec) return '';

    var override = _bulkOverrides[ticketId] || {};
    var colspan = 10;

    var html = '<tr class="bulk-rec-row" data-rec-for="' + _escapeHtml(ticketId) + '">' +
        '<td colspan="' + colspan + '">' +
        '<div class="bulk-rec-panel">';

    html += '<div class="bulk-rec-header">' +
        '<h4>🤖 AI Recommendation for ' + _escapeHtml(ticketId) + '</h4>';

    if (rec.success) {
        html += '<span class="bulk-rec-badge success">✓ Generated</span>';
    } else {
        html += '<span class="bulk-rec-badge error">✗ Failed</span>';
    }
    html += '</div>';

    if (rec.success && rec.recommendation) {
        var r = rec.recommendation;
        var confPct = Math.round((r.confidence || 0) * 100);
        var method = r.method || 'classifier';

        // ── Top Recommendation with confidence ──
        html += '<div class="bulk-rec-top">';
        html += '<div class="bulk-rec-top-group">';
        html += '<span class="bulk-rec-label">Top Prediction:</span> ';
        html += '<strong>' + _escapeHtml(r.support_group_name) + '</strong>';
        html += ' <span class="bulk-rec-method-badge ' + _escapeHtml(method) + '">' + _escapeHtml(method) + '</span>';
        html += '</div>';
        html += '<div class="bulk-rec-confidence">';
        html += '<div class="bulk-rec-conf-bar-container">';
        html += '<div class="bulk-rec-conf-bar" style="width:' + confPct + '%;"></div>';
        html += '</div>';
        html += '<span class="bulk-rec-conf-text">' + confPct + '%</span>';
        html += '</div>';
        html += '</div>';

        // ── Alternatives table ──
        var alts = r.alternatives || [];
        if (alts.length > 0) {
            html += '<div class="bulk-rec-alternatives">';
            html += '<table class="bulk-rec-alt-table">';
            html += '<thead><tr><th>Alternative</th><th>Confidence</th><th></th></tr></thead>';
            html += '<tbody>';
            for (var i = 0; i < alts.length; i++) {
                var alt = alts[i];
                var altPct = Math.round((alt.confidence || 0) * 100);
                html += '<tr class="bulk-rec-alt-row">';
                html += '<td>' + _escapeHtml(alt.support_group) + '</td>';
                html += '<td><div class="bulk-rec-conf-bar-container small">' +
                    '<div class="bulk-rec-conf-bar" style="width:' + altPct + '%;"></div>' +
                    '</div><span class="bulk-rec-conf-text">' + altPct + '%</span></td>';
                html += '<td><button class="btn-alt-select" onclick="bulkSelectAlternative(\'' +
                    _escapeHtml(ticketId) + '\', ' + i + '); event.stopPropagation();">Use ↗</button></td>';
                html += '</tr>';
            }
            html += '</tbody></table>';
            html += '</div>';
        }

        // ── Editable assignment fields (no GUID visible) ──
        html += '<div class="bulk-rec-grid">';

        // Support Group (editable — hidden data-guid attribute for API)
        html += '<div class="bulk-rec-field">' +
            '<label>Assign To</label>' +
            '<input type="text" value="' + _escapeHtml(override.tier_queue_name || r.support_group_name) + '"' +
            ' data-guid="' + _escapeHtml(override.tier_queue_guid || r.support_group_guid) + '"' +
            ' onchange="bulkUpdateOverride(\'' + _escapeHtml(ticketId) + '\', \'tier_queue_name\', this.value)"' +
            ' placeholder="Support group name">' +
            '</div>';

        // Priority (editable)
        html += '<div class="bulk-rec-field">' +
            '<label>Priority</label>' +
            '<input type="text" value="' + _escapeHtml(String(override.priority || r.priority || '')) + '"' +
            ' onchange="bulkUpdateOverride(\'' + _escapeHtml(ticketId) + '\', \'priority\', this.value)"' +
            ' placeholder="Priority">' +
            '</div>';

        html += '</div>';

        // Rationale
        if (r.rationale) {
            html += '<div class="bulk-rec-rationale">' + _escapeHtml(r.rationale) + '</div>';
        }
    } else {
        html += '<div class="bulk-rec-error">' +
            _escapeHtml(rec.error || 'Recommendation generation failed') + '</div>';
    }

    html += '</div></td></tr>';
    return html;
}

/**
 * Select an alternative recommendation for a ticket.
 * Updates the override with the alternative's support group name and GUID.
 */
function bulkSelectAlternative(ticketId, altIndex) {
    var rec = _bulkRecs[ticketId];
    if (!rec || !rec.recommendation) return;
    var alts = rec.recommendation.alternatives || [];
    if (altIndex < 0 || altIndex >= alts.length) return;

    var alt = alts[altIndex];
    // Look up the GUID from the support groups list
    var ticket = _findTicket(ticketId);
    var ticketType = ticket ? ticket.ticket_type : 'incident';
    var groups = _bulkSupportGroups[ticketType] || [];
    var guid = '';
    for (var i = 0; i < groups.length; i++) {
        if (groups[i].name === alt.support_group) {
            guid = groups[i].guid;
            break;
        }
    }

    // Update overrides
    bulkUpdateOverride(ticketId, 'tier_queue_name', alt.support_group);
    if (guid) {
        bulkUpdateOverride(ticketId, 'tier_queue_guid', guid);
    }

    // Re-render the rec row
    var recRow = document.querySelector('tr[data-rec-for="' + ticketId + '"]');
    if (recRow) {
        var tmp = document.createElement('tbody');
        tmp.innerHTML = _renderRecRow(ticketId);
        recRow.replaceWith(tmp.firstElementChild);
    }
}

function _updateTicketRow(ticketId) {
    // Re-render the full queue for simplicity (individual row updates are complex with rec rows)
    _renderQueue();
}

// ── Selection ─────────────────────────────────────────────────────────

function bulkToggleSelect(ticketId, checked) {
    var lockOwner = _bulkLocks[ticketId] || null;
    var isMyLock = lockOwner === _bulkUserId;

    if (checked) {
        if (isMyLock) {
            // Already locked by me — just select
            _bulkSelected.add(ticketId);
            _updateCounts();
            _updateMasterCheckbox();
            _updateRowHighlight(ticketId, true);
        } else if (!lockOwner) {
            // Unlocked — need to lock first, then select
            _lockAndSelect(ticketId);
        }
        // If locked by someone else, do nothing (checkbox should be disabled)
    } else {
        if (isMyLock) {
            // My lock — unlock and deselect
            _bulkSelected.delete(ticketId);
            _unlockAndDeselect(ticketId);
        } else {
            // Just deselect (shouldn't normally happen)
            _bulkSelected.delete(ticketId);
            _updateCounts();
            _updateMasterCheckbox();
            _updateRowHighlight(ticketId, false);
        }
    }
}

/**
 * Lock a ticket via REST, then select it on success.
 * Shows a pending spinner while the lock is being acquired.
 */
function _lockAndSelect(ticketId) {
    if (_bulkLockPending.has(ticketId)) return;  // Already in-flight
    _bulkLockPending.add(ticketId);
    _renderCheckboxCell(ticketId);  // Show spinner

    fetch('/bulk/lock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket_ids: [ticketId], user_id: _bulkUserId })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var locked = data.locked || [];
        _bulkLockPending.delete(ticketId);
        if (locked.indexOf(ticketId) !== -1) {
            // Lock acquired
            _bulkLocks[ticketId] = _bulkUserId;
            _bulkSelected.add(ticketId);
            _showToast('Locked ' + ticketId, 'success');
        } else {
            // Lock failed (someone else got it first)
            _showToast('Could not lock ' + ticketId + ' — already locked by another user', 'warning');
        }
        _renderQueue();
        _updateCounts();
        _updateMasterCheckbox();
    })
    .catch(function (err) {
        _bulkLockPending.delete(ticketId);
        _showToast('Lock failed for ' + ticketId + ': ' + err.message, 'error');
        _renderQueue();
        _updateCounts();
    });
}

/**
 * Unlock a ticket via REST when the user unchecks it.
 */
function _unlockAndDeselect(ticketId) {
    if (_bulkLockPending.has(ticketId)) return;
    _bulkLockPending.add(ticketId);
    _renderCheckboxCell(ticketId);  // Show spinner

    fetch('/bulk/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket_ids: [ticketId], user_id: _bulkUserId })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var unlocked = data.unlocked || [];
        _bulkLockPending.delete(ticketId);
        if (unlocked.indexOf(ticketId) !== -1) {
            delete _bulkLocks[ticketId];
            _bulkSelected.delete(ticketId);
            delete _bulkRecs[ticketId];
            delete _bulkOverrides[ticketId];
            _showToast('Unlocked ' + ticketId, 'info');
        }
        _renderQueue();
        _updateCounts();
        _updateMasterCheckbox();
    })
    .catch(function (err) {
        _bulkLockPending.delete(ticketId);
        _showToast('Unlock failed for ' + ticketId + ': ' + err.message, 'error');
        _renderQueue();
        _updateCounts();
    });
}

/**
 * Re-render just the checkbox cell for a specific ticket (for pending state).
 */
function _renderCheckboxCell(ticketId) {
    var row = document.querySelector('tr[data-ticket-id="' + ticketId + '"]');
    if (!row) return;
    var cell = row.querySelector('.col-checkbox');
    if (!cell) return;

    if (_bulkLockPending.has(ticketId)) {
        cell.innerHTML = '<span class="bulk-checkbox-pending" title="Processing…"><span class="rec-spinner"></span></span>';
    }
}

/**
 * Toggle highlight class on a ticket row.
 */
function _updateRowHighlight(ticketId, selected) {
    var row = document.querySelector('tr[data-ticket-id="' + ticketId + '"]');
    if (row) {
        if (selected) {
            row.classList.add('bulk-row-selected');
        } else {
            row.classList.remove('bulk-row-selected');
        }
    }
}

/**
 * Select All My Locked — master checkbox in table header.
 * Selects or deselects all tickets currently locked by the current user.
 */
function bulkToggleSelectAllMine(checked) {
    _bulkQueue.forEach(function (ticket) {
        var lockOwner = _bulkLocks[ticket.id] || null;
        if (lockOwner === _bulkUserId) {
            if (checked) {
                _bulkSelected.add(ticket.id);
            } else {
                _bulkSelected.delete(ticket.id);
            }
        }
    });
    _renderQueue();
    _updateCounts();
    _updateMasterCheckbox();
}

/**
 * Update the master checkbox state (checked, unchecked, or indeterminate)
 * based on how many of the user's locked tickets are selected.
 */
function _updateMasterCheckbox() {
    var cb = document.getElementById('bulkSelectAllCb');
    if (!cb) return;

    var myLockedCount = 0;
    var mySelectedCount = 0;
    _bulkQueue.forEach(function (ticket) {
        if (_bulkLocks[ticket.id] === _bulkUserId) {
            myLockedCount++;
            if (_bulkSelected.has(ticket.id)) mySelectedCount++;
        }
    });

    if (myLockedCount === 0) {
        cb.checked = false;
        cb.indeterminate = false;
        cb.disabled = true;
    } else if (mySelectedCount === 0) {
        cb.checked = false;
        cb.indeterminate = false;
        cb.disabled = false;
    } else if (mySelectedCount === myLockedCount) {
        cb.checked = true;
        cb.indeterminate = false;
        cb.disabled = false;
    } else {
        cb.checked = false;
        cb.indeterminate = true;
        cb.disabled = false;
    }
}


// ── Override Management ───────────────────────────────────────────────

function bulkUpdateOverride(ticketId, field, value) {
    if (!_bulkOverrides[ticketId]) {
        _bulkOverrides[ticketId] = {};
    }
    _bulkOverrides[ticketId][field] = value;

    // Re-evaluate Assign button state when overrides change
    _updateCounts();
}

// ── Helpers ───────────────────────────────────────────────────────────

function _getMySelectedTicketIds() {
    var ids = [];
    _bulkSelected.forEach(function (tid) {
        if (_bulkLocks[tid] === _bulkUserId) {
            ids.push(tid);
        }
    });
    return ids;
}

function _findTicket(ticketId) {
    for (var i = 0; i < _bulkQueue.length; i++) {
        if (_bulkQueue[i].id === ticketId) return _bulkQueue[i];
    }
    return null;
}

function _updateCounts() {
    document.getElementById('bulkQueueCount').textContent = _bulkQueue.length;

    var myLocks = 0;
    for (var tid in _bulkLocks) {
        if (_bulkLocks[tid] === _bulkUserId) myLocks++;
    }
    document.getElementById('bulkMyLockCount').textContent = myLocks;
    document.getElementById('bulkSelectedCount').textContent = _bulkSelected.size;

    // Enable/disable buttons based on state
    var mySelectedIds = _getMySelectedTicketIds();
    var hasMySelected = mySelectedIds.length > 0;
    document.getElementById('btnRecommend').disabled = !hasMySelected;

    // Assign button requires at least one selected ticket with a support group assigned
    var hasAssignable = mySelectedIds.some(function (tid) {
        var override = _bulkOverrides[tid];
        return override && override.tier_queue_guid;
    });
    document.getElementById('btnAssign').disabled = !hasAssignable;

    // Update master checkbox state
    _updateMasterCheckbox();
}

function _setBusy(busy) {
    _bulkBusy = busy;
    var btns = ['btnRefresh', 'btnClaim', 'btnRecommend', 'btnAssign', 'btnUnlockMine'];
    btns.forEach(function (id) {
        var btn = document.getElementById(id);
        if (btn) btn.disabled = busy;
    });
    // Re-enable state-dependent buttons after busy clears
    if (!busy) _updateCounts();
}

function _formatDate(dateStr) {
    if (!dateStr) return '—';
    try {
        var d = new Date(dateStr);
        if (isNaN(d.getTime())) return dateStr;
        var month = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        var year = d.getFullYear();
        var hours = String(d.getHours()).padStart(2, '0');
        var mins = String(d.getMinutes()).padStart(2, '0');
        return hours + ':' + mins + ' ' + month + '/' + day + '/' + year;
    } catch (e) {
        return dateStr;
    }
}

function _escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ── Support Group Autocomplete ────────────────────────────────────────

/**
 * Load support groups for a ticket type (cached after first load).
 */
function _loadSupportGroups(ticketType, callback) {
    if (_bulkSupportGroups[ticketType]) {
        if (callback) callback(_bulkSupportGroups[ticketType]);
        return;
    }
    if (_bulkSgLoading[ticketType]) {
        // Already loading — retry after a short delay
        setTimeout(function () { _loadSupportGroups(ticketType, callback); }, 200);
        return;
    }

    _bulkSgLoading[ticketType] = true;

    fetch('/bulk/support-groups?ticket_type=' + encodeURIComponent(ticketType))
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function (groups) {
            _bulkSupportGroups[ticketType] = groups;
            _bulkSgLoading[ticketType] = false;
            if (callback) callback(groups);
        })
        .catch(function (err) {
            _bulkSgLoading[ticketType] = false;
            console.error('Failed to load support groups:', err);
            _showToast('Failed to load support groups: ' + err.message, 'error');
        });
}

/**
 * Toggle the support group dropdown panel open/closed.
 */
function bulkSgToggle(ticketId, ticketType) {
    var panel = document.getElementById('sgPanel_' + ticketId);
    if (!panel) return;

    var isOpen = panel.classList.contains('sg-dropdown-panel-open');

    // Close all other open panels first
    document.querySelectorAll('.sg-dropdown-panel-open').forEach(function (p) {
        p.classList.remove('sg-dropdown-panel-open');
    });

    if (isOpen) {
        // Was open, now closed
        return;
    }

    // Open this panel
    panel.classList.add('sg-dropdown-panel-open');
    // Clear any inline display style (set by app.js global handler) so the
    // CSS class .sg-dropdown-panel-open { display: flex } can take effect.
    panel.style.removeProperty('display');

    // Load groups and populate list
    _loadSupportGroups(ticketType, function () {
        bulkSgFilter(ticketId);
        // Focus the search input
        var searchInput = document.getElementById('sgSearch_' + ticketId);
        if (searchInput) {
            searchInput.value = '';
            searchInput.focus();
        }
    });
}

/**
 * Filter the support group list based on the search input value.
 * Splits the query into keywords and matches all of them (AND logic).
 */
function bulkSgFilter(ticketId) {
    var searchInput = document.getElementById('sgSearch_' + ticketId);
    var listContainer = document.getElementById('sgList_' + ticketId);
    if (!searchInput || !listContainer) return;

    var ticket = _findTicket(ticketId);
    if (!ticket) return;

    var groups = _bulkSupportGroups[ticket.ticket_type] || [];
    var rawQuery = searchInput.value.toLowerCase().trim();

    // Split query into keywords for AND matching
    var keywords = rawQuery.length > 0 ? rawQuery.split(/\s+/) : [];

    // Filter groups — all keywords must match (substring)
    var filtered = groups;
    if (keywords.length > 0) {
        filtered = groups.filter(function (g) {
            var nameLower = g.name.toLowerCase();
            return keywords.every(function (kw) {
                return nameLower.indexOf(kw) !== -1;
            });
        });
    }

    // Limit to 50 results for performance
    var shown = filtered.slice(0, 50);

    if (shown.length === 0) {
        listContainer.innerHTML = '<div class="sg-dropdown-no-match">No matching groups</div>';
        return;
    }

    var html = '';
    shown.forEach(function (g) {
        html += '<div class="sg-dropdown-option" ' +
            'onmousedown="bulkSgSelect(\'' + _escapeHtml(ticketId) + '\', ' +
            '\'' + _escapeHtml(g.guid) + '\', ' +
            '\'' + _escapeHtml(g.name) + '\')">' +
            _escapeHtml(g.name) + '</div>';
    });

    if (filtered.length > 50) {
        html += '<div class="sg-dropdown-more">… and ' + (filtered.length - 50) + ' more (refine your search)</div>';
    }

    listContainer.innerHTML = html;
}

/**
 * Select a support group from the dropdown list.
 */
function bulkSgSelect(ticketId, guid, name) {
    var toggle = document.getElementById('sgToggle_' + ticketId);
    var guidInput = document.getElementById('sgGuid_' + ticketId);
    var panel = document.getElementById('sgPanel_' + ticketId);

    // Update toggle button text
    if (toggle) {
        var textSpan = toggle.querySelector('.sg-dropdown-toggle-text');
        if (textSpan) textSpan.textContent = name;
        toggle.classList.add('sg-dropdown-has-value');
    }
    if (guidInput) guidInput.value = guid;
    // Close the panel
    if (panel) panel.classList.remove('sg-dropdown-panel-open');

    // Update override
    if (!_bulkOverrides[ticketId]) _bulkOverrides[ticketId] = {};
    _bulkOverrides[ticketId].tier_queue_name = name;
    _bulkOverrides[ticketId].tier_queue_guid = guid;

    // Re-evaluate Assign button state since a support group was assigned
    _updateCounts();
}

/**
 * Handle priority dropdown change for manual assignment.
 */
function bulkPriorityChange(ticketId, value) {
    if (!_bulkOverrides[ticketId]) _bulkOverrides[ticketId] = {};
    _bulkOverrides[ticketId].priority = value || null;
}

/**
 * Immediately assign a single ticket (manual assignment).
 */
function bulkManualAssign(ticketId) {
    var ticket = _findTicket(ticketId);
    if (!ticket) {
        _showToast('Ticket not found in queue', 'error');
        return;
    }

    var override = _bulkOverrides[ticketId];
    if (!override || !override.tier_queue_guid) {
        _showToast('Select a support group first', 'warning');
        return;
    }

    var btn = document.getElementById('btnManualAssign_' + ticketId);
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ Assigning…';
    }

    var assignment = {
        ticket_id: ticketId,
        entity_id: ticket.entity_id,
        tier_queue_guid: override.tier_queue_guid,
        tier_queue_name: override.tier_queue_name || '',
        priority: override.priority || null
    };

    fetch('/bulk/assign', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assignments: [assignment], user_id: _bulkUserId })
    })
    .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    })
    .then(function (data) {
        var results = data.results || [];
        if (results.length > 0 && results[0].success) {
            // Remove from local state
            _bulkQueue = _bulkQueue.filter(function (t) { return t.id !== ticketId; });
            _bulkSelected.delete(ticketId);
            delete _bulkLocks[ticketId];
            delete _bulkRecs[ticketId];
            delete _bulkOverrides[ticketId];
            _renderQueue();
            _updateCounts();
            _showToast('Assigned ' + ticketId + ' → ' + (override.tier_queue_name || 'group') + ' ✓', 'success');
        } else {
            var errMsg = (results[0] && results[0].error) || 'Unknown error';
            _showToast('Failed to assign ' + ticketId + ': ' + errMsg, 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = '⚡ Assign Now';
            }
        }
    })
    .catch(function (err) {
        _showToast('Assignment failed: ' + err.message, 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = '⚡ Assign Now';
        }
    });
}

// Close support group dropdown panels when clicking outside
document.addEventListener('click', function (evt) {
    if (!evt.target.closest('.sg-dropdown-wrapper')) {
        document.querySelectorAll('.sg-dropdown-panel-open').forEach(function (panel) {
            panel.classList.remove('sg-dropdown-panel-open');
        });
    }
});

// ── Presence Rendering ────────────────────────────────────────────────

/**
 * Render the list of online users in the presence bar.
 * Shows colored badges for each user, highlighting the current user.
 */
function _renderPresence() {
    var container = document.getElementById('bulkPresenceList');
    if (!container) return;

    if (_bulkOnlineUsers.length === 0) {
        container.innerHTML = '—';
        return;
    }

    var html = '';
    _bulkOnlineUsers.forEach(function (uid) {
        var isMe = uid === _bulkUserId;
        var cls = 'bulk-presence-badge' + (isMe ? ' me' : '');
        var label = isMe ? uid + ' (you)' : uid;
        var dotColor = isMe ? _USER_COLOR_SELF : (_bulkUserColors[uid] || '#e74c3c');
        var dotTitle = isMe ? 'Your locks show as ●' : 'Their locks show as ●';
        html += '<span class="' + cls + '" title="' + _escapeHtml(uid) + ' — ' + dotTitle + '">' +
            '<span class="bulk-presence-dot" style="background-color:' + dotColor + ';"></span>' +
            _escapeHtml(label) + '</span>';
    });
    container.innerHTML = html;
}

// ── Toast Notifications ───────────────────────────────────────────────

function _showToast(message, type) {
    var container = document.getElementById('bulkToastContainer');
    if (!container) return;

    var toast = document.createElement('div');
    toast.className = 'bulk-toast ' + (type || 'info');
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(function () {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(function () { toast.remove(); }, 300);
    }, 4000);
}