/** MatchBox Web Admin UI */

// --- API Client ---
function getRelayPrefix() {
    // Detect relay-proxied access: URL ends with /{instance_id}/admin[/]
    // e.g. /FTC/MatchBox/uswacmp/admin -> "/FTC/MatchBox/uswacmp"
    const pathParts = window.location.pathname.split('/').filter(Boolean);
    const adminIdx = pathParts.lastIndexOf('admin');
    if (adminIdx >= 1) {
        return '/' + pathParts.slice(0, adminIdx).join('/');
    }
    return null;
}

function getApiBase() {
    const prefix = getRelayPrefix();
    if (prefix) return prefix + '/api/';
    return '/api/';
}

const API = {
    async get(path) {
        const res = await fetch(getApiBase() + path);
        return res.json();
    },
    async post(path, body) {
        const res = await fetch(getApiBase() + path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : undefined,
        });
        return res.json();
    }
};

// --- State ---
let wsStatus = null;
let wsLogs = null;
let currentStatus = {};
let autoScroll = true;

// --- Tab switching ---
let obsFrameLoaded = false;

function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.toggle('active', el.id === 'tab-' + tabId);
    });

    // Lazy-load obs-web on first visit to OBS tab
    if (tabId === 'obs' && !obsFrameLoaded) {
        obsFrameLoaded = true;
        const wsBase = getWsBase();
        const password = getVal('cfg-obs-password') || '';
        const prefix = getRelayPrefix();
        const obsWebPath = prefix ? `${prefix}/obs-web/` : '/obs-web/';
        const obsUrl = `${obsWebPath}#${wsBase}/ws/obs` + (password ? `#${password}` : '');

        const frame = document.getElementById('obs-frame');
        if (frame) frame.src = obsUrl;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Tab buttons
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // Load initial data
    loadConfig();
    loadClips();
    loadStatus();

    // Connect WebSockets
    connectStatusWS();
    connectLogsWS();

    // Button handlers
    document.getElementById('btn-start').addEventListener('click', startMatchbox);
    document.getElementById('btn-stop').addEventListener('click', stopMatchbox);
    document.getElementById('btn-configure-obs').addEventListener('click', configureObs);
    document.getElementById('btn-save-config').addEventListener('click', saveConfig);
    document.getElementById('btn-start-sync').addEventListener('click', startSync);
    document.getElementById('btn-stop-sync').addEventListener('click', stopSync);
    document.getElementById('btn-start-tunnel').addEventListener('click', startTunnel);
    document.getElementById('btn-stop-tunnel').addEventListener('click', stopTunnel);

    // Auto-scroll toggle
    const logContainer = document.getElementById('log-container');
    logContainer.addEventListener('scroll', () => {
        const atBottom = logContainer.scrollHeight - logContainer.scrollTop - logContainer.clientHeight < 50;
        autoScroll = atBottom;
    });

    // Periodic refresh
    setInterval(loadClips, 30000);
    setInterval(loadStatus, 10000);
});

// --- WebSocket connections ---
function getWsBase() {
    const loc = window.location;
    const prefix = getRelayPrefix();

    if (prefix) {
        // Accessed via relay - WS routes through relay at same origin
        const wsProto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${wsProto}//${loc.host}${prefix}`;
    }

    // Direct access - WS server is on port+1
    const wsPort = parseInt(loc.port || (loc.protocol === 'https:' ? '443' : '80')) + 1;
    return `ws://${loc.hostname}:${wsPort}`;
}

function connectStatusWS() {
    if (wsStatus) { try { wsStatus.close(); } catch(e) {} }

    wsStatus = new WebSocket(getWsBase() + '/ws/status');
    wsStatus.onmessage = (e) => {
        try {
            currentStatus = JSON.parse(e.data);
            updateStatusUI(currentStatus);
        } catch(err) {}
    };
    wsStatus.onclose = () => { setTimeout(connectStatusWS, 3000); };
    wsStatus.onerror = () => {};
}

function connectLogsWS() {
    if (wsLogs) { try { wsLogs.close(); } catch(e) {} }

    wsLogs = new WebSocket(getWsBase() + '/ws/logs');
    wsLogs.onmessage = (e) => {
        try {
            const entry = JSON.parse(e.data);
            appendLog(entry);
        } catch(err) {}
    };
    wsLogs.onclose = () => { setTimeout(connectLogsWS, 3000); };
    wsLogs.onerror = () => {};
}

// --- Status UI ---
function updateStatusUI(status) {
    // Status dots
    setDot('dot-running', status.running);
    setDot('dot-obs', status.obs_connected);
    setDot('dot-ftc', status.ftc_connected);
    setDot('dot-tunnel', status.tunnel_connected);

    // Overview cards
    setText('status-running', status.running ? 'Running' : 'Stopped');
    setText('status-field', status.current_field != null ? 'Field ' + status.current_field : '--');
    setText('status-clips', status.clips_count != null ? status.clips_count : '--');
    setText('status-event', status.event_code || '--');

    // Start/Stop button states
    const startBtn = document.getElementById('btn-start');
    const stopBtn = document.getElementById('btn-stop');
    if (startBtn) startBtn.disabled = !!status.running;
    if (stopBtn) stopBtn.disabled = !status.running;

    // Sync button states
    const startSyncBtn = document.getElementById('btn-start-sync');
    const stopSyncBtn = document.getElementById('btn-stop-sync');
    if (startSyncBtn) startSyncBtn.disabled = !!status.sync_running;
    if (stopSyncBtn) stopSyncBtn.disabled = !status.sync_running;

    // Tunnel button states
    const startTunnelBtn = document.getElementById('btn-start-tunnel');
    const stopTunnelBtn = document.getElementById('btn-stop-tunnel');
    if (startTunnelBtn) startTunnelBtn.disabled = !!status.tunnel_connected;
    if (stopTunnelBtn) stopTunnelBtn.disabled = !status.tunnel_connected;
}

function setDot(id, on) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('on', !!on);
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

// --- Load data ---
async function loadStatus() {
    try {
        const status = await API.get('status');
        currentStatus = status;
        updateStatusUI(status);
    } catch(e) {}
}

async function loadConfig() {
    try {
        const cfg = await API.get('config');
        populateConfigForm(cfg);
    } catch(e) {}
}

async function loadClips() {
    try {
        const clips = await API.get('clips');
        const list = document.getElementById('clips-list');
        if (!list) return;

        if (clips.length === 0) {
            list.innerHTML = '<p style="color: var(--text-muted)">No match clips available yet...</p>';
            return;
        }

        list.innerHTML = clips.map(c => {
            const sizeMB = (c.size / (1024 * 1024)).toFixed(1);
            return `<div class="clip-item">
                <a href="/${c.name}" target="_blank">${c.name}</a>
                <span class="clip-size">${sizeMB} MB</span>
            </div>`;
        }).join('');
    } catch(e) {}
}

// --- Config form ---
function populateConfigForm(cfg) {
    setVal('cfg-event-code', cfg.event_code);
    setVal('cfg-scoring-host', cfg.scoring_host);
    setVal('cfg-scoring-port', cfg.scoring_port);
    setVal('cfg-obs-host', cfg.obs_host);
    setVal('cfg-obs-port', cfg.obs_port);
    setVal('cfg-obs-password', cfg.obs_password);
    setVal('cfg-output-dir', cfg.output_dir);
    setVal('cfg-mdns-name', cfg.mdns_name);
    setVal('cfg-web-port', cfg.web_port);
    setVal('cfg-pre-buffer', cfg.pre_match_buffer_seconds);
    setVal('cfg-post-buffer', cfg.post_match_buffer_seconds);
    setVal('cfg-match-duration', cfg.match_duration_seconds);

    // Scene mappings
    const fsm = cfg.field_scene_mapping || {};
    setVal('cfg-scene-1', fsm['1'] || fsm[1] || '');
    setVal('cfg-scene-2', fsm['2'] || fsm[2] || '');
    setVal('cfg-scene-3', fsm['3'] || fsm[3] || '');

    // Rsync
    setVal('cfg-rsync-host', cfg.rsync_host);
    setVal('cfg-rsync-module', cfg.rsync_module);
    setVal('cfg-rsync-username', cfg.rsync_username);
    setVal('cfg-rsync-password', cfg.rsync_password);
    setVal('cfg-rsync-interval', cfg.rsync_interval_seconds);

    // Tunnel
    setVal('cfg-tunnel-relay-url', cfg.tunnel_relay_url);
    setVal('cfg-tunnel-token', cfg.tunnel_token);
}

function getConfigFromForm() {
    return {
        event_code: getVal('cfg-event-code'),
        scoring_host: getVal('cfg-scoring-host'),
        scoring_port: parseInt(getVal('cfg-scoring-port')) || 80,
        obs_host: getVal('cfg-obs-host'),
        obs_port: parseInt(getVal('cfg-obs-port')) || 4455,
        obs_password: getVal('cfg-obs-password'),
        output_dir: getVal('cfg-output-dir'),
        mdns_name: getVal('cfg-mdns-name'),
        web_port: parseInt(getVal('cfg-web-port')) || 80,
        pre_match_buffer_seconds: parseInt(getVal('cfg-pre-buffer')) || 10,
        post_match_buffer_seconds: parseInt(getVal('cfg-post-buffer')) || 10,
        match_duration_seconds: parseInt(getVal('cfg-match-duration')) || 158,
        field_scene_mapping: {
            1: getVal('cfg-scene-1'),
            2: getVal('cfg-scene-2'),
            3: getVal('cfg-scene-3'),
        },
        rsync_host: getVal('cfg-rsync-host'),
        rsync_module: getVal('cfg-rsync-module'),
        rsync_username: getVal('cfg-rsync-username'),
        rsync_password: getVal('cfg-rsync-password'),
        rsync_interval_seconds: parseInt(getVal('cfg-rsync-interval')) || 60,
        tunnel_relay_url: getVal('cfg-tunnel-relay-url'),
        tunnel_token: getVal('cfg-tunnel-token'),
    };
}

function setVal(id, val) {
    const el = document.getElementById(id);
    if (el) el.value = val != null ? val : '';
}

function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
}

// --- Actions ---
async function saveConfig() {
    try {
        const cfg = getConfigFromForm();
        await API.post('config', cfg);
        const result = await API.post('save-config');
        alert(result.ok ? 'Configuration saved!' : 'Error: ' + (result.error || 'Unknown'));
    } catch(e) {
        alert('Error saving config: ' + e.message);
    }
}

async function configureObs() {
    try {
        const cfg = getConfigFromForm();
        await API.post('config', cfg);
        const result = await API.post('configure-obs');
        alert(result.ok ? 'OBS scenes configured!' : 'Failed to configure OBS scenes');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function startMatchbox() {
    try {
        const result = await API.post('start');
        if (!result.ok) {
            alert(result.message || result.error || 'Could not start');
        }
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function stopMatchbox() {
    try {
        await API.post('stop');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function startSync() {
    try {
        const result = await API.post('sync/start');
        if (!result.ok) alert(result.error || 'Could not start sync');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function stopSync() {
    try {
        const result = await API.post('sync/stop');
        if (!result.ok) alert(result.error || 'Could not stop sync');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function startTunnel() {
    try {
        const cfg = getConfigFromForm();
        await API.post('config', cfg);
        const result = await API.post('tunnel/start');
        if (!result.ok) alert(result.error || 'Could not start tunnel');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function stopTunnel() {
    try {
        const result = await API.post('tunnel/stop');
        if (!result.ok) alert(result.error || 'Could not stop tunnel');
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

// --- Log display ---
function appendLog(entry) {
    const container = document.getElementById('log-container');
    if (!container) return;

    const div = document.createElement('div');
    div.className = 'log-entry ' + (entry.level || 'INFO');
    div.textContent = `${entry.timestamp || ''} [${entry.level || 'INFO'}] ${entry.message || ''}`;
    container.appendChild(div);

    // Limit log entries to prevent memory issues
    while (container.children.length > 1000) {
        container.removeChild(container.firstChild);
    }

    if (autoScroll) {
        container.scrollTop = container.scrollHeight;
    }
}
