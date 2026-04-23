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

// Support group lists for manual assignment: ticket_type → [{name, guid}, ...]
var _bulkSupportGroups = {};
var _bulkSgLoading = {};  // ticket_type → true while loading

// ── Login ─────────────────────────────────────────────────────────────

function bulkLogin(evt) {
    if (evt) evt.preventDefault();
    var input = document.getElementById('bulkUserId');
    var userId = input.value.trim();
    if (!userId) return;

    _bulkUserId = userId;
    document.getElementById('bulkLoginScreen').style.display = 'none';
    document.getElementById('bulkMainView').style.display = '';
    document.getElementById('bulkUserDisplay').textContent = userId;

    _connectWebSocket();
    // Queue will be loaded via WebSocket streaming after connection opens
    // (see _bulkWs.onopen handler which sends load_queue action)
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
        _renderQueue();
        _updateCounts();
    } else if (event === 'lock') {
        _bulkLocks[data.ticket_id] = data.user_id;
        _updateTicketRow(data.ticket_id);
        _updateCounts();
        if (data.user_id !== _bulkUserId) {
            _showToast(data.user_id + ' locked ' + data.ticket_id, 'info');
        }
    } else if (event === 'unlock') {
        delete _bulkLocks[data.ticket_id];
        _updateTicketRow(data.ticket_id);
        _updateCounts();
        if (data.user_id !== _bulkUserId) {
            _showToast(data.user_id + ' unlocked ' + data.ticket_id, 'info');
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
            _showToast(data.user_id + ' assigned ' + data.ticket_id, 'info');
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

    fetch('/bulk/claim', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: _bulkUserId, batch_size: batchSize })
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
            top_k_docs: topKDocs,
            top_k_tickets: topKTickets,
            max_tokens: maxTokens
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
        '<td class="col-lock"><span class="lock-indicator unlocked" title="Unlocked">🟢</span></td>' +
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

    if (_bulkQueue.length === 0) {
        container.innerHTML =
            '<div class="bulk-empty-state">' +
                '<div class="empty-icon">✅</div>' +
                '<div class="empty-text">Queue is empty</div>' +
                '<div class="empty-hint">All tickets have been processed or the queue has no matching tickets</div>' +
            '</div>';
        return;
    }

    var html = '<table class="bulk-table"><thead><tr>' +
        '<th class="col-expand"></th>' +
        '<th class="col-checkbox"><input type="checkbox" onchange="bulkToggleSelectAll(this)" title="Select all my locked tickets"></th>' +
        '<th class="col-lock">Lock</th>' +
        '<th>Ticket ID</th>' +
        '<th>Type</th>' +
        '<th>Title</th>' +
        '<th>Status</th>' +
        '<th>Priority</th>' +
        '<th>Affected User</th>' +
        '<th>Created</th>' +
        '</tr></thead><tbody>';

    _bulkQueue.forEach(function (ticket) {
        var lockOwner = _bulkLocks[ticket.id] || null;
        var isMyLock = lockOwner === _bulkUserId;
        var isOtherLock = lockOwner && !isMyLock;
        var isSelected = _bulkSelected.has(ticket.id);
        var hasRec = !!_bulkRecs[ticket.id];

        var rowClass = '';
        if (isMyLock) rowClass = 'locked-by-me';
        else if (isOtherLock) rowClass = 'locked-by-other';

        // Add recommendation progress classes
        if (_bulkRecProcessing === ticket.id) rowClass += ' rec-processing';
        else if (_bulkRecPending.has(ticket.id)) rowClass += ' rec-pending';

        html += '<tr class="' + rowClass + '" data-ticket-id="' + _escapeHtml(ticket.id) + '">';

        // Expand button
        html += '<td class="col-expand">' +
            '<button class="bulk-expand-btn" onclick="bulkToggleExpand(this)" title="Show details">▶</button>' +
            '</td>';

        // Checkbox
        html += '<td class="col-checkbox">';
        if (isMyLock) {
            html += '<input type="checkbox" ' + (isSelected ? 'checked' : '') +
                ' onchange="bulkToggleSelect(\'' + _escapeHtml(ticket.id) + '\', this.checked)">';
        }
        html += '</td>';

        // Lock indicator
        html += '<td class="col-lock">';
        if (isMyLock) {
            html += '<span class="lock-indicator locked-mine" title="Locked by you">🔵</span>';
        } else if (isOtherLock) {
            html += '<span class="lock-indicator locked-other" title="Locked by ' + _escapeHtml(lockOwner) + '">🔴</span>';
        } else {
            html += '<span class="lock-indicator unlocked" title="Unlocked">🟢</span>';
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
}

function _renderDetailRow(ticket) {
    var colspan = 10;
    var isMyLock = _bulkLocks[ticket.id] === _bulkUserId;
    var html = '<tr class="bulk-detail-row" style="display:none;">' +
        '<td colspan="' + colspan + '">' +
        '<div class="bulk-detail-grid">';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Title</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.title || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Status</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.status || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Priority</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(String(ticket.priority || '—')) + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Location</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.location || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Tier Queue</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.tier_queue || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Affected User</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.affected_user || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Assigned User</span>' +
        '<span class="bulk-detail-value">' + _escapeHtml(ticket.assigned_user || '—') + '</span>' +
        '</div>';

    html += '<div class="bulk-detail-item">' +
        '<span class="bulk-detail-label">Created Date</span>' +
        '<span class="bulk-detail-value">' + _formatDate(ticket.created_date) + '</span>' +
        '</div>';

    if (ticket.description) {
        html += '<div class="bulk-detail-item bulk-detail-description">' +
            '<span class="bulk-detail-label">Description</span>' +
            '<span class="bulk-detail-value">' + _escapeHtml(ticket.description) + '</span>' +
            '</div>';
    }

    html += '</div>';  // close bulk-detail-grid

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

        // Support Group autocomplete
        html += '<div class="manual-assign-field">' +
            '<label>Support Group</label>' +
            '<div class="sg-autocomplete-wrapper">' +
            '<input type="text" class="sg-autocomplete-input" ' +
            'id="sgInput_' + _escapeHtml(ticket.id) + '" ' +
            'value="' + _escapeHtml(sgValue) + '" ' +
            'placeholder="Type to search support groups…" ' +
            'autocomplete="off" ' +
            'onfocus="bulkSgFocus(\'' + _escapeHtml(ticket.id) + '\', \'' + _escapeHtml(ticket.ticket_type) + '\')" ' +
            'oninput="bulkSgFilter(\'' + _escapeHtml(ticket.id) + '\')">' +
            '<input type="hidden" class="sg-autocomplete-guid" ' +
            'id="sgGuid_' + _escapeHtml(ticket.id) + '" ' +
            'value="' + _escapeHtml(override.tier_queue_guid || '') + '">' +
            '<div class="sg-autocomplete-dropdown" id="sgDropdown_' + _escapeHtml(ticket.id) + '"></div>' +
            '</div>' +
            '</div>';

        // Priority dropdown
        html += '<div class="manual-assign-field">' +
            '<label>Priority</label>' +
            '<select class="manual-assign-priority" id="priSelect_' + _escapeHtml(ticket.id) + '" ' +
            'onchange="bulkPriorityChange(\'' + _escapeHtml(ticket.id) + '\', this.value)">';
        html += '<option value="">— Keep current —</option>';

        if (isIR) {
            // IR: numeric priorities 1-9
            for (var p = 1; p <= 9; p++) {
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

        html += '<div class="bulk-rec-grid">';

        // Support Group (editable)
        html += '<div class="bulk-rec-field">' +
            '<label>Support Group</label>' +
            '<input type="text" value="' + _escapeHtml(override.tier_queue_name || r.support_group_name) + '"' +
            ' onchange="bulkUpdateOverride(\'' + _escapeHtml(ticketId) + '\', \'tier_queue_name\', this.value)"' +
            ' placeholder="Support group name">' +
            '</div>';

        // Support Group GUID (editable)
        html += '<div class="bulk-rec-field">' +
            '<label>Support Group GUID</label>' +
            '<input type="text" value="' + _escapeHtml(override.tier_queue_guid || r.support_group_guid) + '"' +
            ' onchange="bulkUpdateOverride(\'' + _escapeHtml(ticketId) + '\', \'tier_queue_guid\', this.value)"' +
            ' placeholder="GUID" style="font-family:monospace;font-size:0.8rem;">' +
            '</div>';

        // Priority (editable)
        html += '<div class="bulk-rec-field">' +
            '<label>Priority</label>' +
            '<input type="text" value="' + _escapeHtml(String(override.priority || r.priority)) + '"' +
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

function _updateTicketRow(ticketId) {
    // Re-render the full queue for simplicity (individual row updates are complex with rec rows)
    _renderQueue();
}

// ── Selection ─────────────────────────────────────────────────────────

function bulkToggleSelect(ticketId, checked) {
    if (checked) {
        _bulkSelected.add(ticketId);
    } else {
        _bulkSelected.delete(ticketId);
    }
    _updateCounts();
}

function bulkToggleSelectAll(checkbox) {
    var checked = checkbox.checked;
    _bulkQueue.forEach(function (ticket) {
        if (_bulkLocks[ticket.id] === _bulkUserId) {
            if (checked) {
                _bulkSelected.add(ticket.id);
            } else {
                _bulkSelected.delete(ticket.id);
            }
        }
    });
    _renderQueue();
    _updateCounts();
}

// ── Override Management ───────────────────────────────────────────────

function bulkUpdateOverride(ticketId, field, value) {
    if (!_bulkOverrides[ticketId]) {
        _bulkOverrides[ticketId] = {};
    }
    _bulkOverrides[ticketId][field] = value;
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
    var hasMySelected = _getMySelectedTicketIds().length > 0;
    document.getElementById('btnRecommend').disabled = !hasMySelected;
    document.getElementById('btnAssign').disabled = !hasMySelected;
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
 * Called when the support group input gains focus — load groups and show dropdown.
 */
function bulkSgFocus(ticketId, ticketType) {
    _loadSupportGroups(ticketType, function () {
        bulkSgFilter(ticketId);
    });
}

/**
 * Filter the support group dropdown based on the current input value.
 */
function bulkSgFilter(ticketId) {
    var input = document.getElementById('sgInput_' + ticketId);
    var dropdown = document.getElementById('sgDropdown_' + ticketId);
    if (!input || !dropdown) return;

    var ticket = _findTicket(ticketId);
    if (!ticket) return;

    var groups = _bulkSupportGroups[ticket.ticket_type] || [];
    var query = input.value.toLowerCase().trim();

    // Filter groups by substring match
    var filtered = groups;
    if (query.length > 0) {
        filtered = groups.filter(function (g) {
            return g.name.toLowerCase().indexOf(query) !== -1;
        });
    }

    // Limit to 50 results for performance
    var shown = filtered.slice(0, 50);

    if (shown.length === 0) {
        dropdown.innerHTML = '<div class="sg-autocomplete-no-match">No matching groups</div>';
        dropdown.style.display = 'block';
        return;
    }

    var html = '';
    shown.forEach(function (g) {
        html += '<div class="sg-autocomplete-option" ' +
            'onmousedown="bulkSgSelect(\'' + _escapeHtml(ticketId) + '\', ' +
            '\'' + _escapeHtml(g.guid) + '\', ' +
            '\'' + _escapeHtml(g.name) + '\')">' +
            _escapeHtml(g.name) + '</div>';
    });

    if (filtered.length > 50) {
        html += '<div class="sg-autocomplete-more">… and ' + (filtered.length - 50) + ' more (type to narrow)</div>';
    }

    dropdown.innerHTML = html;
    dropdown.style.display = 'block';
}

/**
 * Select a support group from the autocomplete dropdown.
 */
function bulkSgSelect(ticketId, guid, name) {
    var input = document.getElementById('sgInput_' + ticketId);
    var guidInput = document.getElementById('sgGuid_' + ticketId);
    var dropdown = document.getElementById('sgDropdown_' + ticketId);

    if (input) input.value = name;
    if (guidInput) guidInput.value = guid;
    if (dropdown) dropdown.style.display = 'none';

    // Update override
    if (!_bulkOverrides[ticketId]) _bulkOverrides[ticketId] = {};
    _bulkOverrides[ticketId].tier_queue_name = name;
    _bulkOverrides[ticketId].tier_queue_guid = guid;
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

// Close autocomplete dropdowns when clicking outside
document.addEventListener('click', function (evt) {
    if (!evt.target.closest('.sg-autocomplete-wrapper')) {
        var dropdowns = document.querySelectorAll('.sg-autocomplete-dropdown');
        dropdowns.forEach(function (dd) { dd.style.display = 'none'; });
    }
});

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