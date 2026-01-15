// TBFS - JavaScript principal

// Estado de la aplicación
let authToken = localStorage.getItem('tbfs_token');
let currentUser = localStorage.getItem('tbfs_user');
let selectedFiles = [];

// ============ INICIALIZACIÓN ============

document.addEventListener('DOMContentLoaded', () => {
    setupDragAndDrop();
    checkAuth();
});

// ============ AUTENTICACIÓN ============

function checkAuth() {
    if (authToken && currentUser) {
        showMainSection();
        loadFiles();
        checkStatus();
    } else {
        showAuthSection();
    }
}

function showAuthSection() {
    document.getElementById('auth-section').style.display = 'block';
    document.getElementById('main-section').style.display = 'none';
}

function showMainSection() {
    document.getElementById('auth-section').style.display = 'none';
    document.getElementById('main-section').style.display = 'block';
    document.getElementById('username-display').textContent = currentUser;
}

function showLogin() {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.auth-tab')[0].classList.add('active');
    document.getElementById('login-form').style.display = 'block';
    document.getElementById('register-form').style.display = 'none';
}

function showRegister() {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.auth-tab')[1].classList.add('active');
    document.getElementById('login-form').style.display = 'none';
    document.getElementById('register-form').style.display = 'block';
}

async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;

    try {
        const response = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        const data = await response.json();

        if (data.success) {
            authToken = data.token;
            currentUser = data.username;
            localStorage.setItem('tbfs_token', authToken);
            localStorage.setItem('tbfs_user', currentUser);
            showMainSection();
            loadFiles();
            checkStatus();
        } else {
            showMessage('auth', data.error, 'error');
        }
    } catch (error) {
        showMessage('auth', 'Error de conexión', 'error');
    }
}

async function handleRegister(e) {
    e.preventDefault();
    const username = document.getElementById('register-username').value;
    const password = document.getElementById('register-password').value;
    const confirm = document.getElementById('register-password-confirm').value;

    if (password !== confirm) {
        showMessage('auth', 'Las contraseñas no coinciden', 'error');
        return;
    }

    try {
        const response = await fetch('/api/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        const data = await response.json();

        if (data.success) {
            showMessage('auth', 'Cuenta creada. Ahora puedes iniciar sesión.', 'success');
            showLogin();
        } else {
            showMessage('auth', data.error, 'error');
        }
    } catch (error) {
        showMessage('auth', 'Error de conexión', 'error');
    }
}

function handleLogout() {
    authToken = null;
    currentUser = null;
    localStorage.removeItem('tbfs_token');
    localStorage.removeItem('tbfs_user');
    showAuthSection();
}

// ============ ARCHIVOS ============

async function loadFiles() {
    const container = document.getElementById('files-container');
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Cargando archivos...</p></div>';

    const tags = document.getElementById('search-tags').value;

    try {
        const url = tags ? `/api/files?tags=${encodeURIComponent(tags)}` : '/api/files';
        console.log('[TBFS] Cargando archivos desde:', url);
        
        const response = await fetch(url, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        console.log('[TBFS] Status:', response.status);
        const data = await response.json();
        console.log('[TBFS] Respuesta:', data);

        if (data.success) {
            renderFiles(data.files);
        } else {
            container.innerHTML = `<div class="empty-state"><div class="icon">❌</div><p>${escapeHtml(data.error || 'Error desconocido')}</p></div>`;
        }
    } catch (error) {
        console.error('[TBFS] Error cargando archivos:', error);
        container.innerHTML = `<div class="empty-state"><div class="icon">❌</div><p>Error al cargar archivos: ${escapeHtml(error.message)}</p></div>`;
    }
}

function renderFiles(files) {
    const container = document.getElementById('files-container');

    if (!files || files.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="icon">📂</div><p>No hay archivos</p><p style="color: #555;">Sube tu primer archivo</p></div>';
        return;
    }

    let html = `
        <table class="files-table">
            <thead>
                <tr>
                    <th>Nombre</th>
                    <th>Etiquetas</th>
                    <th>Acciones</th>
                </tr>
            </thead>
            <tbody>
    `;

    for (const file of files) {
        const name = file.name || 'Sin nombre';
        // tags puede venir como string o array
        let tags = file.tags || [];
        if (typeof tags === 'string') {
            tags = tags.split(',').map(t => t.trim()).filter(t => t);
        }
        const tagsHtml = tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');

        html += `
            <tr>
                <td>${escapeHtml(name)}</td>
                <td><div class="tags">${tagsHtml || '<span style="color: #666;">Sin etiquetas</span>'}</div></td>
                <td>
                    <div class="file-actions">
                        <button class="btn btn-primary btn-icon" onclick="downloadFile('${escapeHtml(name)}')" title="Descargar">📥</button>
                        <button class="btn btn-danger btn-icon" onclick="deleteFile('${escapeHtml(name)}')" title="Eliminar">🗑️</button>
                    </div>
                </td>
            </tr>
        `;
    }

    html += '</tbody></table>';
    container.innerHTML = html;
}

function handleSearch(e) {
    if (e.key === 'Enter') {
        loadFiles();
    }
}

function clearSearch() {
    document.getElementById('search-tags').value = '';
    loadFiles();
}

// ============ UPLOAD ============

function setupDragAndDrop() {
    const uploadArea = document.getElementById('upload-area');
    if (!uploadArea) return;

    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });

    uploadArea.addEventListener('dragleave', () => {
        uploadArea.classList.remove('dragover');
    });

    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFileSelect({ target: { files } });
        }
    });
}

function handleFileSelect(e) {
    const files = Array.from(e.target.files);
    if (files.length > 0) {
        selectedFiles = files;
        showSelectedFiles();
    }
}

function showSelectedFiles() {
    document.getElementById('upload-step-1').style.display = 'none';
    document.getElementById('upload-step-2').style.display = 'block';
    
    const list = document.getElementById('selected-files-list');
    let html = '<ul>';
    
    let totalSize = 0;
    for (const file of selectedFiles) {
        totalSize += file.size;
        const sizeStr = formatFileSize(file.size);
        html += `<li>
            <span>📄 ${escapeHtml(file.name)}</span>
            <span class="file-size">${sizeStr}</span>
        </li>`;
    }
    html += '</ul>';
    html += `<p style="color: #666; margin-top: 10px;">Total: ${selectedFiles.length} archivo(s), ${formatFileSize(totalSize)}</p>`;
    
    list.innerHTML = html;
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function clearSelection() {
    selectedFiles = [];
    document.getElementById('file-input').value = '';
    document.getElementById('upload-tags').value = '';
    document.getElementById('upload-step-1').style.display = 'block';
    document.getElementById('upload-step-2').style.display = 'none';
}

async function startUpload() {
    if (selectedFiles.length === 0) {
        showMessage('main', 'No hay archivos seleccionados', 'error');
        return;
    }
    
    const tags = document.getElementById('upload-tags').value;
    const progressBar = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-bar');
    const status = document.getElementById('upload-status');

    progressBar.style.display = 'block';
    
    let uploaded = 0;
    const total = selectedFiles.length;

    for (const file of selectedFiles) {
        status.textContent = `Subiendo ${file.name}...`;
        
        const formData = new FormData();
        formData.append('file', file);
        formData.append('tags', tags);

        try {
            const response = await fetch('/api/upload', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${authToken}` },
                body: formData
            });

            const data = await response.json();

            if (data.success) {
                uploaded++;
                progressFill.style.width = `${(uploaded / total) * 100}%`;
            } else {
                const errorMsg = typeof data.error === 'object' ? JSON.stringify(data.error) : (data.error || 'Error desconocido');
                showMessage('main', `Error subiendo ${file.name}: ${errorMsg}`, 'error');
            }
        } catch (error) {
            console.error('Error en upload:', error);
            showMessage('main', `Error subiendo ${file.name}: ${error.message || 'Error de conexión'}`, 'error');
        }
    }

    if (uploaded === total) {
        showMessage('main', `${uploaded} archivo(s) subido(s) correctamente`, 'success');
    } else if (uploaded > 0) {
        showMessage('main', `${uploaded} de ${total} archivo(s) subido(s)`, 'success');
    }

    status.textContent = '';
    progressBar.style.display = 'none';
    progressFill.style.width = '0%';
    
    clearSelection();
    loadFiles();
}

// ============ DOWNLOAD ============

function downloadFile(filename) {
    fetch(`/api/download/${encodeURIComponent(filename)}`, {
        headers: { 'Authorization': `Bearer ${authToken}` }
    })
    .then(response => {
        if (!response.ok) throw new Error('Error en descarga');
        return response.blob();
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
    })
    .catch(error => {
        showMessage('main', `Error descargando ${filename}`, 'error');
    });
}

// ============ DELETE ============

async function deleteFile(filename) {
    if (!confirm(`¿Eliminar "${filename}"?`)) return;

    try {
        const response = await fetch(`/api/delete/${encodeURIComponent(filename)}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        const data = await response.json();

        if (data.success) {
            showMessage('main', data.message, 'success');
            loadFiles();
        } else {
            showMessage('main', data.error, 'error');
        }
    } catch (error) {
        showMessage('main', 'Error al eliminar', 'error');
    }
}

// ============ STATUS ============

async function checkStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        const indicator = document.getElementById('status-indicator');
        if (data.connected) {
            indicator.classList.add('connected');
        } else {
            indicator.classList.remove('connected');
        }
    } catch (error) {
        document.getElementById('status-indicator').classList.remove('connected');
    }
}

// ============ UTILIDADES ============

function showMessage(section, text, type) {
    const el = document.getElementById(`message-${section}`);
    el.textContent = text;
    el.className = `message ${type}`;
    el.style.display = 'block';
    
    setTimeout(() => {
        el.style.display = 'none';
    }, 5000);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Verificar estado cada 30 segundos
setInterval(checkStatus, 30000);
