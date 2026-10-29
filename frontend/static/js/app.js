/**
 * BauduSync - Cloud Sync Frontend Application
 * UI inspired by baidu-netdisk-node
 */

const App = {
    // State
    connections: [],
    tasks: [],
    currentView: 'dashboard',
    editingTaskId: null,
    browserMode: null,
    browserPath: '/',
    browserRoot: 'sync',
    browserSelection: '/',
    poll115Timer: null,
    progressTimer: null,
    _prevTransfers: {},  // {transfer_id: {transferred, time}} for per-file speed

    // ---- Initialization ----
    init() {
        this.bindNav();
        this.loadAll();
        this.startProgressPolling();
        // Leaving the drawer open while rotating to landscape would cover the
        // page with a 250px-wide panel, so close it once a desktop width is back.
        window.addEventListener('resize', () => {
            if (window.innerWidth > 900) this.closeSidebar();
        });
    },

    bindNav() {
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                this.switchView(item.dataset.view);
            });
        });

        document.querySelectorAll('input[name="schedule-type"]').forEach(radio => {
            radio.addEventListener('change', () => {
                const group = document.getElementById('schedule-interval-group');
                group.style.display = radio.value === 'scheduled' ? 'block' : 'none';
            });
        });
    },

    // ---- Mobile sidebar drawer ----
    toggleSidebar(force) {
        const sidebar = document.querySelector('.sidebar');
        const backdrop = document.getElementById('nav-backdrop');
        if (!sidebar) return;
        const open = (force === undefined)
            ? !sidebar.classList.contains('open') : !!force;
        sidebar.classList.toggle('open', open);
        if (backdrop) backdrop.classList.toggle('show', open);
        document.body.classList.toggle('nav-open', open);
    },

    closeSidebar() {
        this.toggleSidebar(false);
    },

    switchView(view) {
        this.currentView = view;
        this.closeSidebar();
        // Map sub-views to their parent nav item for active highlighting
        // (任务管理 is reached from 全局设置 → 同步设置, and is now the only
        // place where a task can be edited or deleted).
        const navMap = { logs: 'settings', tasks: 'settings' };
        const activeNav = navMap[view] || view;
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.toggle('active', item.dataset.view === activeNav);
        });
        document.querySelectorAll('.view').forEach(v => {
            v.classList.toggle('active', v.id === `view-${view}`);
        });

        if (view === 'dashboard') this.loadDashboard();
        if (view === 'cloud-files') this.loadCloudFilesView();
        if (view === 'downloads') this.loadDownloads();
        if (view === 'tasks') this.loadTasks();
        if (view === 'logs') this.loadLogs();
        if (view === 'settings') this.loadSettingsView();
        if (view === 'about') this.loadAboutView();
    },

    // ---- API Helpers ----
    async api(method, path, body = null) {
        const opts = { method, headers: {} };
        if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
        if (!resp.ok) {
            let msg = `HTTP ${resp.status}`;
            try {
                const err = await resp.json();
                msg = err.detail || err.message || msg;
            } catch (e) {}
            throw new Error(msg);
        }
        const text = await resp.text();
        return text ? JSON.parse(text) : {};
    },

    toast(msg, type = 'info') {
        const container = document.getElementById('toast-container');
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.textContent = msg;
        container.appendChild(el);
        setTimeout(() => el.remove(), 4000);
    },

    formatSize(bytes) {
        if (!bytes) return '0 B';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
        return (bytes / 1073741824).toFixed(2) + ' GB';
    },

    formatTime(ts) {
        if (!ts) return '-';
        const d = new Date(ts * 1000);
        return d.toLocaleString('zh-CN');
    },

    formatSpeed(bps) {
        if (!bps || bps < 1) return '0 B/s';
        if (bps < 1024) return bps.toFixed(0) + ' B/s';
        if (bps < 1048576) return (bps / 1024).toFixed(1) + ' KB/s';
        if (bps < 1073741824) return (bps / 1048576).toFixed(1) + ' MB/s';
        return (bps / 1073741824).toFixed(2) + ' GB/s';
    },

    basename(path) {
        if (!path) return '';
        const parts = path.replace(/\\/g, '/').split('/');
        return parts[parts.length - 1] || '';
    },

    escape(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    },

    // For values embedded in a single-quoted inline handler: escape() does not
    // touch quotes, and a file name with an apostrophe would break the call.
    escapeAttr(str) {
        return String(str == null ? '' : str)
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '&quot;')
            .replace(/[\r\n]/g, ' ');
    },

    // ---- Dashboard ----
    async loadDashboard() {
        await this.loadTasks();
        const tasks = this.tasks;

        // Render sync cards
        const container = document.getElementById('sync-cards');
        if (tasks.length === 0) {
            container.innerHTML = `
                <div class="empty-state" style="padding:64px 20px;">
                    <div style="font-size:48px;margin-bottom:12px;opacity:0.3;">📂</div>
                    <div>暂无同步任务</div>
                    <button class="btn-add" style="margin-top:16px;" onclick="app.openCreateTaskDialog()">
                        <span style="font-size:18px;font-weight:300;line-height:1;margin-right:2px">+</span>
                        新建任务
                    </button>
                </div>`;
        } else {
            container.innerHTML = tasks.map(t => this.renderSyncCard(t)).join('');
        }

        // Disk usage + build version + the traffic strip. All cheap, and firing
        // them together keeps the 1s poll at a single round trip.
        const [infoRes] = await Promise.allSettled([
            this.api('GET', '/api/system/info'),
            this.refreshTraffic(),
        ]);
        if (infoRes.status === 'fulfilled') {
            const info = infoRes.value;
            const disk = info.disk;
            document.getElementById('disk-usage-text').textContent =
                `${this.formatSize(disk.used)} / ${this.formatSize(disk.total)}`;
            document.getElementById('disk-bar-fill').style.width = disk.usage_percent + '%';
            const verEl = document.getElementById('sidebar-version');
            if (verEl && info.version) verEl.textContent = info.version;
        }
    },

    // ---- Traffic strip (real-time speeds + cumulative usage) ----
    /** Split "12.3 MB/s" into value and unit so they can be styled apart. */
    _splitUnit(text) {
        const i = String(text).lastIndexOf(' ');
        return i < 0 ? [String(text), ''] : [text.slice(0, i), text.slice(i + 1)];
    },

    _renderMetric(id, text) {
        const el = document.getElementById(id);
        if (!el) return;
        const [val, unit] = this._splitUnit(text);
        el.innerHTML = `<span class="stat-val">${this.escape(val)}</span>` +
            (unit ? `<span class="stat-unit">${this.escape(unit)}</span>` : '');
    },

    async refreshTraffic() {
        try {
            const t = await this.api('GET', '/api/system/traffic');
            this._renderMetric('stat-up-speed', this.formatSpeed(t.upload_speed || 0));
            this._renderMetric('stat-down-speed', this.formatSpeed(t.download_speed || 0));
            this._renderMetric('stat-traffic', this.formatSize(t.total || 0));
            const label = document.getElementById('stat-traffic-label');
            if (label) {
                label.title = `上传 ${this.formatSize(t.upload_total || 0)} / `
                    + `下载 ${this.formatSize(t.download_total || 0)}`;
            }
        } catch (e) {
            // The strip is informational — never let it break the dashboard.
        }
    },

    async clearTrafficStats() {
        if (!confirm('清空累计流量统计？实时速度显示不受影响。')) return;
        try {
            await this.api('POST', '/api/system/traffic/reset');
            await this.refreshTraffic();
            this.toast('流量统计已清空', 'success');
        } catch (e) {
            this.toast('清空失败: ' + e.message, 'error');
        }
    },

    renderSyncCard(task) {
        const dirMap = {
            bidirectional: { badge: 'badge-both', text: '⇅ 双向', arrow: 'both' },
            upload_only: { badge: 'badge-up', text: '↑ 上传', arrow: 'upload' },
            download_only: { badge: 'badge-down', text: '↓ 下载', arrow: 'download' }
        };
        const schedMap = {
            realtime: { badge: 'badge-realtime', text: '实时' },
            scheduled: { badge: 'badge-scheduled', text: '定时' },
            manual: { badge: 'badge-manual', text: '手动' }
        };
        const dir = dirMap[task.sync_mode] || dirMap.bidirectional;
        const sched = schedMap[task.schedule_type] || schedMap.manual;

        const isActive = task.is_running;
        const dotClass = isActive ? 'active' : (task.status === 'error' ? 'error' :
                          task.status === 'success' ? 'success' : 'idle');

        // Current runtime state label (separate from schedule_type)
        let stateBadge = '';
        if (isActive) {
            stateBadge = `<span class="badge badge-state-running">● 同步中</span>`;
        } else if (task.status === 'paused') {
            stateBadge = `<span class="badge badge-state-paused">⏸ 暂停中</span>`;
        } else if (task.schedule_type === 'manual') {
            stateBadge = `<span class="badge badge-state-waiting">○ 等待中</span>`;
        } else {
            stateBadge = `<span class="badge badge-state-idle">○ 空闲</span>`;
        }

        // Arrow SVG
        // Sizes live in CSS (.path-arrow svg) so the mobile breakpoint can
        // shrink them without fighting inline styles.
        const arrowSvg = {
            upload: `<svg class="arrow-1" viewBox="0 0 20 12" fill="none"><path d="M1 6H18M18 6L13 1M18 6L13 11" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            download: `<svg class="arrow-1" viewBox="0 0 20 12" fill="none"><path d="M19 6H2M2 6L7 1M2 6L7 11" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
            both: `<svg class="arrow-2" viewBox="0 0 24 12" fill="none"><path d="M1 6H22M22 6L17 1M22 6L17 11M1 6L6 1M1 6L6 11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`
        };

        // Progress info
        const p = task.progress || {};
        const totalFiles = p.total_files || 0;
        const processedFiles = p.processed_files || 0;
        const failedFiles = p.failed_files || 0;
        const skippedFiles = p.skipped_files || 0;
        const noopFiles = p.noop_files || 0;
        // Completed = synced + failed + skipped. The backend sends this (and
        // the percentage) directly; deriving it here from processed_files
        // alone is what produced "0/83 · 100%". Clamp it to the denominator so
        // a stale/legacy payload can never render "63/9".
        const rawCompleted = (p.completed_files !== undefined && p.completed_files !== null)
            ? p.completed_files
            : (processedFiles + skippedFiles + failedFiles);
        const completedFiles = totalFiles > 0
            ? Math.min(rawCompleted, totalFiles) : rawCompleted;
        const overallPct = (p.percent !== undefined && p.percent !== null)
            ? p.percent
            : (totalFiles > 0
                ? Math.min(100, Math.round(completedFiles / totalFiles * 100)) : 0);
        const phase = p.phase || '';

        // Active transfers
        const transfers = p.active_transfers || [];

        let transfersHtml = '';
        if (isActive && transfers.length > 0) {
            transfersHtml = transfers.map(t => {
                const pct = t.file_size > 0 ? Math.round(t.transferred / t.file_size * 100) : 0;
                const actionIcon = t.action === 'upload' ? '⬆' : '⬇';
                const actionName = t.action === 'upload' ? '上传' : '下载';
                const progressClass = t.action === 'upload' ? 'progress-fill-upload' : 'progress-fill-download';
                const dirTagClass = t.action === 'upload' ? 'dir-tag-up' : 'dir-tag-down';

                // Per-file speed is now computed server-side in the API
                // endpoint (see _transfers_with_speed in app/api/tasks.py),
                // which is far more accurate than computing on the frontend
                // because the server can sample at any moment and the
                // backend already tracks transferred bytes atomically.
                const fileSpeed = t.speed || 0;

                return `
                    <div class="transfer-widget">
                        <div class="transfer-top">
                            <div class="transfer-file-info">
                                <div class="transfer-file-icon">${actionIcon}</div>
                                <div class="transfer-filename">${this.escape(this.basename(t.file_path))}</div>
                            </div>
                            <button class="transfer-cancel-btn" title="跳过此文件（可在传输记录里重试）" onclick="app.skipFile(${task.id}, ${t.transfer_id}, '${this.escapeAttr(t.file_path || '')}', '${t.action || ''}', ${t.file_size || 0})">
                                <svg viewBox="0 0 24 24" width="12" height="12"><path fill="currentColor" d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
                            </button>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill ${progressClass}" style="width:${pct}%"></div>
                        </div>
                        <div class="transfer-bottom">
                            <div class="transfer-status">
                                <div class="loader" style="width:12px;height:12px;"></div>
                                <span class="transfer-status-text">${actionName}中</span>
                                <span class="transfer-size">${this.formatSize(t.transferred)} / ${this.formatSize(t.file_size)}</span>
                            </div>
                            <div class="transfer-right-info">
                                <span class="transfer-speed">${this.formatSpeed(fileSpeed)}</span>
                                <span class="dir-tag ${dirTagClass}">${actionName}</span>
                            </div>
                        </div>
                    </div>`;
            }).join('');
            // Clean up stale entries
            const activeIds = new Set(transfers.map(t => t.transfer_id || t.file_path));
            for (const k of Object.keys(this._prevTransfers)) {
                if (!activeIds.has(k)) delete this._prevTransfers[k];
            }
        } else if (isActive) {
            transfersHtml = `<div class="empty-hint">${phase === 'scanning' ? '扫描文件中...' : '处理中...'}</div>`;
        } else if (task.status === 'error') {
            transfersHtml = `<div class="empty-hint" style="color:var(--color-error)">
                ${task.last_error ? this.escape(task.last_error) : '同步出错'}</div>`;
        } else {
            transfersHtml = `<div class="empty-hint">无任务</div>`;
        }

        // Overall progress footer
        let progressFooter = '';
        if (isActive) {
            let label;
            if (totalFiles > 0) {
                label = `${completedFiles}/${totalFiles} · ${overallPct}%`;
                if (noopFiles > 0) label += ` · 无变化 ${noopFiles}`;
                if (failedFiles > 0) label += ` · 失败 ${failedFiles}`;
            } else if (phase === 'scanning') {
                label = '扫描文件中…';
            } else if (noopFiles > 0) {
                label = `${noopFiles} 个文件无变化`;
            } else {
                label = '检查中…';
            }
            progressFooter = `
                <div class="overall-progress">
                    <div class="overall-progress-bar">
                        <div class="overall-progress-fill" style="width:${overallPct}%"></div>
                    </div>
                    <span class="overall-progress-text">${label}</span>
                </div>`;
        } else if (task.status !== 'error' && task.last_sync_at) {
            progressFooter = `
                <div class="overall-progress">
                    <div class="overall-progress-bar">
                        <div class="overall-progress-fill" style="width:100%"></div>
                    </div>
                    <span class="overall-progress-text">已完成 ${this.formatTime(task.last_sync_at)}</span>
                </div>`;
        }

        return `
            <div class="sync-card">
                <div class="sync-card-hd">
                    <div class="sync-card-hd-info">
                        <span class="sync-status-dot ${dotClass}"></span>
                        <span class="sync-card-name">${this.escape(task.name)}</span>
                        <span class="badge ${dir.badge}">${dir.text}</span>
                        <span class="badge ${sched.badge}">${sched.text}</span>
                        ${stateBadge}
                        ${task.last_error && !isActive ? `<span class="badge badge-error">错误</span>` : ''}
                    </div>
                    <div class="sync-card-hd-actions">
                        ${isActive
                            ? `<button class="card-icon-btn card-icon-btn-pause" title="暂停" onclick="app.pauseTask(${task.id})">
                                <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M6 4h4v16H6V4zm8 0h4v16h-4V4z"/></svg>
                               </button>`
                            : (task.status === 'paused'
                                ? `<button class="card-icon-btn card-icon-btn-resume" title="恢复" onclick="app.resumeTask(${task.id})">
                                    <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>
                                   </button>`
                                : `<button class="card-icon-btn" title="暂停" onclick="app.pauseTask(${task.id})">
                                    <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M6 4h4v16H6V4zm8 0h4v16h-4V4z"/></svg>
                                   </button>`)
                        }
                        <button class="card-icon-btn card-icon-btn-sync" title="立即同步" onclick="app.syncNow(${task.id})" ${isActive ? 'disabled' : ''}>
                            <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>
                        </button>
                    </div>
                </div>
                <div class="sync-card-body">
                    <div class="paths-box">
                        <div class="path-row">
                            <span class="path-icon">💻</span>
                            <span class="path-text">${this.escape(task.local_path || '/')}</span>
                        </div>
                        <div class="path-arrow path-arrow-${dir.arrow}">${arrowSvg[dir.arrow]}</div>
                        <div class="path-row">
                            <span class="path-icon">☁️</span>
                            <span class="path-text">${this.escape(task.connection_name || '')}: ${this.escape(task.remote_path || '/')}</span>
                        </div>
                    </div>
                    ${transfersHtml}
                    ${progressFooter}
                </div>
            </div>`;
    },

    // ---- Connections ----
    async loadConnections() {
        try {
            const data = await this.api('GET', '/api/connections');
            this.connections = data.connections || [];
        } catch (e) {
            this.toast('加载连接失败: ' + e.message, 'error');
            return;
        }
        const container = document.getElementById('settings-connections-list')
            || document.getElementById('connections-list');
        if (!container) return;
        this.renderConnections(container);
    },

    renderConnections(container) {
        if (this.connections.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无连接，点击"添加连接"开始</div>';
            return;
        }

        const typeIcons = { baidu: '📦', '115': '💾' };
        const typeNames = { baidu: '百度网盘', '115': '115 网盘' };

        container.innerHTML = this.connections.map(c => {
            const isBaidu = c.type === 'baidu';
            const expired = !!c.token_expired;
            const expiresText = c.token_expires_at
                ? `令牌有效期至 ${this.formatTime(c.token_expires_at)}`
                : '';
            const tokenLine = isBaidu
                ? (expired
                    ? `<div style="font-size:12px;color:var(--color-error);margin-top:6px">⚠️ 令牌已过期，同步会失败，请刷新或重新授权</div>`
                    : (expiresText
                        ? `<div style="font-size:12px;color:var(--text-muted);margin-top:6px">${expiresText}</div>`
                        : ''))
                : '';
            const refreshBtn = isBaidu
                ? `<button class="btn btn-secondary btn-sm" onclick="app.refreshConnectionToken(${c.id})">刷新令牌</button>`
                : '';
            const reauthBtn = isBaidu
                ? `<button class="btn btn-secondary btn-sm" onclick="app.openReauthDialog(${c.id})">重新授权</button>`
                : `<button class="btn btn-secondary btn-sm" onclick="app.openReauthDialog(${c.id})">更新 Cookie</button>`;
            return `
            <div class="conn-card">
                <div class="conn-card-header">
                    <div class="conn-card-icon">${typeIcons[c.type] || '☁️'}</div>
                    <div>
                        <div class="conn-card-name">${this.escape(c.name)}</div>
                        <div class="conn-card-type">${typeNames[c.type] || c.type}</div>
                    </div>
                </div>
                <div class="conn-card-status">
                    <span class="badge ${c.status === 'connected' ? 'badge-realtime' : 'badge-error'}">
                        ${c.status === 'connected' ? '● 已连接' : '● 未连接'}
                    </span>
                    ${expired ? '<span class="badge badge-error" style="margin-left:6px">令牌过期</span>' : ''}
                </div>
                ${tokenLine}
                <div style="font-size:12px;color:var(--text-muted);margin-top:8px">
                    创建于 ${this.formatTime(c.created_at)}
                </div>
                <div class="conn-card-actions">
                    <button class="btn btn-secondary btn-sm" onclick="app.testConnection(${c.id})">测试</button>
                    ${refreshBtn}
                    ${reauthBtn}
                    <button class="btn btn-secondary btn-sm" onclick="app.browseRemote(${c.id})">浏览</button>
                    <button class="btn btn-danger btn-sm" onclick="app.deleteConnection(${c.id})">删除</button>
                </div>
            </div>`;
        }).join('');
    },

    openAddConnectionDialog() {
        this.openModal('modal-connection');
        document.getElementById('conn-form-baidu').style.display = 'none';
        document.getElementById('conn-form-115').style.display = 'none';
    },

    selectConnType(type) {
        document.getElementById('conn-form-baidu').style.display = type === 'baidu' ? 'block' : 'none';
        document.getElementById('conn-form-115').style.display = type === '115' ? 'block' : 'none';
        if (type === 'baidu') this.loadBaiduAppConfig();
    },

    async loadBaiduAppConfig() {
        try {
            const config = await this.api('GET', '/api/connections/baidu/app-config');
            const statusEl = document.getElementById('baidu-app-config-status');
            const loginBtn = document.getElementById('baidu-login-btn');
            loginBtn.disabled = false;
            loginBtn.style.opacity = '1';
            if (config.using_builtin) {
                statusEl.innerHTML = '<span style="color:var(--color-success)">✓ 使用内置应用凭据，无需配置</span>';
            } else if (config.app_key_configured) {
                statusEl.innerHTML = '<span style="color:var(--color-success)">✓ 已配置自定义应用</span>';
                if (config.app_key) document.getElementById('baidu-app-key').value = config.app_key;
            }
        } catch (e) {}
    },

    async saveBaiduAppConfig() {
        const appKey = document.getElementById('baidu-app-key').value.trim();
        const appSecret = document.getElementById('baidu-app-secret').value.trim();
        if (!appKey) { this.toast('请填写 App Key', 'warning'); return; }
        try {
            await this.api('POST', '/api/connections/baidu/app-config', { app_key: appKey, app_secret: appSecret });
            this.toast('应用配置已保存', 'success');
            this.loadBaiduAppConfig();
        } catch (e) { this.toast('保存失败: ' + e.message, 'error'); }
    },

    async baiduGetAuthCode() {
        try {
            const resp = await this.api('GET', '/api/connections/baidu/oauth-tool');
            window.open(resp.url, '_blank');
            this.toast('已打开百度授权页面，请在授权后复制授权码', 'info');
        } catch (e) { this.toast('获取授权链接失败: ' + e.message, 'error'); }
    },

    async baiduConnectWithCode() {
        const name = document.getElementById('baidu-conn-name').value.trim() || '我的百度网盘';
        const authCode = document.getElementById('baidu-auth-code-input').value.trim();
        if (!authCode) { this.toast('请粘贴授权码', 'warning'); return; }
        const statusEl = document.getElementById('baidu-oauth-status');
        statusEl.style.display = 'block';
        statusEl.className = 'baidu-oauth-status baidu-oauth-pending';
        statusEl.innerHTML = '<span class="oauth-spinner"></span> 正在验证授权码并连接...';
        try {
            const result = await this.api('POST', '/api/connections/baidu/code', { name, code: authCode });
            statusEl.className = 'baidu-oauth-status baidu-oauth-success';
            statusEl.innerHTML = `✅ 百度网盘「${result.name || name}」连接成功！`;
            this.toast('百度网盘连接成功', 'success');
            setTimeout(() => { this.closeModal('modal-connection'); this.loadConnections(); }, 1500);
        } catch (e) {
            statusEl.className = 'baidu-oauth-status baidu-oauth-error';
            statusEl.innerHTML = '❌ ' + e.message;
            this.toast('连接失败: ' + e.message, 'error');
        }
    },

    async createBaiduTokenConn() {
        const name = document.getElementById('baidu-conn-name').value.trim();
        const accessToken = document.getElementById('baidu-access-token').value.trim();
        const refreshToken = document.getElementById('baidu-refresh-token').value.trim();
        if (!name || !accessToken) { this.toast('请填写连接名称和 Access Token', 'warning'); return; }
        try {
            await this.api('POST', '/api/connections/baidu/token', { name, access_token: accessToken, refresh_token: refreshToken });
            this.toast('百度网盘连接成功', 'success');
            this.closeModal('modal-connection');
            this.loadConnections();
        } catch (e) { this.toast('连接失败: ' + e.message, 'error'); }
    },

    // ---- 115 QR Login ----
    async start115QRLogin() {
        const statusEl = document.getElementById('115-qr-status');
        const imageEl = document.getElementById('115-qr-image');
        imageEl.innerHTML = '<div style="color:var(--text-secondary)">加载中...</div>';
        statusEl.textContent = '';
        try {
            const tokenData = await this.api('GET', '/api/connections/115/qr-token');
            const imgResp = await fetch(`/api/connections/115/qr-image?token=${encodeURIComponent(tokenData.token)}`);
            const imgData = await imgResp.json();
            imageEl.innerHTML = `<img src="${imgData.image}" alt="QR Code">`;
            statusEl.textContent = '请使用 115 手机 App 扫描二维码';
            this.poll115(tokenData);
        } catch (e) {
            imageEl.innerHTML = '<div style="color:var(--color-error)">获取二维码失败</div>';
            this.toast('获取二维码失败: ' + e.message, 'error');
        }
    },

    poll115(tokenData) {
        if (this.poll115Timer) clearInterval(this.poll115Timer);
        const statusEl = document.getElementById('115-qr-status');
        const nameInput = document.getElementById('115-conn-name');
        this.poll115Timer = setInterval(async () => {
            try {
                const result = await this.api('POST', '/api/connections/115/qr-poll', {
                    uid: tokenData.uid, token: tokenData.token, sign: tokenData.sign, time: tokenData.time,
                });
                if (result.status === 2) {
                    clearInterval(this.poll115Timer);
                    this.poll115Timer = null;
                    statusEl.innerHTML = '<span style="color:var(--color-success)">✓ 登录成功！</span>';
                    if (!nameInput.value) nameInput.value = '我的115网盘';
                    const name = nameInput.value.trim();
                    if (name) {
                        try {
                            await this.api('POST', '/api/connections/115/cookies', { name, cookies: result.cookies });
                            this.toast('115 网盘连接成功', 'success');
                            this.closeModal('modal-connection');
                            this.loadConnections();
                        } catch (e) { this.toast('创建连接失败: ' + e.message, 'error'); }
                    }
                } else if (result.status === 1) {
                    statusEl.textContent = '已扫描，请在手机上确认';
                } else if (result.status === -2) {
                    clearInterval(this.poll115Timer);
                    this.poll115Timer = null;
                    statusEl.innerHTML = '<span style="color:var(--color-error)">二维码已过期，请重新获取</span>';
                }
            } catch (e) {}
        }, 2000);
    },

    async create115CookieConn() {
        const name = document.getElementById('115-conn-name').value.trim();
        const uid = document.getElementById('115-cookie-uid').value.trim();
        const cid = document.getElementById('115-cookie-cid').value.trim();
        const seid = document.getElementById('115-cookie-seid').value.trim();
        if (!name || !uid) { this.toast('请填写连接名称和 UID', 'warning'); return; }
        try {
            await this.api('POST', '/api/connections/115/cookies', { name, cookies: { UID: uid, CID: cid, SEID: seid } });
            this.toast('115 网盘连接成功', 'success');
            this.closeModal('modal-connection');
            this.loadConnections();
        } catch (e) { this.toast('连接失败: ' + e.message, 'error'); }
    },

    async testConnection(id) {
        try {
            const result = await this.api('POST', `/api/connections/${id}/test`);
            this.toast(result.connected ? '连接成功' : '连接失败', result.connected ? 'success' : 'error');
            this.loadConnections();
        } catch (e) { this.toast('测试失败: ' + e.message, 'error'); }
    },

    // ---- Token maintenance ----
    async refreshConnectionToken(id) {
        const conn = this.connections.find(c => c.id === id);
        const name = conn ? conn.name : '';
        this.toast('正在刷新令牌…', 'info');
        try {
            const r = await this.api('POST', `/api/connections/${id}/refresh`);
            const when = r.expires_at ? this.formatTime(r.expires_at) : '';
            this.toast(`「${name}」令牌已刷新${when ? '，有效期至 ' + when : ''}`, 'success');
        } catch (e) {
            this.toast('刷新失败: ' + e.message + '（可点"重新授权"）', 'error');
        }
        this.loadConnections();
    },

    async openReauthDialog(id) {
        const conn = this.connections.find(c => c.id === id);
        if (!conn) return;
        this._reauthConn = conn;
        document.getElementById('reauth-title').textContent =
            conn.type === 'baidu' ? `重新授权：${conn.name}` : `更新 Cookie：${conn.name}`;
        const body = document.getElementById('reauth-body');

        if (conn.type === 'baidu') {
            let urls = { authorize_url: '', oauth_tool_url: '' };
            try { urls = await this.api('GET', `/api/connections/${id}/auth-url`); } catch (e) {}
            const stateText = conn.token_expired
                ? '<span style="color:var(--color-error)">已过期</span>'
                : (conn.token_expires_at ? '有效期至 ' + this.formatTime(conn.token_expires_at) : '有效期未知');
            body.innerHTML = `
                <div style="font-size:13px;color:var(--text-secondary);margin-bottom:12px;">
                    当前令牌：${stateText}${conn.has_refresh_token ? '' : '（无 refresh_token，只能重新授权）'}
                </div>
                <div style="background:var(--accent-light);border:1px solid rgba(79,110,247,0.2);border-radius:var(--radius-md);padding:16px;margin-bottom:12px;">
                    <div style="font-weight:600;margin-bottom:8px;">方式一：授权码（推荐）</div>
                    <p style="font-size:13px;color:var(--text-secondary);line-height:1.7;margin-bottom:10px;">
                        1. 点下面按钮，在百度登录并授权<br>
                        2. 页面会显示一串授权码（code），复制它<br>
                        3. 粘贴到输入框，点"确认更新"
                    </p>
                    <button class="btn btn-primary" onclick="window.open('${urls.authorize_url || urls.oauth_tool_url}', '_blank')">
                        打开百度授权页面
                    </button>
                    <div class="form-group" style="margin-top:12px;margin-bottom:0;">
                        <label>授权码 (code)</label>
                        <input type="text" id="reauth-code" class="input" placeholder="在此粘贴授权码"
                               style="font-family:monospace;font-size:13px;">
                    </div>
                </div>
                <details>
                    <summary style="cursor:pointer;font-size:13px;color:var(--text-secondary);">方式二：直接粘贴 refresh_token</summary>
                    <div class="form-group" style="margin-top:10px;">
                        <input type="text" id="reauth-refresh-token" class="input" placeholder="refresh_token"
                               style="font-family:monospace;font-size:13px;">
                    </div>
                    <p style="font-size:12px;color:var(--text-muted);margin:0;">
                        refresh_token 可从
                        <a href="${urls.oauth_tool_url}" target="_blank" style="color:var(--accent)">AList OAuth 工具页</a>
                        获取。
                    </p>
                </details>
                <p style="font-size:12px;color:var(--text-muted);margin-top:12px;">
                    重新授权只替换凭据，连接 ID、同步任务和增量状态都不受影响。
                </p>`;
        } else {
            body.innerHTML = `
                <p style="font-size:13px;color:var(--text-secondary);line-height:1.7;margin-bottom:12px;">
                    在浏览器登录 <b>115 网盘</b> → 按 F12 → Application → Cookies，
                    复制 <code>UID</code> / <code>CID</code> / <code>SEID</code> 三个值填入下方。
                </p>
                <div class="form-group"><label>UID</label>
                    <input type="text" id="reauth-uid" class="input" style="font-family:monospace"></div>
                <div class="form-group"><label>CID</label>
                    <input type="text" id="reauth-cid" class="input" style="font-family:monospace"></div>
                <div class="form-group"><label>SEID</label>
                    <input type="text" id="reauth-seid" class="input" style="font-family:monospace"></div>
                <p style="font-size:12px;color:var(--text-muted);">更新后连接 ID 与同步任务保持不变。</p>`;
        }
        this.openModal('modal-reauth');
    },

    async submitReauth() {
        const conn = this._reauthConn;
        if (!conn) return;
        const payload = {};

        if (conn.type === 'baidu') {
            const code = (document.getElementById('reauth-code')?.value || '').trim();
            const rt = (document.getElementById('reauth-refresh-token')?.value || '').trim();
            if (code) payload.code = code;
            else if (rt) payload.refresh_token = rt;
            else { this.toast('请粘贴授权码或 refresh_token', 'warning'); return; }
        } else {
            const uid = (document.getElementById('reauth-uid')?.value || '').trim();
            const cid = (document.getElementById('reauth-cid')?.value || '').trim();
            const seid = (document.getElementById('reauth-seid')?.value || '').trim();
            if (!uid || !cid || !seid) { this.toast('请填完整的 UID / CID / SEID', 'warning'); return; }
            payload.cookies = { UID: uid, CID: cid, SEID: seid };
        }

        try {
            await this.api('POST', `/api/connections/${conn.id}/reauth`, payload);
            this.toast('凭据已更新，同步任务不受影响', 'success');
            this.closeModal('modal-reauth');
            this.loadConnections();
        } catch (e) {
            this.toast('更新失败: ' + e.message, 'error');
        }
    },

    async deleteConnection(id) {
        if (!confirm('确定删除此连接？关联的同步任务也会被删除。')) return;
        try {
            await this.api('DELETE', `/api/connections/${id}`);
            this.toast('已删除', 'success');
            this.loadConnections();
        } catch (e) { this.toast('删除失败: ' + e.message, 'error'); }
    },

    async browseRemote(connId) {
        // Navigate to cloud-files view with this connection pre-selected
        this._cloudFilesConnId = connId;
        this._cloudFilesPath = '/';
        this.switchView('cloud-files');
    },

    // ---- Tasks ----
    async loadTasks() {
        try {
            const data = await this.api('GET', '/api/tasks');
            this.tasks = data.tasks || [];
        } catch (e) {
            this.toast('加载任务失败: ' + e.message, 'error');
            return;
        }

        const container = document.getElementById('tasks-list');
        if (this.tasks.length === 0) {
            container.innerHTML = '<div class="empty-state">暂无任务，点击"创建任务"开始</div>';
            return;
        }

        const modeNames = { bidirectional: '双向同步', upload_only: '仅上传', download_only: '仅下载' };
        const statusMap = {
            idle: { text: '空闲', badge: 'badge-manual' },
            running: { text: '同步中', badge: 'badge-realtime' },
            success: { text: '已同步', badge: 'badge-realtime' },
            error: { text: '错误', badge: 'badge-error' },
            paused: { text: '已暂停', badge: 'badge-both' },
        };

        container.innerHTML = this.tasks.map(t => {
            const st = statusMap[t.status] || statusMap.idle;
            let progressInfo = '';
            if (t.is_running && t.progress) {
                const done = t.progress.completed_files !== undefined
                    ? t.progress.completed_files
                    : ((t.progress.processed_files || 0) + (t.progress.skipped_files || 0) + (t.progress.failed_files || 0));
                progressInfo = `<span>📊 ${done}/${t.progress.total_files || 0} 文件</span>`;
            }
            return `
                <div class="task-item">
                    <div class="task-item-info">
                        <div class="task-item-name">${this.escape(t.name)}</div>
                        <div class="task-item-detail">
                            <span>📁 ${this.escape(t.local_path || '/')}</span>
                            <span>☁️ ${this.escape(t.connection_name || '')} → ${this.escape(t.remote_path || '/')}</span>
                            <span>🔄 ${modeNames[t.sync_mode] || t.sync_mode}</span>
                            <span class="badge ${st.badge}">${st.text}</span>
                            ${progressInfo}
                            ${t.last_error ? `<span style="color:var(--color-error)">⚠️ ${this.escape(t.last_error)}</span>` : ''}
                        </div>
                    </div>
                    <div class="task-item-actions">
                        ${t.is_running
                            ? `<button class="btn btn-secondary btn-sm" onclick="app.pauseTask(${t.id})">暂停</button>`
                            : (t.status === 'paused'
                                ? `<button class="btn btn-primary btn-sm" onclick="app.resumeTask(${t.id})">恢复</button>`
                                : `<button class="btn btn-secondary btn-sm" onclick="app.pauseTask(${t.id})">暂停</button>`)
                        }
                        <button class="btn btn-primary btn-sm" onclick="app.syncNow(${t.id})" ${t.is_running ? 'disabled' : ''}>${t.is_running ? '同步中' : '立即同步'}</button>
                        <button class="btn btn-secondary btn-sm" onclick="app.editTask(${t.id})">编辑</button>
                        <button class="btn btn-danger btn-sm" onclick="app.deleteTask(${t.id})">删除</button>
                    </div>
                </div>`;
        }).join('');
    },

    async openCreateTaskDialog() {
        this.editingTaskId = null;
        document.getElementById('task-modal-title').textContent = '创建同步任务';
        document.getElementById('task-name').value = '';
        document.getElementById('task-local-path').value = '';
        document.getElementById('task-remote-path').value = '/';
        document.getElementById('task-include').value = '';
        document.getElementById('task-exclude').value = '';
        document.getElementById('task-max-size').value = '0';
        document.getElementById('task-interval').value = '30';
        document.querySelector('input[name="sync-mode"][value="bidirectional"]').checked = true;
        document.querySelector('input[name="schedule-type"][value="scheduled"]').checked = true;
        document.getElementById('schedule-interval-group').style.display = 'block';
        try {
            const data = await this.api('GET', '/api/connections');
            const select = document.getElementById('task-connection');
            select.innerHTML = '<option value="">选择连接...</option>' +
                data.connections.map(c => `<option value="${c.id}">${this.escape(c.name)} (${c.type})</option>`).join('');
        } catch (e) { this.toast('加载连接失败', 'error'); }
        document.getElementById('task-ignored-group').style.display = 'none';
        this.openModal('modal-task');
    },

    async editTask(id) {
        const task = this.tasks.find(t => t.id === id);
        if (!task) return;
        this.editingTaskId = id;
        document.getElementById('task-modal-title').textContent = '编辑任务';
        document.getElementById('task-name').value = task.name;
        document.getElementById('task-local-path').value = task.local_path;
        document.getElementById('task-remote-path').value = task.remote_path;
        document.getElementById('task-include').value = task.filter_include || '';
        document.getElementById('task-exclude').value = task.filter_exclude || '';
        document.getElementById('task-max-size').value = task.max_file_size || 0;
        document.getElementById('task-interval').value = (task.schedule_interval || 1800) / 60;
        const syncMode = document.querySelector(`input[name="sync-mode"][value="${task.sync_mode}"]`);
        if (syncMode) syncMode.checked = true;
        const schedType = document.querySelector(`input[name="schedule-type"][value="${task.schedule_type}"]`);
        if (schedType) schedType.checked = true;
        document.getElementById('schedule-interval-group').style.display = task.schedule_type === 'scheduled' ? 'block' : 'none';
        try {
            const data = await this.api('GET', '/api/connections');
            const select = document.getElementById('task-connection');
            select.innerHTML = '<option value="">选择连接...</option>' +
                data.connections.map(c => `<option value="${c.id}" ${c.id === task.connection_id ? 'selected' : ''}>${this.escape(c.name)} (${c.type})</option>`).join('');
        } catch (e) {}
        await this._renderIgnoredList(id);
        this.openModal('modal-task');
    },

    async saveTask() {
        const name = document.getElementById('task-name').value.trim();
        const connectionId = parseInt(document.getElementById('task-connection').value);
        const localPath = document.getElementById('task-local-path').value.trim();
        const remotePath = document.getElementById('task-remote-path').value.trim() || '/';
        const syncMode = document.querySelector('input[name="sync-mode"]:checked').value;
        const scheduleType = document.querySelector('input[name="schedule-type"]:checked').value;
        const intervalMin = parseInt(document.getElementById('task-interval').value) || 30;
        const include = document.getElementById('task-include').value.trim();
        const exclude = document.getElementById('task-exclude').value.trim();
        const maxSizeMB = parseInt(document.getElementById('task-max-size').value) || 0;
        if (!name || !connectionId || !localPath) { this.toast('请填写名称、连接和本地目录', 'warning'); return; }
        const payload = {
            name, connection_id: connectionId, local_path: localPath, remote_path: remotePath,
            sync_mode: syncMode, schedule_type: scheduleType,
            schedule_interval: scheduleType === 'scheduled' ? intervalMin * 60 : 0,
            filter_include: include, filter_exclude: exclude,
            max_file_size: maxSizeMB * 1024 * 1024,
        };
        try {
            if (this.editingTaskId) {
                await this.api('PUT', `/api/tasks/${this.editingTaskId}`, payload);
                this.toast('任务已更新', 'success');
            } else {
                await this.api('POST', '/api/tasks', payload);
                this.toast('任务已创建', 'success');
            }
            this.closeModal('modal-task');
            this.loadTasks();
        } catch (e) { this.toast('保存失败: ' + e.message, 'error'); }
    },

    async syncNow(id) {
        try {
            await this.api('POST', `/api/tasks/${id}/sync`);
            this.toast('同步已启动', 'success');
            this.loadTasks();
        } catch (e) { this.toast('启动同步失败: ' + e.message, 'error'); }
    },

    async pauseTask(id) {
        try {
            await this.api('POST', `/api/tasks/${id}/pause`);
            this.toast('任务已暂停', 'info');
            await this._refreshCurrentView();
        } catch (e) { this.toast('暂停失败: ' + e.message, 'error'); }
    },

    async resumeTask(id) {
        try {
            await this.api('POST', `/api/tasks/${id}/resume`);
            this.toast('任务已恢复', 'success');
            await this._refreshCurrentView();
        } catch (e) { this.toast('恢复失败: ' + e.message, 'error'); }
    },

    async cancelTransfer(taskId, transferId) {
        try {
            await this.api('POST', `/api/tasks/${taskId}/cancel-transfer`, { transfer_id: transferId });
            this.toast('已跳过该文件', 'info');
            // No need to refresh the entire view; progress polling will pick it up
        } catch (e) { this.toast('跳过失败: ' + e.message, 'error'); }
    },

    // "Skip" has to mean "stop offering me this file", not "abort this one
    // transfer": the latter left the file queued for the very next cycle, which
    // is why a skipped file came straight back. Cancel the live transfer AND
    // record the decision.
    async skipFile(taskId, transferId, filePath, action, size) {
        try {
            if (filePath) {
                await this.api('POST', `/api/tasks/${taskId}/ignore-file`, {
                    path: filePath,
                    action: action || '',
                    size: size || 0,
                });
            }
            if (transferId != null) {
                try {
                    await this.api('POST', `/api/tasks/${taskId}/cancel-transfer`,
                                   { transfer_id: transferId });
                } catch (e) {
                    // The transfer may have finished between render and click.
                }
            }
            this.toast('已跳过，可在传输记录里点「重试」恢复', 'info');
        } catch (e) {
            this.toast('跳过失败: ' + e.message, 'error');
        }
    },

    async restoreIgnored(taskId, path) {
        try {
            await this.api('DELETE',
                `/api/tasks/${taskId}/ignore-file?path=${encodeURIComponent(path)}`);
            this.toast('已恢复同步', 'success');
            await this._renderIgnoredList(taskId);
        } catch (e) {
            this.toast('恢复失败: ' + e.message, 'error');
        }
    },

    // Re-queue a single failed or skipped file straight from the transfer
    // panel. The server clears that file's ignore flag and retry backoff, then
    // spawns a sync, so it goes through the normal scan rather than a special
    // one-off path. If a sync is already running the file is simply picked up
    // by the current cycle.
    async retryRecord(taskId, recordId, btn) {
        if (btn) { btn.disabled = true; btn.textContent = '重试中'; }
        try {
            const res = await this.api('POST',
                `/api/tasks/${taskId}/records/${recordId}/retry`);
            if (res && res.status === 'already_synced') {
                this.toast('文件已在云端且未变动，已标记为成功', 'success');
            } else if (res && res.already_running) {
                this.toast('已清除状态，当前轮同步结束后自动补跑一轮', 'success');
            } else {
                this.toast('已重新排队，正在同步', 'success');
            }
            // The old row stays in place until it succeeds; a fresh success row
            // then appears above it, so the reload keeps both visible.
            await this.loadRecords(true);
        } catch (e) {
            this.toast('重试失败: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '重试'; }
        }
    },

    // Remove one row from the history. Purely cosmetic: the file on either
    // side is untouched, and a file that keeps failing will simply write a
    // fresh failure row on its next cycle.
    async deleteRecord(recordId, btn) {
        if (btn) btn.disabled = true;
        try {
            await this.api('DELETE', `/api/records/${recordId}`);
            const row = btn && btn.closest('.rec-item');
            if (row) {
                row.style.transition = 'opacity .18s, transform .18s';
                row.style.opacity = '0';
                row.style.transform = 'translateX(12px)';
                setTimeout(() => row.remove(), 180);
            }
            this.toast('记录已删除', 'success');
            // Keep the header counts honest without refetching everything.
            setTimeout(() => this.loadRecords(true), 220);
        } catch (e) {
            if (btn) btn.disabled = false;
            this.toast('删除失败: ' + e.message, 'error');
        }
    },

    // One click to re-queue everything that went wrong, across all tasks.
    // The server clears ignore flags + backoff per file and spawns one sync
    // per affected task; rows stay until the files actually succeed.
    async retryAllRecords(btn) {
        if (btn) { btn.disabled = true; btn.textContent = '重试中'; }
        try {
            const res = await this.api('POST', '/api/records/retry-all');
            if (!res || (!res.queued && !res.resolved)) {
                this.toast('没有需要重试的记录', 'info');
            } else {
                const bits = [];
                if (res.resolved) bits.push(`${res.resolved} 条已在云端，直接标记成功`);
                if (res.queued) {
                    bits.push(`${res.queued} 条重新排队`);
                    bits.push(res.already_running && res.already_running.length
                        ? `当前轮结束后自动补跑（${res.already_running.join('、')}）`
                        : `已启动任务（${(res.started || []).join('、')}）`);
                }
                this.toast(bits.join('，'), 'success');
            }
            await this.loadRecords(true);
        } catch (e) {
            this.toast('重试全部失败: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = '重试全部'; }
        }
    },

    async _renderIgnoredList(taskId) {
        const group = document.getElementById('task-ignored-group');
        const list = document.getElementById('task-ignored-list');
        if (!group || !list) return;
        if (!taskId) { group.style.display = 'none'; return; }
        try {
            const data = await this.api('GET', `/api/tasks/${taskId}/ignored`);
            const items = data.ignored || [];
            if (!items.length) { group.style.display = 'none'; return; }
            group.style.display = 'block';
            list.innerHTML = items.map(it => `
                <div class="ignored-item">
                    <span class="path" title="${this.escape(it.file_path)}">${this.escape(it.file_path)}</span>
                    <button class="restore" onclick="app.restoreIgnored(${taskId}, '${this.escapeAttr(it.file_path)}')">恢复</button>
                </div>`).join('');
        } catch (e) {
            group.style.display = 'none';
        }
    },

    // Refresh whatever view is currently active so state changes (pause/resume) are reflected immediately
    async _refreshCurrentView() {
        if (this.currentView === 'dashboard') {
            await this.loadDashboard();
        } else if (this.currentView === 'tasks') {
            await this.loadTasks();
        } else if (this.currentView === 'downloads') {
            await this.loadDownloads();
        } else if (this.currentView === 'logs') {
            await this.loadLogs();
        } else if (this.currentView === 'settings') {
            await this.refreshSettingsActiveCounts();
        }
    },

    async deleteTask(id) {
        if (!confirm('确定删除此任务？')) return;
        try {
            await this.api('DELETE', `/api/tasks/${id}`);
            this.toast('已删除', 'success');
            this.loadTasks();
        } catch (e) { this.toast('删除失败: ' + e.message, 'error'); }
    },

    // ---- Directory Browser ----
    async browseLocalDir() {
        this.browserMode = 'local';
        this.browserPath = '/';
        this.browserRoot = 'sync';
        this.browserSelection = '/';
        document.getElementById('browser-title').textContent = '浏览本地目录';
        document.getElementById('browser-root-switcher').style.display = 'flex';
        this.updateBrowserRootButtons();
        await this.loadBrowserItems();
        this.openModal('modal-browser');
    },

    async browseRemoteDir() {
        const connId = parseInt(document.getElementById('task-connection').value);
        if (!connId) { this.toast('请先选择云连接', 'warning'); return; }
        this.browserMode = 'remote';
        this.browserPath = '/';
        this.browserSelection = '/';
        this._browserConnId = connId;
        document.getElementById('browser-title').textContent = '浏览远程目录';
        document.getElementById('browser-root-switcher').style.display = 'none';
        await this.loadBrowserItems();
        this.openModal('modal-browser');
    },

    async loadBrowserItems() {
        const listEl = document.getElementById('browser-list');
        const pathEl = document.getElementById('browser-current-path');
        const backBtn = document.getElementById('browser-back-btn');
        const rootLabel = this.browserMode === 'local' && this.browserRoot === 'downloads' ? '下载目录' : '';
        pathEl.textContent = (rootLabel ? rootLabel + ' · ' : '当前路径: ') + this.browserPath;
        // Show back button unless at root
        if (backBtn) {
            backBtn.style.display = this.browserPath === '/' ? 'none' : '';
        }
        listEl.innerHTML = '<div style="padding:20px;color:var(--text-secondary)">加载中...</div>';
        try {
            let entries = [];
            if (this.browserMode === 'local') {
                const data = await this.api('GET', `/api/system/local-dirs?path=${encodeURIComponent(this.browserPath)}&root=${this.browserRoot}`);
                entries = data.entries;
            } else {
                const data = await this.api('GET', `/api/connections/${this._browserConnId}/browse?path=${encodeURIComponent(this.browserPath)}`);
                entries = data.entries;
            }
            if (entries.length === 0) {
                listEl.innerHTML = '<div style="padding:20px;color:var(--text-secondary);text-align:center;line-height:2;">' +
                    '<div style="font-size:32px;margin-bottom:8px;">📂</div>' +
                    '<div>当前目录为空</div>' +
                    '<div style="font-size:13px;color:var(--text-muted);margin-top:4px;">可直接点击下方「选择此目录」使用当前路径</div></div>';
            } else {
                // Sort: directories first, then files
                entries.sort((a, b) => {
                    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
                    return a.name.localeCompare(b.name, 'zh');
                });
                listEl.innerHTML = entries.map(e => {
                    if (e.is_dir) {
                        return `<div class="browser-item browser-item-dir" onclick="app.browserEnter('${this.escape(e.path)}')">
                            <span class="browser-item-icon">📁</span>
                            <span class="browser-item-name">${this.escape(e.name)}</span>
                        </div>`;
                    } else {
                        const sizeStr = e.size != null ? this.formatSize(e.size) : '';
                        const ext = e.name.split('.').pop().toLowerCase();
                        let icon = '📄';
                        if (['jpg','jpeg','png','gif','bmp','webp','svg','heic','raf','cr2','arw','dng','xmp'].includes(ext)) icon = '🖼️';
                        else if (['mp4','mkv','avi','mov','wmv','flv','rmvb'].includes(ext)) icon = '🎬';
                        else if (['mp3','flac','ape','wav','aac','ogg'].includes(ext)) icon = '🎵';
                        else if (['zip','rar','7z','tar','gz','bz2'].includes(ext)) icon = '📦';
                        else if (['pdf'].includes(ext)) icon = '📕';
                        else if (['doc','docx'].includes(ext)) icon = '📘';
                        else if (['xls','xlsx'].includes(ext)) icon = '📗';
                        else if (['ppt','pptx'].includes(ext)) icon = '📙';
                        else if (['txt','md','json','xml','csv','log'].includes(ext)) icon = '📃';
                        return `<div class="browser-item browser-item-file">
                            <span class="browser-item-icon">${icon}</span>
                            <span class="browser-item-name">${this.escape(e.name)}</span>
                            ${sizeStr ? `<span class="browser-item-size">${sizeStr}</span>` : ''}
                        </div>`;
                    }
                }).join('');
            }
        } catch (e) {
            listEl.innerHTML = `<div style="padding:20px;color:var(--color-error)">${e.message}</div>`;
        }
    },

    browserEnter(path) {
        this.browserPath = path;
        this.browserSelection = path;
        this.loadBrowserItems();
    },

    browserGoUp() {
        if (this.browserPath === '/') return;
        const parts = this.browserPath.split('/').filter(Boolean);
        parts.pop();
        this.browserPath = '/' + parts.join('/');
        this.browserSelection = this.browserPath;
        this.loadBrowserItems();
    },

    switchBrowserRoot(root) {
        this.browserRoot = root;
        this.browserPath = '/';
        this.browserSelection = '/';
        this.updateBrowserRootButtons();
        this.loadBrowserItems();
    },

    updateBrowserRootButtons() {
        const syncBtn = document.getElementById('browser-root-sync');
        const dlBtn = document.getElementById('browser-root-downloads');
        if (this.browserRoot === 'sync') {
            syncBtn.className = 'btn btn-sm btn-primary';
            dlBtn.className = 'btn btn-sm btn-secondary';
        } else {
            syncBtn.className = 'btn btn-sm btn-secondary';
            dlBtn.className = 'btn btn-sm btn-primary';
        }
    },

    confirmBrowserSelection() {
        if (this.browserMode === 'local') {
            const prefix = this.browserRoot === 'downloads' ? '/downloads' : '/sync';
            document.getElementById('task-local-path').value = prefix + this.browserSelection;
        } else {
            document.getElementById('task-remote-path').value = this.browserSelection || '/';
        }
        this.closeModal('modal-browser');
    },

    // ---- Settings / About ----
    async loadSettingsView() {
        // Refresh connection list and system info
        const connPromise = this.loadConnections();
        try {
            const info = await this.api('GET', '/api/system/info');
            const el = id => document.getElementById(id);
            if (el('settings-info-version'))  el('settings-info-version').textContent  = info.version || '—';
            if (el('settings-info-data'))     el('settings-info-data').textContent     = info.data_dir || '—';
            if (el('settings-info-download')) el('settings-info-download').textContent = info.download_dir || '—';
            if (el('settings-info-sync'))     el('settings-info-sync').textContent     = info.sync_dir || '—';
            if (el('settings-info-active'))   el('settings-info-active').textContent   = String(info.active_tasks ?? '—');
            if (el('settings-info-downloads'))el('settings-info-downloads').textContent= String(info.active_downloads ?? '—');
        } catch (e) { /* system info optional */ }

        // Runtime settings (currently just the per-task upload concurrency)
        try {
            const s = await this.api('GET', '/api/system/settings');
            const input = document.getElementById('setting-sync-concurrency');
            if (input && s) {
                const lim = (s.limits && s.limits.sync_concurrency) || { min: 1, max: 16 };
                const dflt = (s.defaults && s.defaults.sync_concurrency);
                input.min = lim.min;
                input.max = lim.max;
                input.value = s.sync_concurrency;
                input.dataset.default = dflt == null ? '' : String(dflt);
            }
        } catch (e) { /* settings optional */ }
        await connPromise;
    },

    async saveSyncConcurrency() {
        const input = document.getElementById('setting-sync-concurrency');
        const btn = document.getElementById('setting-sync-concurrency-save');
        if (!input) return;
        // Accept only whole numbers inside the range the API advertises.
        const min = parseInt(input.min, 10) || 1;
        const max = parseInt(input.max, 10) || 16;
        const value = parseInt(input.value, 10);
        if (!Number.isFinite(value) || value < min || value > max) {
            this.toast(`请输入 ${min}–${max} 之间的整数`, 'error');
            input.focus();
            return;
        }
        if (btn) btn.disabled = true;
        try {
            const res = await this.api('PUT', '/api/system/settings', { sync_concurrency: value });
            input.value = res.sync_concurrency;
            this.toast(`并发上传数量已设为 ${res.sync_concurrency}，下一轮同步生效`, 'success');
            if (btn) {
                btn.classList.add('saved');
                setTimeout(() => btn.classList.remove('saved'), 1200);
            }
        } catch (e) {
            this.toast('保存失败: ' + e.message, 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    },

    loadAboutView() {
        // Best-effort: pull version from system info
        this.api('GET', '/api/system/info').then(info => {
            const el = document.getElementById('about-version-text');
            if (el && info && info.version) el.textContent = info.version;
        }).catch(() => {});
    },

    // ---- Cloud Files Browser (full page) ----
    _cloudFilesConnId: null,
    _cloudFilesPath: '/',

    async loadCloudFilesView() {
        const tabsEl = document.getElementById('cloud-files-tabs');
        const area = document.getElementById('cloud-files-area');
        try {
            const data = await this.api('GET', '/api/connections');
            const conns = data.connections || [];
            this.connections = conns;
        } catch (e) {
            tabsEl.innerHTML = '';
            area.innerHTML = '<div class="empty-state">加载连接失败</div>';
            return;
        }

        if (this.connections.length === 0) {
            tabsEl.innerHTML = '';
            area.innerHTML = '<div class="empty-state">暂无云连接，请先在「全局设置」中添加连接</div>';
            return;
        }

        // Render tab bar
        const typeIcons = { baidu: '📦', '115': '💾' };
        tabsEl.innerHTML = this.connections.map(c => `
            <button class="conn-tab" data-conn-id="${c.id}" onclick="app.selectCloudTab(${c.id})">
                <span class="conn-tab-icon">${typeIcons[c.type] || '☁️'}</span>
                <span class="conn-tab-name">${this.escape(c.name)}</span>
            </button>`).join('');

        // Auto-select: use _cloudFilesConnId if set (from "浏览" button), otherwise pick first
        if (!this._cloudFilesConnId || !this.connections.find(c => c.id === this._cloudFilesConnId)) {
            this._cloudFilesConnId = this.connections[0].id;
        }
        // Reset path when switching to a fresh view
        this._cloudFilesPath = '/';
        this._updateCloudTabsActive();
        await this.loadCloudFiles('/');
    },

    _updateCloudTabsActive() {
        document.querySelectorAll('.conn-tab').forEach(tab => {
            tab.classList.toggle('active', parseInt(tab.dataset.connId) === this._cloudFilesConnId);
        });
    },

    selectCloudTab(connId) {
        if (this._cloudFilesConnId === connId) return;
        this._cloudFilesConnId = connId;
        this._cloudFilesPath = '/';
        this._updateCloudTabsActive();
        this.loadCloudFiles('/');
    },

    async loadCloudFiles(path) {
        const connId = this._cloudFilesConnId;
        if (!connId) {
            document.getElementById('cloud-files-area').innerHTML =
                '<div class="empty-state">请选择一个云连接开始浏览</div>';
            return;
        }
        if (path !== undefined) this._cloudFilesPath = path;
        const currentPath = this._cloudFilesPath;

        const area = document.getElementById('cloud-files-area');
        area.innerHTML = '<div style="padding:20px;color:var(--text-secondary)">加载中...</div>';

        try {
            const data = await this.api('GET', `/api/connections/${connId}/browse?path=${encodeURIComponent(currentPath)}`);
            const entries = data.entries || [];

            // Sort: directories first, then files
            entries.sort((a, b) => {
                if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
                return a.name.localeCompare(b.name, 'zh');
            });

            let html = '<div class="cloud-files-toolbar">';
            html += `<button class="cloud-files-back" ${currentPath === '/' ? 'disabled' : ''} onclick="app.cloudFilesGoUp()">`;
            html += '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>';
            html += ' 返回上级</button>';
            html += `<div class="cloud-files-path">${this.escape(currentPath)}</div>`;
            html += '</div>';

            if (entries.length === 0) {
                html += '<div class="empty-state">当前目录为空</div>';
            } else {
                html += '<div class="cloud-files-list">';
                for (const e of entries) {
                    const icon = e.is_dir ? '📁' : this.getFileIcon(e.name);
                    const sizeStr = !e.is_dir && e.size != null ? this.formatSize(e.size) : '';
                    const escapedPath = this.escape(e.path);
                    const escapedName = this.escape(e.name);

                    if (e.is_dir) {
                        html += `<div class="cloud-files-item" onclick="app.loadCloudFiles('${escapedPath}')">
                            <span class="cloud-files-item-icon">${icon}</span>
                            <span class="cloud-files-item-name">${escapedName}</span>
                            <span class="cloud-files-item-size"></span>
                            <button class="cloud-files-item-download" onclick="event.stopPropagation();app.downloadFromCloud('${escapedPath}', true, '${escapedName}')">
                                <svg viewBox="0 0 24 24" width="12" height="12"><path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
                                下载
                            </button>
                        </div>`;
                    } else {
                        html += `<div class="cloud-files-item">
                            <span class="cloud-files-item-icon">${icon}</span>
                            <span class="cloud-files-item-name">${escapedName}</span>
                            <span class="cloud-files-item-size">${sizeStr}</span>
                            <button class="cloud-files-item-download" onclick="app.downloadFromCloud('${escapedPath}', false, '${escapedName}', ${e.size || 0})">
                                <svg viewBox="0 0 24 24" width="12" height="12"><path fill="currentColor" d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
                                下载
                            </button>
                        </div>`;
                    }
                }
                html += '</div>';
            }
            area.innerHTML = html;
        } catch (e) {
            area.innerHTML = `<div class="empty-state" style="color:var(--color-error)">${e.message}</div>`;
        }
    },

    cloudFilesGoUp() {
        if (this._cloudFilesPath === '/') return;
        const parts = this._cloudFilesPath.split('/').filter(Boolean);
        parts.pop();
        this._cloudFilesPath = '/' + parts.join('/');
        this.loadCloudFiles();
    },

    getFileIcon(name) {
        const ext = name.split('.').pop().toLowerCase();
        if (['jpg','jpeg','png','gif','bmp','webp','svg','heic','raf','cr2','arw','dng','xmp'].includes(ext)) return '🖼️';
        if (['mp4','mkv','avi','mov','wmv','flv','rmvb'].includes(ext)) return '🎬';
        if (['mp3','flac','ape','wav','aac','ogg'].includes(ext)) return '🎵';
        if (['zip','rar','7z','tar','gz','bz2'].includes(ext)) return '📦';
        if (['pdf'].includes(ext)) return '📕';
        if (['doc','docx'].includes(ext)) return '📘';
        if (['xls','xlsx'].includes(ext)) return '📗';
        if (['ppt','pptx'].includes(ext)) return '📙';
        if (['txt','md','json','xml','csv','log'].includes(ext)) return '📃';
        return '📄';
    },

    async downloadFromCloud(remotePath, isDir, fileName, fileSize = 0) {
        const connId = this._cloudFilesConnId;
        if (!connId) { this.toast('请先选择连接', 'warning'); return; }
        try {
            await this.api('POST', '/api/downloads', {
                connection_id: connId,
                remote_path: remotePath,
                is_dir: isDir,
                file_name: fileName,
                file_size: fileSize,
            });
            this.toast(`已添加下载任务: ${fileName}`, 'success');
        } catch (e) {
            this.toast('下载失败: ' + e.message, 'error');
        }
    },

    // ---- Downloads View ----
    async loadDownloads() {
        try {
            const data = await this.api('GET', '/api/downloads');
            const downloads = data.downloads || [];
            const container = document.getElementById('downloads-list');
            if (downloads.length === 0) {
                container.innerHTML = '<div class="empty-state">暂无下载任务</div>';
                return;
            }
            const statusMap = {
                pending: { text: '等待中', badge: 'pending' },
                downloading: { text: '下载中', badge: 'downloading' },
                completed: { text: '已完成', badge: 'completed' },
                failed: { text: '失败', badge: 'failed' },
                cancelled: { text: '已取消', badge: 'cancelled' },
            };
            container.innerHTML = downloads.map(d => {
                const st = statusMap[d.status] || statusMap.pending;
                const pct = d.file_size > 0 ? Math.round((d.downloaded_bytes || 0) / d.file_size * 100) : 0;
                const icon = d.is_dir ? '📁' : this.getFileIcon(d.file_name);
                const connName = this.escape(d.connection_name || '');
                let footerLeft = '';
                if (d.status === 'downloading') {
                    footerLeft = `<span>${this.formatSize(d.downloaded_bytes || 0)} / ${this.formatSize(d.file_size || 0)}`;
                    if (d.total_files > 1) footerLeft += ` · ${d.processed_files || 0}/${d.total_files} 文件`;
                    footerLeft += `</span>`;
                    if (d.current_file) footerLeft += `<span style="margin-left:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px;">${this.escape(d.current_file)}</span>`;
                } else if (d.status === 'completed') {
                    footerLeft = `<span>✓ ${this.formatSize(d.file_size || 0)}`;
                    if (d.total_files > 1) footerLeft += ` · ${d.total_files} 文件`;
                    footerLeft += `</span>`;
                } else if (d.status === 'failed') {
                    footerLeft = `<span style="color:var(--color-error)">✗ ${this.escape(d.error_message || '下载失败')}</span>`;
                } else if (d.status === 'cancelled') {
                    footerLeft = `<span>已取消</span>`;
                } else {
                    footerLeft = `<span>${this.formatSize(d.file_size || 0)}</span>`;
                }

                let actions = '';
                if (d.status === 'downloading') {
                    actions = `<button class="btn btn-secondary btn-sm" onclick="app.cancelDownload(${d.id})">取消</button>`;
                }
                actions += `<button class="btn btn-danger btn-sm" onclick="app.deleteDownload(${d.id})">删除</button>`;

                const speedHtml = d.status === 'downloading' && d.speed > 0
                    ? `<span class="download-item-speed">${this.formatSpeed(d.speed)}</span>` : '';

                return `
                <div class="download-item">
                    <div class="download-item-header">
                        <div class="download-item-info">
                            <span class="download-item-icon">${icon}</span>
                            <span class="download-item-name">${this.escape(d.file_name)}</span>
                        </div>
                        <div class="download-item-status">
                            ${speedHtml}
                            <span class="download-status-badge ${st.badge}">${st.text}</span>
                        </div>
                        <div class="download-item-actions">${actions}</div>
                    </div>
                    <div class="download-progress-bar">
                        <div class="download-progress-fill ${st.badge}" style="width:${pct}%"></div>
                    </div>
                    <div class="download-item-footer">
                        <div style="display:flex;align-items:center;gap:8px;min-width:0;">
                            <span style="color:var(--text-muted)">${connName}</span>
                            ${footerLeft}
                        </div>
                        <span style="color:var(--text-muted)">${this.formatTime(d.created_at)}</span>
                    </div>
                </div>`;
            }).join('');
        } catch (e) {
            this.toast('加载下载任务失败: ' + e.message, 'error');
        }
    },

    async cancelDownload(id) {
        try {
            await this.api('POST', `/api/downloads/${id}/cancel`);
            this.toast('已取消下载', 'info');
            this.loadDownloads();
        } catch (e) { this.toast('取消失败: ' + e.message, 'error'); }
    },

    async deleteDownload(id) {
        if (!confirm('确定删除此下载记录？（已下载的文件不会被删除）')) return;
        try {
            await this.api('DELETE', `/api/downloads/${id}`);
            this.toast('已删除', 'success');
            this.loadDownloads();
        } catch (e) { this.toast('删除失败: ' + e.message, 'error'); }
    },

    // ---- Logs (aggregated, all tasks) ----
    _lastLogsHtml: '',

    async loadLogs(isAutoRefresh = false) {
        const container = document.getElementById('logs-container');
        if (!container) return;
        // Only show loading placeholder on manual load, not on auto-refresh
        if (!isAutoRefresh) {
            container.innerHTML = '<div class="empty-state">加载中...</div>';
        }
        try {
            const data = await this.api('GET', '/api/logs?limit=1000');
            const logs = data.logs || [];
            if (logs.length === 0) {
                this._lastLogsHtml = '';
                container.innerHTML = '<div class="empty-state">暂无日志</div>';
                return;
            }
            const html = logs.map(log => {
                const taskName = log.task_name ? this.escape(log.task_name) : `任务#${log.task_id}`;
                const actionClass = log.action || 'info';
                const actionLabels = {
                    upload: '上传', download: '下载',
                    delete_local: '删除本地', delete_remote: '删除远程',
                    skip: '跳过', mkdir_local: '创建目录', mkdir_remote: '创建远程目录',
                    error: '错误', info: '信息'
                };
                const actionLabel = actionLabels[log.action] || log.action;
                const detail = [log.file_path, log.detail].filter(Boolean).map(s => this.escape(s)).join(' ');
                return `
                    <div class="log-entry">
                        <span class="log-time">${this.formatTime(log.timestamp)}</span>
                        <span class="log-action ${actionClass}">${actionLabel}</span>
                        <span class="log-task">${taskName}</span>
                        <span class="log-detail">${detail}</span>
                    </div>`;
            }).join('');

            // Skip DOM update if content hasn't changed (prevents flicker on auto-refresh)
            if (isAutoRefresh && html === this._lastLogsHtml) return;

            // Preserve scroll position on auto-refresh; scroll to top on manual load
            const savedScroll = isAutoRefresh ? container.scrollTop : 0;
            container.innerHTML = html;
            this._lastLogsHtml = html;
            if (isAutoRefresh) {
                container.scrollTop = savedScroll;
            } else {
                container.scrollTop = 0;
            }
        } catch (e) {
            if (!isAutoRefresh) {
                container.innerHTML = `<div class="empty-state" style="color:#f38ba8">${e.message}</div>`;
            }
        }
    },

    async clearAllLogs() {
        if (!confirm('确定清除所有日志？此操作不可撤销。')) return;
        try {
            await this.api('DELETE', '/api/logs');
            this.toast('日志已清除', 'success');
            this.loadLogs();
        } catch (e) {
            this.toast('清除失败: ' + e.message, 'error');
        }
    },

    // ---- Sync records (per-file transfer history) ----
    _records: [],
    _recordsTotal: 0,
    _recordsLoading: false,
    _recordsQuery: '',
    _recordsAction: '',
    _recordsTask: '',
    _recordsStatus: '',
    _recordsSearchTimer: null,

    openRecords() {
        this._records = [];
        this._recordsTotal = 0;
        this._recordsQuery = '';
        this._recordsAction = '';
        this._recordsTask = '';
        this._recordsStatus = '';
        ['records-search', 'records-filter', 'records-task', 'records-status'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        this.openModal('modal-records');
        this._bindRecordsScroll();
        this.loadRecords(true);
    },

    // Bound once: openRecords() runs every time the panel is opened, and a
    // listener added per open would fire the "load more" N times.
    _bindRecordsScroll() {
        const list = document.getElementById('records-list');
        if (!list || list.dataset.scrollBound === '1') return;
        list.dataset.scrollBound = '1';
        list.addEventListener('scroll', () => {
            if (this._recordsLoading) return;
            if (this._records.length >= this._recordsTotal) return;
            if (list.scrollTop + list.clientHeight >= list.scrollHeight - 240) {
                this.loadRecords(false);
            }
        });
    },

    async loadRecords(reset) {
        if (this._recordsLoading) return;
        const list = document.getElementById('records-list');
        this._recordsLoading = true;
        if (reset) {
            this._records = [];
            this._recordsTotal = 0;
            if (list) list.innerHTML = '<div class="empty-state">加载中...</div>';
        }
        try {
            const params = new URLSearchParams({
                limit: 60, offset: reset ? 0 : this._records.length
            });
            if (this._recordsAction) params.set('action', this._recordsAction);
            if (this._recordsTask) params.set('task_id', this._recordsTask);
            if (this._recordsStatus) params.set('status', this._recordsStatus);
            if (this._recordsQuery) params.set('q', this._recordsQuery);
            const data = await this.api('GET', '/api/records?' + params.toString());
            const rows = data.records || [];
            this._records = reset ? rows : this._records.concat(rows);
            this._recordsTotal = data.total || 0;
            this.renderRecords(data);
        } catch (e) {
            if (list) {
                list.innerHTML = `<div class="empty-state" style="color:var(--color-error)">${this.escape(e.message)}</div>`;
            }
        } finally {
            this._recordsLoading = false;
        }
    },

    reloadRecords() {
        const filter = document.getElementById('records-filter');
        const task = document.getElementById('records-task');
        const status = document.getElementById('records-status');
        this._recordsAction = filter ? filter.value : '';
        this._recordsTask = task ? task.value : '';
        this._recordsStatus = status ? status.value : '';
        this.loadRecords(true);
    },

    onRecordsSearch() {
        clearTimeout(this._recordsSearchTimer);
        this._recordsSearchTimer = setTimeout(() => {
            const el = document.getElementById('records-search');
            this._recordsQuery = el ? el.value.trim() : '';
            this.loadRecords(true);
        }, 280);
    },

    async clearRecords() {
        if (!confirm('确定清空同步记录？只删除这份历史清单，不会动本地或云端文件。')) return;
        try {
            await this.api('DELETE', '/api/records');
            this.toast('同步记录已清空', 'success');
            this.loadRecords(true);
        } catch (e) {
            this.toast('清空失败: ' + e.message, 'error');
        }
    },

    _recordIcon(cls) {
        const paths = {
            up: 'M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z',
            down: 'M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z',
            del: 'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z'
        };
        return `<svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="${paths[cls]}"/></svg>`;
    },

    _dayLabel(ts) {
        const d = new Date(ts * 1000);
        const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
        const days = Math.round((startOf(new Date()) - startOf(d)) / 86400000);
        if (days === 0) return '今天';
        if (days === 1) return '昨天';
        const md = `${d.getMonth() + 1}月${d.getDate()}日`;
        return d.getFullYear() === new Date().getFullYear()
            ? md : `${d.getFullYear()}年${md}`;
    },

    _renderRecordRow(r) {
        const action = r.action || '';
        const isDelete = action.indexOf('delete') === 0;
        const cls = isDelete ? 'del' : (action === 'upload' ? 'up' : 'down');
        const label = isDelete ? '删除' : (action === 'upload' ? '上传' : '下载');
        // upload / delete_remote start on the local side; download /
        // delete_local start on the cloud side.
        const localFirst = action === 'upload' || action === 'delete_remote';
        const cloud = this.escape(r.connection || '云端');
        const leftSide = localFirst ? '本地' : cloud;
        const rightSide = localFirst ? cloud : '本地';
        const leftPath = localFirst ? r.source : r.target;
        const rightPath = localFirst ? r.target : r.source;
        const failed = r.status === 'failed';
        const skipped = r.status === 'skipped';
        const statusCls = failed ? 'rec-failed' : (skipped ? 'rec-skipped' : 'rec-ok');
        const d = new Date((r.created_at || 0) * 1000);
        const pad = (n) => String(n).padStart(2, '0');
        const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
        const taskName = this.escape(r.task_name || `任务#${r.task_id}`);
        const size = r.file_size ? this.formatSize(r.file_size) : '';

        return `
            <div class="rec-item ${statusCls}" onclick="this.classList.toggle('expanded')">
                <div class="rec-icon rec-icon-${cls}">${this._recordIcon(cls)}</div>
                <div class="rec-main">
                    <div class="rec-line1">
                        <span class="rec-name" title="${this.escape(r.file_name)}">${this.escape(r.file_name || '(未命名)')}</span>
                        ${size ? `<span class="rec-size">${size}</span>` : ''}
                    </div>
                    <div class="rec-line2">
                        <span class="rec-end">
                            <span class="rec-side">${leftSide}</span>
                            <span class="rec-path" title="${this.escape(leftPath)}">${this.escape(leftPath)}</span>
                        </span>
                        <span class="rec-arrow">→</span>
                        <span class="rec-end">
                            <span class="rec-side">${rightSide}</span>
                            <span class="rec-path" title="${this.escape(rightPath)}">${this.escape(rightPath)}</span>
                        </span>
                    </div>
                    <div class="rec-line3">
                        <span class="rec-tag rec-tag-${cls}">${label}</span>
                        <span>${time}</span>
                        <span class="rec-sep">·</span>
                        <span class="rec-task">${taskName}</span>
                        ${failed ? `<span class="rec-fail" title="${this.escape(r.error || '')}">失败${(r.retry_count || 0) > 0 ? ` ×${(r.retry_count || 0) + 1}` : ''}</span>`
                                 : (skipped ? `<span class="rec-skip" title="${this.escape(r.error || '')}">已跳过</span>`
                                            : `<span class="rec-status-ok">成功</span>`)}
                    </div>
                </div>
                <div class="rec-actions">
                    ${failed || skipped ? `<button class="rec-retry" title="清除失败/跳过状态并立即重试"
                        onclick="event.stopPropagation(); app.retryRecord(${r.task_id}, ${r.id}, this)">重试</button>` : ''}
                    <button class="rec-del" title="删除这条记录（只删历史，不动文件）"
                        onclick="event.stopPropagation(); app.deleteRecord(${r.id}, this)">
                        <svg viewBox="0 0 24 24" width="12" height="12"><path fill="currentColor" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                    </button>
                </div>
            </div>`;
    },

    renderRecords(data) {
        const list = document.getElementById('records-list');
        if (!list) return;
        const stats = data.stats || {};
        const byAction = stats.by_action || {};
        const summary = document.getElementById('records-summary');
        if (summary) {
            const parts = [`共 ${stats.total || 0} 条`];
            if (byAction.upload) parts.push(`上传 ${byAction.upload}`);
            if (byAction.download) parts.push(`下载 ${byAction.download}`);
            if (byAction.delete) parts.push(`删除 ${byAction.delete}`);
            if (stats.failed) parts.push(`其中失败 ${stats.failed}`);
            if (stats.skipped) parts.push(`已跳过 ${stats.skipped}`);
            summary.textContent = parts.join(' · ');
        }

        // "Retry all" only makes sense when there is something to retry; keep
        // it visible but disabled otherwise so the toolbar layout is stable.
        const retryAllBtn = document.getElementById('records-retry-all');
        if (retryAllBtn) {
            const n = (stats.failed || 0) + (stats.skipped || 0);
            retryAllBtn.disabled = n === 0;
            retryAllBtn.title = n
                ? `重试全部失败/跳过的 ${n} 条记录`
                : '没有失败或跳过的记录';
        }

        const taskSel = document.getElementById('records-task');
        if (taskSel) {
            const opts = ['<option value="">全部任务</option>'];
            (stats.tasks || []).forEach(t => {
                const name = this.escape(t.task_name || `任务#${t.task_id}`);
                opts.push(`<option value="${t.task_id}">${name} (${t.n})</option>`);
            });
            taskSel.innerHTML = opts.join('');
            taskSel.value = this._recordsTask;
        }

        if (this._records.length === 0) {
            const filtered = this._recordsQuery || this._recordsAction || this._recordsTask;
            list.innerHTML = `<div class="empty-state">${filtered ? '没有匹配的记录' : '暂无同步记录'}</div>`;
            return;
        }

        const html = [];
        let lastDay = '';
        this._records.forEach(r => {
            const day = this._dayLabel(r.created_at);
            if (day !== lastDay) {
                html.push(`<div class="rec-day">${this.escape(day)}</div>`);
                lastDay = day;
            }
            html.push(this._renderRecordRow(r));
        });

        if (this._records.length < this._recordsTotal) {
            html.push(`<button class="records-more" id="records-more"
                onclick="app.loadRecords(false)">
                加载更多（${this._records.length}/${this._recordsTotal}）</button>`);
        } else if (this._recordsTotal > 20) {
            html.push(`<div class="records-end">已显示全部 ${this._recordsTotal} 条</div>`);
        }
        list.innerHTML = html.join('');
    },

    // ---- Progress Polling ----
    startProgressPolling() {
        if (this.progressTimer) clearTimeout(this.progressTimer);
        const tick = async () => {
            if (this.currentView === 'dashboard') {
                await this.loadDashboard();
            } else if (this.currentView === 'tasks') {
                await this.loadTasks();
            } else if (this.currentView === 'downloads') {
                this.loadDownloads();
            } else if (this.currentView === 'logs') {
                this.loadLogs(true);
            } else if (this.currentView === 'settings') {
                this.refreshSettingsActiveCounts();
            }
            // Adaptive interval: 1s when sync running (smooth speed/progress),
            // 3s when idle (less noise)
            const hasRunning = (this.tasks || []).some(t => t.is_running);
            const interval = hasRunning ? 1000 : 3000;
            this.progressTimer = setTimeout(tick, interval);
        };
        this.progressTimer = setTimeout(tick, 1000);
    },

    async refreshSettingsActiveCounts() {
        try {
            const info = await this.api('GET', '/api/system/info');
            const setText = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.textContent = String(v); };
            setText('settings-info-active', info.active_tasks);
            setText('settings-info-downloads', info.active_downloads);
        } catch (e) {}
    },

    // ---- Modal Helpers ----
    openModal(id) {
        document.getElementById(id).style.display = 'flex';
    },

    closeModal(id) {
        document.getElementById(id).style.display = 'none';
        if (id === 'modal-connection' && this.poll115Timer) {
            clearInterval(this.poll115Timer);
            this.poll115Timer = null;
        }
    },

    // ---- Init ----
    async loadAll() {
        await this.loadDashboard();
    }
};

const app = App;
App.init();
