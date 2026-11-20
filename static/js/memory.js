// Altera — Memory Management Page JS

let allMemories = [];

async function memoryRequest(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
        let reason = `HTTP ${response.status}`;
        try { reason = (await response.json()).error || reason; } catch {}
        throw new Error(reason);
    }
    return response;
}

AlteraPage.ready(() => {
    loadAllMemories();
});

async function loadAllMemories() {
    try {
        const res = await memoryRequest('/api/memory');
        allMemories = await res.json();
        updateStats();
    } catch (err) {
        console.error('Failed to load memories:', err);
        showToast('Failed to load memories', 'error');
    }
}

function updateStats() {
    const total = allMemories.length;
    const facts = allMemories.filter(m => m.metadata?.type === 'fact').length;
    const prefs = allMemories.filter(m => m.metadata?.type === 'preference').length;
    const events = allMemories.filter(m => m.metadata?.type === 'event').length;

    document.getElementById('totalMemories').textContent = `${total} memory(s)`;
    document.getElementById('factCount').textContent = facts;
    document.getElementById('preferenceCount').textContent = prefs;
    document.getElementById('eventCount').textContent = events;
}

// ============================================================
// Add Memory
// ============================================================

async function addMemory() {
    const content = document.getElementById('newMemoryContent').value.trim();
    const type = document.getElementById('newMemoryType').value;

    if (!content) {
        showToast('Please enter memory content', 'error');
        return;
    }

    try {
        await memoryRequest('/api/memory', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content, type })
        });

        document.getElementById('newMemoryContent').value = '';
        showToast(`Added ${type} memory`);
        loadAllMemories();
    } catch (err) {
        console.error('Failed to add memory:', err);
        showToast(err.message, 'error');
    }
}

// ============================================================
// Edit Memory
// ============================================================

function editMemory(id) {
    const card = document.querySelector(`[data-id="${id}"]`);
    const contentEl = document.getElementById(`content-${id}`);
    const editInput = document.getElementById(`edit-${id}`);

    if (card.classList.contains('editing')) {
        // Save changes
        const newContent = editInput.value.trim();
        if (!newContent) return;

        fetch(`/api/memory/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: newContent })
        }).then(() => {
            card.classList.remove('editing');
            contentEl.style.display = 'block';
            editInput.style.display = 'none';
            loadAllMemories();
            showToast('Memory updated');
        });
    } else {
        // Enter edit mode
        card.classList.add('editing');
        contentEl.style.display = 'none';
        editInput.style.display = 'block';
        editInput.value = allMemories.find(m => m.id === id)?.content || '';
        editInput.focus();

        // Save on Enter, cancel on Escape
        editInput.onkeydown = (e) => {
            if (e.key === 'Enter') editMemory(id);
            if (e.key === 'Escape') loadAllMemories();
        };
    }
}

// ============================================================
// Delete Memory
// ============================================================

async function deleteMemory(id) {
    const card = document.querySelector(`[data-id="${id}"]`);
    const content = allMemories.find(m => m.id === id)?.content || 'this memory';

    if (!confirm(`Delete this memory?\n\n"${content.substring(0, 60)}${content.length > 60 ? '...' : ''}"`)) return;

    try {
        await memoryRequest(`/api/memory/${id}`, { method: 'DELETE' });
        showToast('Memory deleted');
        loadAllMemories();
    } catch (err) {
        console.error('Failed to delete memory:', err);
        showToast('Failed to delete memory', 'error');
    }
}

// ============================================================
// Clear All Memories
// ============================================================

async function clearAllMemories() {
    if (!confirm('Are you sure? This will delete ALL memories. Type "yes" to confirm.')) return;

    try {
        await memoryRequest('/api/memory/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: 'yes' })
        });
        showToast('All memories cleared');
        loadAllMemories();
    } catch (err) {
        console.error('Failed to clear memories:', err);
        showToast('Failed to clear memories', 'error');
    }
}

// ============================================================
// Search / Filter
// ============================================================

function filterMemories() {
    const query = document.getElementById('searchInput').value.toLowerCase();
    const cards = document.querySelectorAll('.memory-card');

    cards.forEach(card => {
        const content = card.querySelector('.memory-content-text')?.textContent.toLowerCase() || '';
        const type = (card.dataset.type || '').toLowerCase();
        const matchesSearch = !query || content.includes(query) || type.includes(query);
        card.style.display = matchesSearch ? 'block' : 'none';
    });

    // Hide empty sections
    document.querySelectorAll('.memory-section').forEach(section => {
        const visibleCards = section.querySelectorAll('.memory-card[style*="display: block"], .memory-card:not([style*="display: none"])');
        if (visibleCards.length === 0 && query) {
            section.style.display = 'none';
        } else {
            section.style.display = 'block';
        }
    });
}

// ============================================================
// Toast Notification
// ============================================================

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.style.borderLeft = `4px solid ${type === 'error' ? '#dc2626' : '#10b981'}`;
    toast.classList.add('show');

    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ============================================================
// Settings Panel (minimal)
// ============================================================

function toggleSettings() {
    alert('Personality and settings are configured in personality.yaml and config.yaml.');
}
