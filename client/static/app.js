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
                        <button class="btn btn-danger btn-icon" onclick="deleteFileById(${file.id})" title="Eliminar">🗑️</button>
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

// Variable global para almacenar intervalos de polling de progreso
const uploadProgressIntervals = {};
// Variable global para rastrear uploads activos (upload_id -> {filename, completed})
const activeUploads = {};

// Variable global para almacenar intervalos de polling de progreso de descarga
const downloadProgressIntervals = {};
// Variable global para rastrear descargas activas (download_id -> {filename, completed})
const activeDownloads = {};

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

async function checkUploadProgress(uploadId, filename) {
    try {
        const response = await fetch(`/api/upload-progress/${uploadId}`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });
        
        if (!response.ok) {
            console.error(`[TBFS] Error consultando progreso: ${response.status}`);
            return null;
        }
        
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('[TBFS] Error consultando progreso:', error);
        return null;
    }
}

function checkAllUploadsCompleted() {
    // Verificar si todos los uploads activos han terminado
    const allCompleted = Object.values(activeUploads).every(upload => upload.completed || upload.error);
    const hasActiveUploads = Object.keys(activeUploads).length > 0;
    const noPollingIntervals = Object.keys(uploadProgressIntervals).length === 0;
    
    if (allCompleted && hasActiveUploads && noPollingIntervals) {
        // Todos los uploads terminaron, limpiar selección y resetear card
        const progressBar = document.getElementById('upload-progress');
        const progressFill = document.getElementById('upload-progress-bar');
        const status = document.getElementById('upload-status');
        
        setTimeout(() => {
            clearSelection();
            // Limpiar tracking de uploads
            Object.keys(activeUploads).forEach(id => delete activeUploads[id]);
            // Asegurar que la barra de progreso esté oculta
            progressBar.style.display = 'none';
            progressFill.style.width = '0%';
            status.textContent = '';
        }, 2000);
    }
}

function startProgressPolling(uploadId, filename) {
    const progressBar = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-bar');
    const status = document.getElementById('upload-status');
    
    // Registrar este upload como activo
    activeUploads[uploadId] = { filename, completed: false, error: false };
    
    progressBar.style.display = 'block';
    status.textContent = `Iniciando subida de ${filename}...`;
    
    const pollInterval = setInterval(async () => {
        const progressData = await checkUploadProgress(uploadId, filename);
        
        if (!progressData || !progressData.success) {
            console.error('[TBFS] No se pudo obtener progreso');
            return;
        }
        
        const progress = progressData.progress || 0;
        const chunksUploaded = progressData.chunks_uploaded || 0;
        const totalChunks = progressData.total_chunks || 0;
        const bytesUploaded = progressData.bytes_uploaded || 0;
        const fileSize = progressData.file_size || 0;
        const statusText = progressData.status;
        
        // Actualizar barra de progreso (usar el máximo progreso de todos los uploads activos)
        let maxProgress = progress;
        Object.keys(activeUploads).forEach(id => {
            if (uploadProgressIntervals[id]) {
                // Si hay otros uploads activos, calcular progreso promedio
                // Por ahora usamos el progreso actual
            }
        });
        progressFill.style.width = `${maxProgress}%`;
        
        // Actualizar texto de estado según el estado actual
        let statusMessage = '';
        switch (statusText) {
            case 'initializing':
                statusMessage = `Iniciando subida de ${filename}...`;
                break;
            case 'uploading_chunks':
                statusMessage = `Subiendo ${filename}: ${chunksUploaded}/${totalChunks} chunks (${formatBytes(bytesUploaded)}/${formatBytes(fileSize)}) - ${progress.toFixed(1)}%`;
                break;
            case 'finalizing':
                statusMessage = `Finalizando en DataNode: ${filename}...`;
                break;
            case 'finalizing_namenode':
                statusMessage = `Completando metadatos: ${filename}...`;
                break;
            case 'completed':
                statusMessage = `✅ ${filename} subido correctamente`;
                clearInterval(pollInterval);
                delete uploadProgressIntervals[uploadId];
                // Marcar como completado
                if (activeUploads[uploadId]) {
                    activeUploads[uploadId].completed = true;
                }
                loadFiles();
                showMessage('main', `Archivo '${filename}' subido correctamente`, 'success');
                // Verificar si todos los uploads terminaron
                checkAllUploadsCompleted();
                // Si hay otros uploads activos, no ocultar la barra aún
                const otherActiveUploads = Object.keys(uploadProgressIntervals).length > 0;
                if (!otherActiveUploads) {
                    setTimeout(() => {
                        progressBar.style.display = 'none';
                        progressFill.style.width = '0%';
                        status.textContent = '';
                    }, 2000);
                }
                break;
            case 'error':
                const errorMsg = progressData.error || 'Error desconocido';
                statusMessage = `❌ Error: ${errorMsg}`;
                clearInterval(pollInterval);
                delete uploadProgressIntervals[uploadId];
                // Marcar como error
                if (activeUploads[uploadId]) {
                    activeUploads[uploadId].error = true;
                }
                showMessage('main', `Error subiendo ${filename}: ${errorMsg}`, 'error');
                // Verificar si todos los uploads terminaron
                checkAllUploadsCompleted();
                // Si hay otros uploads activos, no ocultar la barra aún
                const otherActiveUploadsOnError = Object.keys(uploadProgressIntervals).length > 0;
                if (!otherActiveUploadsOnError) {
                    setTimeout(() => {
                        progressBar.style.display = 'none';
                        progressFill.style.width = '0%';
                        status.textContent = '';
                    }, 5000);
                }
                break;
            default:
                statusMessage = `Subiendo ${filename}... (${progress.toFixed(1)}%)`;
        }
        
        status.textContent = statusMessage;
        
    }, 500); // Consultar cada 500ms para actualización fluida
    
    uploadProgressIntervals[uploadId] = pollInterval;
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
        status.textContent = `Preparando ${file.name}...`;
        
        const formData = new FormData();
        formData.append('file', file);
        formData.append('tags', tags);

        try {
            const response = await fetch('/api/upload', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${authToken}` },
                body: formData
            });

            // Verificar que la respuesta sea válida antes de parsear JSON
            if (!response.ok) {
                let errorMsg = `Error HTTP ${response.status}`;
                try {
                    const errorData = await response.json();
                    errorMsg = errorData.error || errorMsg;
                } catch (e) {
                    const text = await response.text();
                    errorMsg = text || errorMsg;
                }
                showMessage('main', `Error subiendo ${file.name}: ${errorMsg}`, 'error');
                continue;
            }

            let data;
            try {
                data = await response.json();
            } catch (e) {
                console.error('Error parseando respuesta JSON:', e);
                showMessage('main', `Error subiendo ${file.name}: Respuesta inválida del servidor`, 'error');
                continue;
            }

            if (data.success) {
                // Si es un upload chunked, recibimos upload_id y debemos consultar progreso
                if (data.upload_id) {
                    // Archivo grande - usar polling de progreso
                    startProgressPolling(data.upload_id, file.name);
                    uploaded++; // Contamos como iniciado
                } else {
                    // Archivo pequeño - subida directa completada
                    uploaded++;
                    progressFill.style.width = `${(uploaded / total) * 100}%`;
                    if (uploaded === total) {
                        status.textContent = `✅ ${uploaded} archivo(s) subido(s) correctamente`;
                        setTimeout(() => {
                            progressBar.style.display = 'none';
                            progressFill.style.width = '0%';
                            status.textContent = '';
                            clearSelection();
                            loadFiles();
                        }, 2000);
                    }
                }
            } else {
                const errorMsg = typeof data.error === 'object' ? JSON.stringify(data.error) : (data.error || 'Error desconocido');
                showMessage('main', `Error subiendo ${file.name}: ${errorMsg}`, 'error');
            }
        } catch (error) {
            console.error('Error en upload:', error);
            showMessage('main', `Error subiendo ${file.name}: ${error.message || 'Error de conexión'}`, 'error');
        }
    }

    // Si todos los archivos son pequeños y se completaron, mostrar mensaje y limpiar
    if (uploaded === total && Object.keys(uploadProgressIntervals).length === 0) {
        showMessage('main', `${uploaded} archivo(s) subido(s) correctamente`, 'success');
        setTimeout(() => {
            clearSelection();
            loadFiles();
        }, 2000);
    }
    // Si hay uploads chunked en progreso, no limpiar aún
    // Se limpiará cuando todos terminen en checkAllUploadsCompleted()
}

// ============ DOWNLOAD ============

async function checkDownloadProgress(downloadId, filename) {
    try {
        const response = await fetch(`/api/download-progress/${downloadId}`, {
            headers: { 'Authorization': `Bearer ${authToken}` }
        });
        
        if (!response.ok) {
            console.error(`[TBFS] Error consultando progreso de descarga: ${response.status}`);
            return null;
        }
        
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('[TBFS] Error consultando progreso de descarga:', error);
        return null;
    }
}

function startDownloadProgressPolling(downloadId, filename) {
    const progressBar = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-bar');
    const status = document.getElementById('upload-status');
    
    progressBar.style.display = 'block';
    status.textContent = `Iniciando descarga de ${filename}...`;
    
    const pollInterval = setInterval(async () => {
        const progressData = await checkDownloadProgress(downloadId, filename);
        
        if (!progressData || !progressData.success) {
            console.error('[TBFS] No se pudo obtener progreso de descarga');
            return;
        }
        
        const progress = progressData.progress || 0;
        const chunksDownloaded = progressData.chunks_downloaded || 0;
        const totalChunks = progressData.total_chunks || 0;
        const bytesDownloaded = progressData.bytes_downloaded || 0;
        const fileSize = progressData.file_size || 0;
        const statusText = progressData.status;
        
        // Actualizar barra de progreso
        progressFill.style.width = `${progress}%`;
        
        // Actualizar texto de estado según el estado actual
        let statusMessage = '';
        switch (statusText) {
            case 'initializing':
                statusMessage = `Iniciando descarga de ${filename}...`;
                break;
            case 'downloading_chunks':
                statusMessage = `Descargando ${filename}: ${chunksDownloaded}/${totalChunks} chunks (${formatBytes(bytesDownloaded)}/${formatBytes(fileSize)}) - ${progress.toFixed(1)}%`;
                break;
            case 'assembling':
                statusMessage = `Ensamblando archivo: ${filename}...`;
                break;
            case 'completed':
                const finalFilename = progressData.final_filename || filename;
                statusMessage = `✅ ${finalFilename} descargado correctamente en Downloads`;
                clearInterval(pollInterval);
                delete downloadProgressIntervals[downloadId];
                setTimeout(() => {
                    progressBar.style.display = 'none';
                    progressFill.style.width = '0%';
                    status.textContent = '';
                }, 3000);
                showMessage('main', `Archivo '${finalFilename}' descargado correctamente en tu carpeta Downloads`, 'success');
                break;
            case 'error':
                const errorMsg = progressData.error || 'Error desconocido';
                statusMessage = `❌ Error: ${errorMsg}`;
                clearInterval(pollInterval);
                delete downloadProgressIntervals[downloadId];
                setTimeout(() => {
                    progressBar.style.display = 'none';
                    progressFill.style.width = '0%';
                    status.textContent = '';
                }, 5000);
                showMessage('main', `Error descargando ${filename}: ${errorMsg}`, 'error');
                break;
            default:
                statusMessage = `Descargando ${filename}... (${progress.toFixed(1)}%)`;
        }
        
        status.textContent = statusMessage;
        
    }, 500); // Consultar cada 500ms para actualización fluida
    
    downloadProgressIntervals[downloadId] = pollInterval;
}

async function downloadFile(filename) {
    try {
        const response = await fetch(`/api/download/${encodeURIComponent(filename)}`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${authToken}` }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ error: 'Error en descarga' }));
            showMessage('main', `Error descargando ${filename}: ${errorData.error || 'Error desconocido'}`, 'error');
            return;
        }

        const data = await response.json();

        if (data.success) {
            // Si es una descarga por chunks, recibimos download_id y debemos consultar progreso
            if (data.download_id) {
                // Archivo grande - usar polling de progreso
                startDownloadProgressPolling(data.download_id, filename);
                showMessage('main', `Descarga iniciada para '${filename}'`, 'info');
            } else {
                // Archivo pequeño - descarga directa (compatibilidad hacia atrás)
                showMessage('main', `Descarga completada: ${filename}`, 'success');
            }
        } else {
            const errorMsg = data.error || 'Error desconocido';
            showMessage('main', `Error descargando ${filename}: ${errorMsg}`, 'error');
        }
    } catch (error) {
        console.error('Error en descarga:', error);
        showMessage('main', `Error descargando ${filename}: ${error.message || 'Error de conexión'}`, 'error');
    }
}

// ============ DELETE ============

async function deleteFileById(fileId) {
    console.log('[TBFS] deleteFileById llamada con:', fileId);
    
    try {
        const response = await fetch(`/api/delete/${fileId}`, {
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
        console.error('[TBFS] Error:', error);
        showMessage('main', 'Error al eliminar archivo', 'error');
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

// ============ TAG OPERATIONS ============

function toggleTagOperations() {
    const container = document.getElementById('tag-operations');
    const icon = document.getElementById('tag-ops-icon');
    
    if (container.style.display === 'none') {
        container.style.display = 'grid';
        icon.classList.add('open');
    } else {
        container.style.display = 'none';
        icon.classList.remove('open');
    }
}

async function addTags() {
    const queryTags = document.getElementById('add-tags-query').value.trim();
    const newTags = document.getElementById('add-tags-new').value.trim();
    
    if (!queryTags || !newTags) {
        showMessage('main', 'Debes especificar las etiquetas de búsqueda y las nuevas etiquetas', 'error');
        return;
    }
    
    try {
        const response = await fetch('/api/add-tags', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ query_tags: queryTags, new_tags: newTags })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showMessage('main', data.message, 'success');
            document.getElementById('add-tags-query').value = '';
            document.getElementById('add-tags-new').value = '';
            loadFiles();
        } else {
            showMessage('main', data.error, 'error');
        }
    } catch (error) {
        showMessage('main', 'Error al agregar etiquetas', 'error');
    }
}

async function removeTags() {
    const queryTags = document.getElementById('del-tags-query').value.trim();
    const delTags = document.getElementById('del-tags-remove').value.trim();
    
    if (!queryTags || !delTags) {
        showMessage('main', 'Debes especificar las etiquetas de búsqueda y las etiquetas a eliminar', 'error');
        return;
    }
    
    try {
        const response = await fetch('/api/delete-tags', {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${authToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ query_tags: queryTags, del_tags: delTags })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showMessage('main', data.message, 'success');
            document.getElementById('del-tags-query').value = '';
            document.getElementById('del-tags-remove').value = '';
            loadFiles();
        } else {
            showMessage('main', data.error, 'error');
        }
    } catch (error) {
        showMessage('main', 'Error al eliminar etiquetas', 'error');
    }
}

async function deleteFilesByTags() {
    console.log('[TBFS] deleteFilesByTags llamada');
    
    const tagsInput = document.getElementById('del-files-tags');
    console.log('[TBFS] Input element:', tagsInput);
    
    if (!tagsInput) {
        console.error('[TBFS] No se encontró el elemento del-files-tags');
        showMessage('main', 'Error interno: campo no encontrado', 'error');
        return;
    }
    
    const tags = tagsInput.value.trim();
    console.log('[TBFS] Tags ingresadas:', tags);
    
    if (!tags) {
        showMessage('main', 'Debes especificar las etiquetas de los archivos a eliminar', 'error');
        return;
    }
    
    console.log('[TBFS] Enviando petición DELETE...');
    
    try {
        const response = await fetch(`/api/delete-by-tags?tags=${encodeURIComponent(tags)}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${authToken}` }
        });
        
        console.log('[TBFS] Respuesta status:', response.status);
        const data = await response.json();
        console.log('[TBFS] Respuesta data:', data);
        
        if (data.success) {
            showMessage('main', data.message, 'success');
            tagsInput.value = '';
            loadFiles();
        } else {
            showMessage('main', data.error, 'error');
        }
    } catch (error) {
        console.error('[TBFS] Error:', error);
        showMessage('main', 'Error al eliminar archivos: ' + error.message, 'error');
    }
}

// Verificar estado cada 30 segundos
setInterval(checkStatus, 30000);
