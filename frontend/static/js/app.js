/* ══════════════════════════════════════════════════════════════════════
   Service Desk Helper — App JavaScript
   Theme toggle, tab switching, and utility functions
   ══════════════════════════════════════════════════════════════════════ */

// ── Theme Management ──────────────────────────────────────────────────

(function initTheme() {
    const saved = localStorage.getItem('sdh-theme') || 'light';
    document.documentElement.setAttribute('data-theme', saved);
})();

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('sdh-theme', theme);

    // Update toggle button states
    document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === theme);
    });
}

// ── Tab Switching ─────────────────────────────────────────────────────

function switchTab(tabGroup, tabName) {
    // Deactivate all tabs in the group
    const container = document.querySelector(`[data-tab-group="${tabGroup}"]`);
    if (!container) return;

    container.querySelectorAll('.tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    container.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.dataset.tabContent === tabName);
    });
}

// ── Toggle Group (IR/SR selector) ─────────────────────────────────────

function setToggle(groupId, value) {
    const group = document.getElementById(groupId);
    if (!group) return;

    group.querySelectorAll('.toggle-option').forEach(opt => {
        opt.classList.toggle('active', opt.dataset.value === value);
    });

    // Update the hidden input
    const hiddenInput = group.querySelector('input[type="hidden"]');
    if (hiddenInput) {
        hiddenInput.value = value;
    }
}

// ── Range Slider Display ──────────────────────────────────────────────

function updateRangeDisplay(inputId, displayId) {
    const input = document.getElementById(inputId);
    const display = document.getElementById(displayId);
    if (input && display) {
        display.textContent = input.value;
    }
}

// ── Clipboard Helper (works over HTTP on LAN, not just HTTPS/localhost) ─

function _writeToClipboard(text) {
    // navigator.clipboard requires a Secure Context (HTTPS or localhost).
    // When accessed over plain HTTP from another machine on the LAN,
    // the Clipboard API is unavailable. Fall back to execCommand('copy').
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    }
    // Fallback: create a temporary textarea, select its content, and copy
    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    return new Promise(function (resolve, reject) {
        var ok = document.execCommand('copy');
        document.body.removeChild(textarea);
        ok ? resolve() : reject(new Error('execCommand copy failed'));
    });
}

// ── Copy Ticket ID to Clipboard ───────────────────────────────────────

function copyTicketId(btn, ticketId) {
    _writeToClipboard(ticketId).then(function () {
        btn.textContent = '✓';
        btn.classList.add('copied');
        setTimeout(function () {
            btn.textContent = '📋';
            btn.classList.remove('copied');
        }, 1500);
    });
}

// ── Ticket Detail Expand/Collapse (inline data) ──────────────────────

function toggleTicketDetails(btn) {
    var row = btn.closest('tr');
    var detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('ticket-detail-row')) return;

    var isVisible = detailRow.style.display !== 'none';
    detailRow.style.display = isVisible ? 'none' : 'table-row';
    btn.textContent = isVisible ? '▶' : '▼';
    btn.classList.toggle('expanded', !isVisible);
}

// ── Ticket Detail Expand/Collapse (lazy-loaded via HTMX) ────────────

function toggleTicketDetailsHtmx(btn, ticketId) {
    var row = btn.closest('tr');
    var detailRow = row.nextElementSibling;
    if (!detailRow || !detailRow.classList.contains('ticket-detail-row')) return;

    var isVisible = detailRow.style.display !== 'none';
    if (isVisible) {
        detailRow.style.display = 'none';
        btn.textContent = '▶';
        btn.classList.remove('expanded');
        return;
    }

    // Show the detail row
    detailRow.style.display = 'table-row';
    btn.textContent = '▼';
    btn.classList.add('expanded');

    // Only fetch if not already loaded (still has the loading placeholder)
    var cell = detailRow.querySelector('td');
    if (cell && cell.querySelector('.ticket-detail-loading')) {
        fetch('/ui/ticket/' + encodeURIComponent(ticketId) + '/details')
            .then(function (response) { return response.text(); })
            .then(function (html) { cell.innerHTML = html; })
            .catch(function () {
                cell.innerHTML = '<div class="alert alert-error"><span>⚠️</span><span>Failed to load details</span></div>';
            });
    }
}

// ── Documentation Expand/Collapse ─────────────────────────────────────

function toggleDocContent(btn) {
    const content = btn.previousElementSibling;
    if (!content) return;

    const isExpanded = content.classList.toggle('expanded');
    btn.textContent = isExpanded ? 'Show less' : 'Show more';
}

// ── Table Sorting ─────────────────────────────────────────────────────

/**
 * Sort a results table by clicking a column header.
 * Cycles: unsorted → ascending → descending → unsorted.
 *
 * @param {HTMLElement} th - The <th> element clicked
 * @param {number} colIndex - 0-based column index to sort by
 * @param {string} sortType - 'string' | 'number' | 'date' | 'natural' | 'similarity'
 */
function sortTable(th, colIndex, sortType) {
    var table = th.closest('table');
    if (!table) return;

    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    // Determine current sort direction from the th
    var currentDir = th.getAttribute('data-sort-dir') || 'none';
    var newDir;
    if (currentDir === 'none') newDir = 'asc';
    else if (currentDir === 'asc') newDir = 'desc';
    else newDir = 'none';

    // Reset all sibling th sort states
    var allThs = table.querySelectorAll('thead th[data-sortable]');
    allThs.forEach(function (h) {
        h.setAttribute('data-sort-dir', 'none');
        var indicator = h.querySelector('.sort-indicator');
        if (indicator) indicator.textContent = '⇅';
    });

    // Collect data rows (skip detail rows)
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var rowPairs = []; // [{dataRow, detailRow, value}]
    for (var i = 0; i < rows.length; i++) {
        if (rows[i].classList.contains('ticket-detail-row')) continue;
        var dataRow = rows[i];
        var detailRow = (i + 1 < rows.length && rows[i + 1].classList.contains('ticket-detail-row'))
            ? rows[i + 1] : null;
        var cell = dataRow.cells[colIndex];
        var rawValue = cell ? (cell.getAttribute('data-sort-value') || cell.textContent.trim()) : '';
        rowPairs.push({ dataRow: dataRow, detailRow: detailRow, value: rawValue, originalIndex: rowPairs.length });
    }

    if (newDir === 'none') {
        // Restore original order
        rowPairs.sort(function (a, b) { return a.originalIndex - b.originalIndex; });
    } else {
        var multiplier = newDir === 'asc' ? 1 : -1;
        rowPairs.sort(function (a, b) {
            var va = a.value;
            var vb = b.value;

            if (va === '—' || va === '') va = null;
            if (vb === '—' || vb === '') vb = null;

            // Nulls always sort to bottom
            if (va === null && vb === null) return 0;
            if (va === null) return 1;
            if (vb === null) return -1;

            if (sortType === 'number' || sortType === 'similarity') {
                return (parseFloat(va) - parseFloat(vb)) * multiplier;
            } else if (sortType === 'date') {
                // Parse HH:MM MM/DD/YYYY format
                var da = _parseSortDate(va);
                var db = _parseSortDate(vb);
                return (da - db) * multiplier;
            } else if (sortType === 'natural') {
                return _naturalCompare(va, vb) * multiplier;
            } else {
                // string
                return va.localeCompare(vb, undefined, { sensitivity: 'base' }) * multiplier;
            }
        });
    }

    // Re-append rows in sorted order
    rowPairs.forEach(function (pair) {
        tbody.appendChild(pair.dataRow);
        if (pair.detailRow) tbody.appendChild(pair.detailRow);
    });

    // Update the clicked th
    th.setAttribute('data-sort-dir', newDir);
    var indicator = th.querySelector('.sort-indicator');
    if (indicator) {
        if (newDir === 'asc') indicator.textContent = '▲';
        else if (newDir === 'desc') indicator.textContent = '▼';
        else indicator.textContent = '⇅';
    }
}

function _parseSortDate(str) {
    // HH:MM MM/DD/YYYY
    var match = str.match(/^(\d{1,2}):(\d{2})\s+(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
    if (match) {
        return new Date(
            parseInt(match[5]), parseInt(match[3]) - 1, parseInt(match[4]),
            parseInt(match[1]), parseInt(match[2])
        ).getTime();
    }
    return 0;
}

/**
 * Convert a date input value (YYYY-MM-DD) to a timestamp.
 * @param {string} dateStr - Date string in YYYY-MM-DD format
 * @param {boolean} endOfDay - If true, set time to 23:59:59; otherwise 00:00:00
 * @returns {number} Timestamp in milliseconds
 */
function _dateInputToTimestamp(dateStr, endOfDay) {
    var parts = dateStr.split('-');
    if (parts.length !== 3) return 0;
    var year = parseInt(parts[0]);
    var month = parseInt(parts[1]) - 1;
    var day = parseInt(parts[2]);
    if (endOfDay) {
        return new Date(year, month, day, 23, 59, 59, 999).getTime();
    }
    return new Date(year, month, day, 0, 0, 0, 0).getTime();
}

function _naturalCompare(a, b) {
    // Natural sort: IR100 > IR99
    var ax = [], bx = [];
    a.replace(/(\d+)|(\D+)/g, function (_, $1, $2) { ax.push([$1 || Infinity, $2 || '']); });
    b.replace(/(\d+)|(\D+)/g, function (_, $1, $2) { bx.push([$1 || Infinity, $2 || '']); });
    while (ax.length && bx.length) {
        var an = ax.shift();
        var bn = bx.shift();
        var nn = (an[0] - bn[0]) || an[1].localeCompare(bn[1]);
        if (nn) return nn;
    }
    return ax.length - bx.length;
}

// ── Table Filtering ───────────────────────────────────────────────────

/**
 * Filter table rows based on filter inputs in the filter row.
 * Called on every input/change event in a filter control.
 *
 * @param {HTMLElement} filterInput - The input/select element that changed
 */
function filterTable(filterInput) {
    var table = filterInput.closest('table');
    // If triggered from sg-dropdown-toggle, go up to the wrapper's table
    if (!table) {
        var wrapper = filterInput.closest('.sg-dropdown-wrapper');
        if (wrapper) table = wrapper.closest('table');
    }
    if (!table) return;

    var filterRow = table.querySelector('.filter-row');
    if (!filterRow) return;

    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    // Collect all filter values
    var filters = [];
    var filterCells = filterRow.querySelectorAll('td');
    filterCells.forEach(function (cell, idx) {
        // Check for date-range-filter first
        var dateRangeWrapper = cell.querySelector('.date-range-filter');
        if (dateRangeWrapper) {
            var fromVal = dateRangeWrapper.querySelector('.date-from').value;
            var toVal = dateRangeWrapper.querySelector('.date-to').value;
            if (fromVal || toVal) {
                filters.push({ colIndex: idx, value: 'date-range', type: 'date-range', from: fromVal, to: toVal });
            } else {
                filters.push(null);
            }
            return;
        }
        // Check for sg-dropdown-wrapper
        var sgWrapper = cell.querySelector('.sg-dropdown-wrapper');
        if (sgWrapper) {
            var selectedVal = sgWrapper.getAttribute('data-selected-value') || '';
            filters.push({ colIndex: idx, value: selectedVal, type: 'sg-dropdown' });
            return;
        }
        var input = cell.querySelector('input, select');
        if (input) {
            filters.push({ colIndex: idx, value: input.value.trim().toLowerCase(), type: input.tagName.toLowerCase() });
        } else {
            filters.push(null);
        }
    });

    // Apply filters to rows
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var visibleCount = 0;
    var totalCount = 0;

    for (var i = 0; i < rows.length; i++) {
        if (rows[i].classList.contains('ticket-detail-row')) continue;
        totalCount++;

        var dataRow = rows[i];
        var detailRow = (i + 1 < rows.length && rows[i + 1].classList.contains('ticket-detail-row'))
            ? rows[i + 1] : null;

        var visible = true;
        for (var f = 0; f < filters.length; f++) {
            if (!filters[f] || !filters[f].value) continue;
            var cell = dataRow.cells[f];
            if (!cell) continue;

            if (filters[f].type === 'date-range') {
                // Parse the cell date (format: MM/DD/YYYY HH:MM)
                var cellDateTs = _parseSortDate(cell.textContent.trim());
                if (cellDateTs === 0) { visible = false; break; }
                if (filters[f].from) {
                    var fromTs = _dateInputToTimestamp(filters[f].from, false);
                    if (cellDateTs < fromTs) { visible = false; break; }
                }
                if (filters[f].to) {
                    var toTs = _dateInputToTimestamp(filters[f].to, true);
                    if (cellDateTs > toTs) { visible = false; break; }
                }
            } else {
                var cellText = cell.textContent.trim().toLowerCase();
                if (cellText.indexOf(filters[f].value) === -1) {
                    visible = false;
                    break;
                }
            }
        }

        dataRow.style.display = visible ? '' : 'none';
        if (detailRow) {
            if (!visible) {
                detailRow.style.display = 'none';
            }
            // If visible, keep detail row in its current state (don't force-show it)
        }

        if (visible) visibleCount++;
    }

    // Update filter count display
    var resultsArea = table.closest('.results-area') || table.parentElement;
    if (!resultsArea) resultsArea = table.parentElement;
    var filterInfo = resultsArea ? resultsArea.querySelector('.filter-info') : null;
    if (filterInfo) {
        if (visibleCount < totalCount) {
            filterInfo.textContent = 'Showing ' + visibleCount + ' of ' + totalCount + ' (' + (totalCount - visibleCount) + ' filtered out)';
            filterInfo.style.display = '';
        } else {
            filterInfo.textContent = '';
            filterInfo.style.display = 'none';
        }
    }
}

/**
 * Clear all filter inputs in a table and show all rows.
 *
 * @param {HTMLElement} btn - The clear button clicked
 */
function clearFilters(btn) {
    var table = btn.closest('.results-area') ? btn.closest('.results-area').querySelector('table') : null;
    if (!table) {
        // Try going up from the button
        table = btn.closest('div').querySelector('table');
    }
    if (!table) return;

    var filterRow = table.querySelector('.filter-row');
    if (!filterRow) return;

    filterRow.querySelectorAll('input, select').forEach(function (input) {
        if (input.tagName.toLowerCase() === 'select') {
            input.selectedIndex = 0;
        } else if (!input.classList.contains('sg-dropdown-search')) {
            input.value = '';
        }
    });

    // Reset date range filters
    filterRow.querySelectorAll('.date-range-filter').forEach(function (wrapper) {
        wrapper.querySelector('.date-from').value = '';
        wrapper.querySelector('.date-to').value = '';
    });

    // Reset sg-dropdown wrappers
    filterRow.querySelectorAll('.sg-dropdown-wrapper').forEach(function (wrapper) {
        wrapper.setAttribute('data-selected-value', '');
        var toggle = wrapper.querySelector('.sg-dropdown-toggle');
        if (toggle) {
            toggle.textContent = 'All';
            toggle.classList.remove('has-value');
        }
        var list = wrapper.querySelector('.sg-dropdown-list');
        if (list) {
            list.querySelectorAll('.sg-dropdown-item').forEach(function (item) {
                item.classList.remove('selected');
                item.style.display = '';
            });
            var allItem = list.querySelector('.sg-dropdown-item-all');
            if (allItem) allItem.classList.add('selected');
        }
        var panel = wrapper.querySelector('.sg-dropdown-panel');
        if (panel) panel.style.display = 'none';
        var searchInput = wrapper.querySelector('.sg-dropdown-search');
        if (searchInput) searchInput.value = '';
    });

    // Show all rows
    var tbody = table.querySelector('tbody');
    if (tbody) {
        tbody.querySelectorAll('tr').forEach(function (row) {
            if (!row.classList.contains('ticket-detail-row')) {
                row.style.display = '';
            }
        });
    }

    // Hide filter info
    var resultsArea = table.closest('.results-area') || table.parentElement;
    var filterInfo = resultsArea ? resultsArea.querySelector('.filter-info') : null;
    if (filterInfo) {
        filterInfo.style.display = 'none';
        filterInfo.textContent = '';
    }
}

// ── Support Group Searchable Dropdown ──────────────────────────────────

/**
 * Populate the support group dropdown with unique values from the table.
 * Scans the Support Group column for unique values and builds the list.
 *
 * @param {HTMLElement} table - The results table element
 */
function populateSgDropdown(table) {
    if (!table) return;
    var wrapper = table.querySelector('.filter-row .sg-dropdown-wrapper');
    if (!wrapper) return;

    var list = wrapper.querySelector('.sg-dropdown-list');
    if (!list) return;

    // Find the column index for the support group wrapper
    var cell = wrapper.closest('td');
    var sgCol = cell ? cell.cellIndex : -1;
    if (sgCol < 0) return;

    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    var groups = new Set();
    tbody.querySelectorAll('tr').forEach(function (row) {
        if (row.classList.contains('ticket-detail-row')) return;
        var td = row.cells[sgCol];
        if (td) {
            var text = td.textContent.trim();
            if (text && text !== '—') groups.add(text);
        }
    });

    // Clear existing items except the "All" item
    var allItem = list.querySelector('.sg-dropdown-item-all');
    while (list.children.length > 1) {
        if (list.lastChild !== allItem) {
            list.removeChild(list.lastChild);
        } else {
            break;
        }
    }

    // Add sorted unique groups
    Array.from(groups).sort(function (a, b) {
        return a.localeCompare(b, undefined, { sensitivity: 'base' });
    }).forEach(function (group) {
        var li = document.createElement('li');
        li.className = 'sg-dropdown-item';
        li.textContent = group;
        li.setAttribute('data-value', group.toLowerCase());
        li.onclick = function () { selectSgOption(li); };
        list.appendChild(li);
    });

    // Remove stale "no match" message if present
    var noMatch = wrapper.querySelector('.sg-dropdown-no-match');
    if (noMatch) noMatch.remove();
}

/**
 * Toggle the support group dropdown panel open/closed.
 *
 * @param {HTMLElement} btn - The toggle button clicked
 */
function toggleSgDropdown(btn) {
    var wrapper = btn.closest('.sg-dropdown-wrapper');
    if (!wrapper) return;

    var panel = wrapper.querySelector('.sg-dropdown-panel');
    if (!panel) return;

    var isOpen = panel.style.display !== 'none';
    if (isOpen) {
        panel.style.display = 'none';
    } else {
        // Close any other open sg-dropdowns first
        document.querySelectorAll('.sg-dropdown-panel').forEach(function (p) {
            p.style.display = 'none';
        });
        panel.style.display = 'flex';
        // Focus the search input
        var searchInput = panel.querySelector('.sg-dropdown-search');
        if (searchInput) {
            searchInput.value = '';
            searchInput.focus();
            // Show all items
            _filterSgItems(wrapper, '');
        }
    }
}

/**
 * Filter the dropdown list items as the user types in the search box.
 *
 * @param {HTMLElement} searchInput - The search input element
 */
function filterSgDropdown(searchInput) {
    var wrapper = searchInput.closest('.sg-dropdown-wrapper');
    if (!wrapper) return;
    _filterSgItems(wrapper, searchInput.value.trim().toLowerCase());
}

/**
 * Internal: filter list items by search text.
 */
function _filterSgItems(wrapper, searchText) {
    var list = wrapper.querySelector('.sg-dropdown-list');
    if (!list) return;

    var items = list.querySelectorAll('.sg-dropdown-item');
    var visibleCount = 0;

    items.forEach(function (item) {
        if (item.classList.contains('sg-dropdown-item-all')) {
            // Always show the "All" option
            item.style.display = '';
            return;
        }
        var text = item.textContent.toLowerCase();
        var match = !searchText || text.indexOf(searchText) !== -1;
        item.style.display = match ? '' : 'none';
        if (match) visibleCount++;
    });

    // Show/hide "no match" message
    var noMatch = wrapper.querySelector('.sg-dropdown-no-match');
    if (visibleCount === 0 && searchText) {
        if (!noMatch) {
            noMatch = document.createElement('div');
            noMatch.className = 'sg-dropdown-no-match';
            noMatch.textContent = 'No matching groups';
            var panel = wrapper.querySelector('.sg-dropdown-panel');
            if (panel) panel.appendChild(noMatch);
        }
        noMatch.style.display = '';
    } else if (noMatch) {
        noMatch.style.display = 'none';
    }
}

/**
 * Select a support group option from the dropdown.
 *
 * @param {HTMLElement} li - The list item clicked
 */
function selectSgOption(li) {
    var wrapper = li.closest('.sg-dropdown-wrapper');
    if (!wrapper) return;

    var list = wrapper.querySelector('.sg-dropdown-list');
    var toggle = wrapper.querySelector('.sg-dropdown-toggle');
    var panel = wrapper.querySelector('.sg-dropdown-panel');

    // Update selected state
    list.querySelectorAll('.sg-dropdown-item').forEach(function (item) {
        item.classList.remove('selected');
    });
    li.classList.add('selected');

    var isAll = li.classList.contains('sg-dropdown-item-all');
    var value = isAll ? '' : li.textContent.trim();

    // Update toggle button text
    toggle.textContent = isAll ? 'All' : value;
    toggle.classList.toggle('has-value', !isAll);

    // Store the selected value for filtering
    wrapper.setAttribute('data-selected-value', value.toLowerCase());

    // Close the panel
    if (panel) panel.style.display = 'none';

    // Trigger table filtering
    filterTable(toggle);
}

/**
 * Populate the status dropdown filter with unique values from the table.
 * Called after HTMX swaps in new content.
 */
function populateStatusDropdown(table) {
    if (!table) return;
    var dropdown = table.querySelector('.filter-row select[data-filter-col="status"]');
    if (!dropdown) return;

    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    var statusCol = parseInt(dropdown.closest('td').cellIndex || dropdown.getAttribute('data-col-index'));
    var statuses = new Set();

    tbody.querySelectorAll('tr').forEach(function (row) {
        if (row.classList.contains('ticket-detail-row')) return;
        var cell = row.cells[statusCol];
        if (cell) {
            var text = cell.textContent.trim();
            if (text && text !== '—') statuses.add(text);
        }
    });

    // Clear existing options except the first "All" option
    while (dropdown.options.length > 1) {
        dropdown.remove(1);
    }

    // Add sorted unique statuses
    Array.from(statuses).sort().forEach(function (status) {
        var opt = document.createElement('option');
        opt.value = status.toLowerCase();
        opt.textContent = status;
        dropdown.appendChild(opt);
    });
}

// ── HTMX After-Swap Hook ──────────────────────────────────────────────

document.addEventListener('htmx:afterSwap', function (evt) {
    // After HTMX swaps in new search results, populate dropdowns
    var target = evt.detail.target;
    if (target) {
        var tables = target.querySelectorAll('table.results-table');
        tables.forEach(function (table) {
            populateStatusDropdown(table);
            populateSgDropdown(table);
        });
    }
});

// ── Close SG Dropdown on Outside Click ────────────────────────────────

document.addEventListener('click', function (evt) {
    document.querySelectorAll('.sg-dropdown-wrapper').forEach(function (wrapper) {
        if (!wrapper.contains(evt.target)) {
            var panel = wrapper.querySelector('.sg-dropdown-panel');
            if (panel) panel.style.display = 'none';
        }
    });
});

// ── DOMContentLoaded Setup ────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
    // Set initial theme toggle button states
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
    document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === currentTheme);
    });
});

// ══════════════════════════════════════════════════════════════════════
//  Chat UI — Feature #2: Q&A Chatbot
// ══════════════════════════════════════════════════════════════════════

var _chatSessionId = null;
var _chatSending = false;

// ── Send Message ──────────────────────────────────────────────────────

function sendChatMessage(evt) {
    if (evt) evt.preventDefault();
    if (_chatSending) return;

    var input = document.getElementById('chatInput');
    var message = input.value.trim();
    if (!message) return;

    // Remove welcome screen if present
    var welcome = document.querySelector('.chat-welcome');
    if (welcome) welcome.remove();

    // Append user message
    _appendChatMessage('user', message);

    // Clear input and reset height
    input.value = '';
    input.style.height = 'auto';

    // Show typing indicator
    _showTypingIndicator();

    // Disable send
    _chatSending = true;
    _setChatSendEnabled(false);

    // Gather settings
    var topKDocs = parseInt(document.getElementById('chatTopKDocs')?.value || '5');
    var topKTickets = parseInt(document.getElementById('chatTopKTickets')?.value || '5');
    var maxTokens = parseInt(document.getElementById('chatMaxTokens')?.value || '2048');

    // Build request body
    var body = {
        message: message,
        top_k_docs: topKDocs,
        top_k_tickets: topKTickets,
        max_tokens: maxTokens
    };
    if (_chatSessionId) {
        body.session_id = _chatSessionId;
    }

    fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function (response) {
        if (!response.ok) {
            return response.text().then(function (text) {
                var detail = 'Request failed (' + response.status + ')';
                try {
                    var err = JSON.parse(text);
                    detail = err.detail || detail;
                } catch (_) {
                    if (text) detail = text;
                }
                throw new Error(detail);
            });
        }
        return response.json();
    })
    .then(function (data) {
        _removeTypingIndicator();
        _chatSessionId = data.session_id;
        _updateSessionBadge();
        _appendChatMessage('assistant', data.message, data.sources || []);
    })
    .catch(function (err) {
        _removeTypingIndicator();
        _appendChatError(err.message || 'An unexpected error occurred.');
    })
    .finally(function () {
        _chatSending = false;
        _setChatSendEnabled(true);
        input.focus();
    });
}

// ── Reset Session ─────────────────────────────────────────────────────

function resetChatSession() {
    if (_chatSessionId && !_chatSending) {
        // Fire-and-forget reset on the server
        fetch('/chat/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: _chatSessionId })
        }).catch(function () { /* ignore */ });
    }

    _chatSessionId = null;
    _chatSending = false;
    _updateSessionBadge();

    // Restore the messages area to the welcome screen
    var messagesEl = document.getElementById('chatMessages');
    if (messagesEl) {
        messagesEl.innerHTML =
            '<div class="chat-welcome">' +
                '<div class="chat-welcome-icon">🤖</div>' +
                '<div class="chat-welcome-title">Penn Medicine IT Service Desk Assistant</div>' +
                '<div class="chat-welcome-text">' +
                    'Ask me anything about IT support procedures, troubleshooting steps, or ticket handling. ' +
                    'I\'ll search our knowledge base and historical tickets to help you find answers.' +
                '</div>' +
                '<div class="chat-welcome-suggestions">' +
                    '<button class="chat-suggestion" onclick="useSuggestion(this)">How do I reset a PennChart password?</button>' +
                    '<button class="chat-suggestion" onclick="useSuggestion(this)">VPN connection troubleshooting steps</button>' +
                    '<button class="chat-suggestion" onclick="useSuggestion(this)">Printer not working at HUP</button>' +
                    '<button class="chat-suggestion" onclick="useSuggestion(this)">Citrix session is frozen</button>' +
                '</div>' +
            '</div>';
    }

    _setChatSendEnabled(true);
    var input = document.getElementById('chatInput');
    if (input) {
        input.value = '';
        input.focus();
    }
}

// ── Suggestion Buttons ────────────────────────────────────────────────

function useSuggestion(btn) {
    var input = document.getElementById('chatInput');
    if (input) {
        input.value = btn.textContent;
        input.focus();
        sendChatMessage(null);
    }
}

// ── Settings Toggle ───────────────────────────────────────────────────

function toggleChatSettings() {
    var panel = document.getElementById('chatSettings');
    if (panel) {
        panel.style.display = panel.style.display === 'none' ? '' : 'none';
    }
}

// ── Keyboard Handling ─────────────────────────────────────────────────

function handleChatKeydown(evt) {
    // Enter without Shift sends the message
    if (evt.key === 'Enter' && !evt.shiftKey) {
        evt.preventDefault();
        sendChatMessage(null);
    }
}

// ── Auto-resize Textarea ──────────────────────────────────────────────

function autoResizeChatInput(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ── Toggle Sources Panel ──────────────────────────────────────────────

function toggleChatSources(btn) {
    var panel = btn.nextElementSibling;
    if (!panel) return;
    var isExpanded = panel.classList.toggle('expanded');
    var arrow = btn.querySelector('.sources-arrow');
    if (arrow) arrow.textContent = isExpanded ? '▾' : '▸';
}

// ── Internal Helpers ──────────────────────────────────────────────────

function _appendChatMessage(role, content, sources) {
    var messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return;

    var msgDiv = document.createElement('div');
    msgDiv.className = 'chat-msg chat-msg-' + role;

    var bubbleDiv = document.createElement('div');
    bubbleDiv.className = 'chat-msg-bubble';

    if (role === 'assistant') {
        bubbleDiv.innerHTML = _formatMarkdown(content);
    } else {
        bubbleDiv.textContent = content;
    }

    msgDiv.appendChild(bubbleDiv);

    // Meta line (timestamp)
    var metaDiv = document.createElement('div');
    metaDiv.className = 'chat-msg-meta';
    var timeSpan = document.createElement('span');
    timeSpan.className = 'chat-msg-time';
    timeSpan.textContent = _formatTime(new Date());
    metaDiv.appendChild(timeSpan);
    msgDiv.appendChild(metaDiv);

    // Sources panel (assistant only, collapsed by default)
    if (role === 'assistant' && sources && sources.length > 0) {
        var sourcesHtml = _buildSourcesHtml(sources);
        var wrapper = document.createElement('div');
        wrapper.innerHTML = sourcesHtml;
        msgDiv.appendChild(wrapper.firstElementChild);
        // Also append the panel
        msgDiv.appendChild(wrapper.lastElementChild);
    }

    messagesEl.appendChild(msgDiv);
    _scrollChatToBottom();
}

function _appendChatError(message) {
    var messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return;

    var errDiv = document.createElement('div');
    errDiv.className = 'chat-error';
    errDiv.innerHTML =
        '<div class="chat-error-bubble">' +
            '<span>⚠️</span>' +
            '<span>' + _escapeHtml(message) + '</span>' +
        '</div>';

    messagesEl.appendChild(errDiv);
    _scrollChatToBottom();
}

function _showTypingIndicator() {
    var messagesEl = document.getElementById('chatMessages');
    if (!messagesEl) return;

    // Remove existing indicator if any
    _removeTypingIndicator();

    var indicator = document.createElement('div');
    indicator.className = 'chat-typing';
    indicator.id = 'chatTypingIndicator';
    indicator.innerHTML =
        '<div class="chat-typing-dots">' +
            '<div class="chat-typing-dot"></div>' +
            '<div class="chat-typing-dot"></div>' +
            '<div class="chat-typing-dot"></div>' +
        '</div>' +
        '<span class="chat-typing-text">Thinking…</span>';

    messagesEl.appendChild(indicator);
    _scrollChatToBottom();
}

function _removeTypingIndicator() {
    var indicator = document.getElementById('chatTypingIndicator');
    if (indicator) indicator.remove();
}

function _setChatSendEnabled(enabled) {
    var btn = document.getElementById('chatSendBtn');
    if (btn) btn.disabled = !enabled;
}

function _updateSessionBadge() {
    var badge = document.getElementById('chatSessionBadge');
    if (!badge) return;
    if (_chatSessionId) {
        badge.textContent = _chatSessionId.substring(0, 8) + '…';
        badge.style.display = '';
        badge.title = 'Session: ' + _chatSessionId;
    } else {
        badge.style.display = 'none';
    }
}

function _scrollChatToBottom() {
    var messagesEl = document.getElementById('chatMessages');
    if (messagesEl) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
}

function _formatTime(date) {
    var h = date.getHours();
    var m = date.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
}

// ── Markdown Formatting (lightweight) ─────────────────────────────────

function _formatMarkdown(text) {
    if (!text) return '';

    // Escape HTML first
    var html = _escapeHtml(text);

    // Code blocks (```...```)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
        return '<pre><code>' + code.trim() + '</code></pre>';
    });

    // Inline code (`...`)
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold (**...**)
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // Italic (*...*)
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Unordered lists (lines starting with - or *)
    html = html.replace(/^(\s*[-*])\s+(.+)$/gm, function (_, bullet, content) {
        return '<li>' + content + '</li>';
    });
    // Wrap consecutive <li> in <ul>
    html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

    // Numbered lists (lines starting with 1. 2. etc.)
    html = html.replace(/^(\s*\d+\.)\s+(.+)$/gm, function (_, num, content) {
        return '<oli>' + content + '</oli>';
    });
    html = html.replace(/((?:<oli>.*<\/oli>\n?)+)/g, function (match) {
        return '<ol>' + match.replace(/<\/?oli>/g, function (tag) {
            return tag.replace('oli', 'li');
        }) + '</ol>';
    });

    // Paragraphs (double newlines)
    html = html.replace(/\n\n+/g, '</p><p>');
    // Single newlines within paragraphs
    html = html.replace(/\n/g, '<br>');

    // Wrap in paragraph tags if not already wrapped
    if (!html.startsWith('<')) {
        html = '<p>' + html + '</p>';
    }

    // Clean up empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');
    // Clean up <br> right after block elements
    html = html.replace(/(<\/(?:ul|ol|pre|h[1-6])>)\s*<br>/g, '$1');
    html = html.replace(/<br>\s*(<(?:ul|ol|pre|h[1-6]))/g, '$1');

    return html;
}

function _escapeHtml(text) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
}

// ── Sources HTML Builder ──────────────────────────────────────────────

function _buildSourcesHtml(sources) {
    if (!sources || sources.length === 0) return '';

    var toggleHtml =
        '<button class="chat-sources-toggle" onclick="toggleChatSources(this)">' +
            '<span class="sources-arrow">▸</span> ' +
            'Sources ' +
            '<span class="sources-count">' + sources.length + '</span>' +
        '</button>';

    var itemsHtml = '';
    for (var i = 0; i < sources.length; i++) {
        var src = sources[i];
        var icon = src.type === 'documentation' ? '📄' : '🎫';
        var scorePercent = Math.round((src.similarity || 0) * 100);
        var scoreText = scorePercent + '%';

        var detailParts = [];
        if (src.notebook) detailParts.push(src.notebook);
        if (src.section) detailParts.push(src.section);
        var detailText = detailParts.length > 0 ? detailParts.join(' › ') : (src.type === 'ticket' ? 'Historical ticket' : '');

        var titleText = src.title || 'Untitled';

        itemsHtml +=
            '<div class="chat-source-item">' +
                '<span class="chat-source-icon">' + icon + '</span>' +
                '<div class="chat-source-info">' +
                    '<div class="chat-source-title">' +
                        '<span>' + _escapeHtml(titleText) + '</span>' +
                        '<button class="chat-source-copy-btn" onclick="copyChatSource(this, \'' + _escapeAttr(titleText) + '\')" title="Copy to clipboard">📋</button>' +
                    '</div>' +
                    (detailText ? '<div class="chat-source-detail">' + _escapeHtml(detailText) + '</div>' : '') +
                    (src.content_preview ? '<div class="chat-source-preview">' + _escapeHtml(src.content_preview) + '</div>' : '') +
                '</div>' +
                '<div class="chat-source-score">' +
                    '<div class="similarity-score">' +
                        '<div class="similarity-bar"><div class="similarity-bar-fill" style="width:' + scorePercent + '%"></div></div>' +
                        '<span>' + scoreText + '</span>' +
                    '</div>' +
                '</div>' +
            '</div>';
    }

    var panelHtml =
        '<div class="chat-sources-panel">' +
            '<div class="chat-sources-list">' + itemsHtml + '</div>' +
        '</div>';

    return toggleHtml + panelHtml;
}

// ── Copy Chat Source to Clipboard ─────────────────────────────────────

function copyChatSource(btn, text) {
    _writeToClipboard(text).then(function () {
        btn.textContent = '✓';
        btn.classList.add('copied');
        setTimeout(function () {
            btn.textContent = '📋';
            btn.classList.remove('copied');
        }, 1500);
    });
}

function _escapeAttr(text) {
    return text.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '"');
}

// ══════════════════════════════════════════════════════════════════════
//  Assignment UI — Feature #3: Ticket Assignment Recommendation
// ══════════════════════════════════════════════════════════════════════

// ── Copy to Clipboard (generic) ───────────────────────────────────────

function copyToClipboard(btn, text) {
    _writeToClipboard(text).then(function () {
        var original = btn.textContent;
        btn.textContent = '✓';
        btn.classList.add('copied');
        setTimeout(function () {
            btn.textContent = original;
            btn.classList.remove('copied');
        }, 1500);
    });
}

/* ── Assignment Source Ticket Detail Expand ─────────────────────────── */

function toggleSourceTicketDetail(btn, ticketId) {
    var panel = document.getElementById('source-detail-' + ticketId);
    if (!panel) return;

    var isVisible = panel.style.display !== 'none';

    if (isVisible) {
        // Collapse
        panel.style.display = 'none';
        btn.textContent = '▶';
        btn.classList.remove('expanded');
    } else {
        // Expand
        panel.style.display = 'block';
        btn.textContent = '▼';
        btn.classList.add('expanded');

        // Lazy-load ticket details if not yet loaded
        var loading = panel.querySelector('.assignment-source-detail-loading');
        if (loading) {
            fetch('/ui/ticket/' + encodeURIComponent(ticketId) + '/details')
                .then(function (response) { return response.text(); })
                .then(function (html) { panel.innerHTML = html; })
                .catch(function () {
                    panel.innerHTML = '<div class="alert alert-error"><span>⚠️</span><span>Could not load details for ' + ticketId + '</span></div>';
                });
        }
    }
}
