/* ============================================================
   Support Console - Application Logic
   ============================================================
   Note: innerHTML is used intentionally for rendering trusted
   markdown/HTML from marked.js and highlight.js. All user-
   provided strings are escaped with escapeHtml() before
   insertion. Server-side content is considered trusted.
   ============================================================ */

(function () {
  'use strict';

  // --------------------------------------------------------
  // Configuration
  // --------------------------------------------------------

  const CONFIG = {
    wsReconnectBaseDelay: 1000,
    wsReconnectMaxDelay: 30000,
    wsReconnectBackoffFactor: 1.5,
    kernelPollInterval: 10000,
    chatAutoScrollThreshold: 80,
    maxInputHeight: 160,
    maxFileSize: 10 * 1024 * 1024,
    maxFiles: 5,
    allowedImageTypes: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'],
    allowedTextExtensions: [
      '.txt', '.log', '.csv', '.json', '.xml', '.yaml',
      '.py', '.js', '.ts', '.html', '.css', '.md',
      '.sh', '.sql', '.toml', '.ini', '.cfg', '.conf',
    ],
  };

  // --------------------------------------------------------
  // State
  // --------------------------------------------------------

  const state = {
    // WebSocket
    ws: null,
    wsConnected: false,
    wsReconnectAttempts: 0,
    wsReconnectTimer: null,

    // Chat
    messages: [],          // conversation history for server
    isStreaming: false,
    currentAssistantEl: null,
    currentAssistantContent: '',

    // Notebook
    cells: [],
    cellCounter: 0,

    // Kernel
    kernelAlive: false,
    kernelBusy: false,
    kernelPollTimer: null,

    // Divider drag
    isDragging: false,

    // File uploads
    pendingFiles: [],
    isUploading: false,
    dragCounter: 0,
  };

  // --------------------------------------------------------
  // DOM References
  // --------------------------------------------------------

  const dom = {};

  function cacheDom() {
    dom.header = document.getElementById('header');
    dom.wsStatus = document.getElementById('ws-status');
    dom.kernelStatus = document.getElementById('kernel-status');
    dom.chatPanel = document.getElementById('chat-panel');
    dom.chatMessages = document.getElementById('chat-messages');
    dom.chatStreaming = document.getElementById('chat-streaming');
    dom.chatInput = document.getElementById('chat-input');
    dom.chatSend = document.getElementById('chat-send');
    dom.divider = document.getElementById('divider');
    dom.notebookPanel = document.getElementById('notebook-panel');
    dom.notebookCells = document.getElementById('notebook-cells');
    dom.addCell = document.getElementById('add-cell');
    dom.clearNotebook = document.getElementById('clear-notebook');
    dom.reconnectingOverlay = document.getElementById('reconnecting-overlay');
    dom.main = document.getElementById('main');
    dom.attachBtn = document.getElementById('attach-btn');
    dom.fileInput = document.getElementById('file-input');
    dom.attachmentPreview = document.getElementById('attachment-preview');
    dom.dropOverlay = document.getElementById('drop-overlay');
    dom.fileAnnounce = document.getElementById('file-announce');
  }

  // --------------------------------------------------------
  // Markdown Configuration
  // --------------------------------------------------------

  function configureMarked() {
    const renderer = new marked.Renderer();

    // Custom code block renderer with "Send to Notebook" buttons
    renderer.code = function (codeObj) {
      // marked v12+ passes an object {text, lang, escaped}
      const text = typeof codeObj === 'object' ? codeObj.text : codeObj;
      const lang = typeof codeObj === 'object' ? (codeObj.lang || '') : (arguments[1] || '');

      const langLabel = lang || 'code';

      let highlighted;
      try {
        if (lang && hljs.getLanguage(lang)) {
          highlighted = hljs.highlight(text, { language: lang }).value;
        } else {
          highlighted = hljs.highlightAuto(text).value;
        }
      } catch (_) {
        highlighted = escapeHtml(text);
      }

      const id = 'codeblock-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);

      // Build the wrapper using DOM-safe attribute encoding
      const escapedCode = escapeAttr(text);
      const escapedLang = escapeAttr(lang);
      const escapedLangLabel = escapeHtml(langLabel);
      const langClass = lang ? 'language-' + escapeHtml(lang) : '';

      return '<div class="code-block-wrapper" data-code-id="' + id + '" data-code="' + escapedCode + '" data-lang="' + escapedLang + '">'
        + '<div class="code-block-header">'
        + '<span class="code-block-lang">' + escapedLangLabel + '</span>'
        + '<div class="code-block-actions">'
        + '<button class="btn-copy" data-copy-id="' + id + '" title="Copy code">Copy</button>'
        + '<button class="btn-send-notebook" data-notebook-id="' + id + '" title="Send code to notebook">Send to Notebook &rarr;</button>'
        + '</div>'
        + '</div>'
        + '<pre><code class="' + langClass + '">' + highlighted + '</code></pre>'
        + '</div>';
    };

    marked.setOptions({
      renderer: renderer,
      gfm: true,
      breaks: false,
    });
  }

  // --------------------------------------------------------
  // Utility Functions
  // --------------------------------------------------------

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function escapeAttr(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function generateId() {
    return 'id-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function getFileExtension(name) {
    var dot = name.lastIndexOf('.');
    return dot >= 0 ? name.slice(dot).toLowerCase() : '';
  }

  function isAllowedFile(file) {
    if (CONFIG.allowedImageTypes.indexOf(file.type) !== -1) return true;
    var ext = getFileExtension(file.name);
    return CONFIG.allowedTextExtensions.indexOf(ext) !== -1;
  }

  // --------------------------------------------------------
  // WebSocket Connection
  // --------------------------------------------------------

  function connectWebSocket() {
    if (state.ws && (state.ws.readyState === WebSocket.CONNECTING || state.ws.readyState === WebSocket.OPEN)) {
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = protocol + '//' + window.location.host + '/api/chat';

    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = function () {
      state.wsConnected = true;
      state.wsReconnectAttempts = 0;
      updateWsStatus('connected');
      dom.reconnectingOverlay.classList.add('hidden');
    };

    state.ws.onclose = function () {
      state.wsConnected = false;
      updateWsStatus('disconnected');
      scheduleReconnect();
    };

    state.ws.onerror = function () {
      state.wsConnected = false;
      updateWsStatus('disconnected');
    };

    state.ws.onmessage = function (event) {
      handleWsMessage(event.data);
    };
  }

  function scheduleReconnect() {
    if (state.wsReconnectTimer) return;

    state.wsReconnectAttempts++;
    const delay = Math.min(
      CONFIG.wsReconnectBaseDelay * Math.pow(CONFIG.wsReconnectBackoffFactor, state.wsReconnectAttempts - 1),
      CONFIG.wsReconnectMaxDelay
    );

    updateWsStatus('reconnecting');

    if (state.wsReconnectAttempts > 2) {
      dom.reconnectingOverlay.classList.remove('hidden');
    }

    state.wsReconnectTimer = setTimeout(function () {
      state.wsReconnectTimer = null;
      connectWebSocket();
    }, delay);
  }

  function updateWsStatus(status) {
    const el = dom.wsStatus;
    el.className = 'status-badge status-' + status;
    const label = el.querySelector('.status-label');
    const labels = {
      connected: 'Connected',
      disconnected: 'Disconnected',
      reconnecting: 'Reconnecting...',
    };
    label.textContent = labels[status] || status;
  }

  // --------------------------------------------------------
  // WebSocket Message Handler
  // --------------------------------------------------------

  function handleWsMessage(raw) {
    let data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      console.error('Failed to parse WebSocket message:', raw);
      return;
    }

    switch (data.type) {
      case 'text':
        handleTextEvent(data);
        break;
      case 'tool_use':
        handleToolUseEvent(data);
        break;
      case 'tool_result':
        handleToolResultEvent(data);
        break;
      case 'code_block':
        handleCodeBlockEvent(data);
        break;
      case 'done':
        handleDoneEvent(data);
        break;
      case 'history_update':
        handleHistoryUpdate(data);
        break;
      case 'error':
        handleErrorEvent(data);
        break;
      default:
        console.warn('Unknown WS event type:', data.type);
    }
  }

  function handleTextEvent(data) {
    if (!state.currentAssistantEl) {
      state.currentAssistantEl = createAssistantMessage();
      state.currentAssistantContent = '';
    }

    state.currentAssistantContent += data.content;
    renderAssistantContent(state.currentAssistantEl, state.currentAssistantContent);
    autoScrollChat();
  }

  function handleToolUseEvent(data) {
    if (!state.currentAssistantEl) {
      state.currentAssistantEl = createAssistantMessage();
      state.currentAssistantContent = '';
    }

    const toolEl = createToolUsageElement(data.name, data.input, null);
    const contentEl = state.currentAssistantEl.querySelector('.message-content');
    contentEl.appendChild(toolEl);
    // Store reference for updating when result comes in
    toolEl.dataset.toolCallId = data.id || '';
    autoScrollChat();
  }

  function handleToolResultEvent(data) {
    // Try to find the matching tool_use element and add the result
    if (state.currentAssistantEl) {
      const contentEl = state.currentAssistantEl.querySelector('.message-content');
      const toolEls = contentEl.querySelectorAll('.tool-usage');
      // Find last tool without a result for this tool name
      for (let i = toolEls.length - 1; i >= 0; i--) {
        const nameEl = toolEls[i].querySelector('.tool-usage-name');
        const resultSection = toolEls[i].querySelector('.tool-usage-result');
        if (nameEl && nameEl.textContent === data.name && !resultSection) {
          appendToolResult(toolEls[i], data.result);
          autoScrollChat();
          return;
        }
      }
    }

    // Fallback: create a standalone result element
    if (state.currentAssistantEl) {
      const contentEl = state.currentAssistantEl.querySelector('.message-content');
      const toolEl = createToolUsageElement(data.name, null, data.result);
      contentEl.appendChild(toolEl);
      autoScrollChat();
    }
  }

  function handleCodeBlockEvent(data) {
    createCell(data.code, data.language || 'python');
  }

  function handleDoneEvent(_data) {
    state.isStreaming = false;
    state.currentAssistantEl = null;
    state.currentAssistantContent = '';
    hideStreamingStatus();
    setInputsDisabled(false);
    dom.chatInput.focus();
  }

  function handleHistoryUpdate(data) {
    if (data.messages) {
      state.messages = data.messages;
    }
  }

  function handleErrorEvent(data) {
    state.isStreaming = false;
    state.currentAssistantEl = null;
    state.currentAssistantContent = '';
    hideStreamingStatus();
    setInputsDisabled(false);

    appendSystemMessage('Error: ' + (data.error || 'Unknown error'), 'error');
    autoScrollChat();
  }

  // --------------------------------------------------------
  // Chat UI
  // --------------------------------------------------------

  function clearWelcome() {
    const welcome = dom.chatMessages.querySelector('.chat-welcome');
    if (welcome) welcome.remove();
  }

  function createUserMessage(text, attachments) {
    clearWelcome();
    var el = document.createElement('div');
    el.className = 'message message-user';

    var roleEl = document.createElement('div');
    roleEl.className = 'message-role';
    roleEl.textContent = 'You';

    var contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    if (text) {
      contentEl.textContent = text;
    }

    el.appendChild(roleEl);
    el.appendChild(contentEl);

    // Attachment chips
    if (attachments && attachments.length > 0) {
      var chipsEl = document.createElement('div');
      chipsEl.className = 'message-attachments';
      for (var i = 0; i < attachments.length; i++) {
        var chip = document.createElement('span');
        chip.className = 'attachment-chip';
        chip.textContent = attachments[i].name + ' (' + formatFileSize(attachments[i].size) + ')';
        chipsEl.appendChild(chip);
      }
      el.appendChild(chipsEl);
    }

    dom.chatMessages.appendChild(el);
    autoScrollChat();
    return el;
  }

  function createAssistantMessage() {
    clearWelcome();
    const el = document.createElement('div');
    el.className = 'message message-assistant';

    const roleEl = document.createElement('div');
    roleEl.className = 'message-role';
    roleEl.textContent = 'Assistant';

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';

    el.appendChild(roleEl);
    el.appendChild(contentEl);
    dom.chatMessages.appendChild(el);
    dom.chatStreaming.classList.remove('hidden');
    return el;
  }

  function renderAssistantContent(msgEl, markdownText) {
    const contentEl = msgEl.querySelector('.message-content');

    // Preserve any tool-usage elements that were inserted
    const toolElements = contentEl.querySelectorAll('.tool-usage');
    const savedTools = [];
    for (let i = 0; i < toolElements.length; i++) {
      savedTools.push(toolElements[i]);
    }

    // Temporarily detach tool elements so they are not destroyed
    savedTools.forEach(function (t) { t.remove(); });

    // Render markdown using marked.js (trusted server content)
    // marked.parse output contains highlight.js syntax-highlighted code
    try {
      contentEl.innerHTML = marked.parse(markdownText); // eslint-disable-line no-unsanitized/property
    } catch (e) {
      contentEl.textContent = markdownText;
    }

    // Wire up code block action buttons via event delegation
    wireCodeBlockButtons(contentEl);

    // Re-append tool elements
    savedTools.forEach(function (t) {
      contentEl.appendChild(t);
    });
  }

  function wireCodeBlockButtons(container) {
    // Copy buttons
    var copyBtns = container.querySelectorAll('[data-copy-id]');
    for (var i = 0; i < copyBtns.length; i++) {
      (function (btn) {
        if (btn._wired) return;
        btn._wired = true;
        btn.addEventListener('click', function () {
          handleCopyCode(btn.getAttribute('data-copy-id'));
        });
      })(copyBtns[i]);
    }

    // Send to notebook buttons
    var notebookBtns = container.querySelectorAll('[data-notebook-id]');
    for (var j = 0; j < notebookBtns.length; j++) {
      (function (btn) {
        if (btn._wired) return;
        btn._wired = true;
        btn.addEventListener('click', function () {
          handleSendToNotebook(btn.getAttribute('data-notebook-id'));
        });
      })(notebookBtns[j]);
    }
  }

  function handleCopyCode(id) {
    var wrapper = document.querySelector('[data-code-id="' + id + '"]');
    if (!wrapper) return;
    var code = wrapper.dataset.code;
    var btn = wrapper.querySelector('[data-copy-id]');

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(function () {
        if (btn) {
          var orig = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(function () { btn.textContent = orig; }, 1500);
        }
      }).catch(function () {
        fallbackCopy(code, btn);
      });
    } else {
      fallbackCopy(code, btn);
    }
  }

  function fallbackCopy(text, btn) {
    var textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand('copy');
      if (btn) {
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = orig; }, 1500);
      }
    } catch (_) {
      if (btn) {
        var origText = btn.textContent;
        btn.textContent = 'Failed';
        setTimeout(function () { btn.textContent = origText; }, 1500);
      }
    }
    document.body.removeChild(textarea);
  }

  function handleSendToNotebook(id) {
    var wrapper = document.querySelector('[data-code-id="' + id + '"]');
    if (!wrapper) return;
    var code = wrapper.dataset.code;
    var lang = wrapper.dataset.lang || 'python';
    createCell(code, lang);

    // Visual feedback
    var btn = wrapper.querySelector('[data-notebook-id]');
    if (btn) {
      var origText = btn.textContent;
      btn.textContent = 'Sent!';
      btn.style.color = 'var(--accent-green)';
      setTimeout(function () {
        btn.textContent = origText;
        btn.style.color = '';
      }, 1500);
    }
  }

  function appendSystemMessage(text, type) {
    clearWelcome();
    var el = document.createElement('div');
    el.className = 'message message-assistant';
    var color = type === 'error' ? 'var(--accent-red)' : 'var(--text-secondary)';

    var roleEl = document.createElement('div');
    roleEl.className = 'message-role';
    roleEl.textContent = 'System';
    roleEl.style.color = color;

    var contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    contentEl.textContent = text;
    contentEl.style.color = color;

    el.appendChild(roleEl);
    el.appendChild(contentEl);
    dom.chatMessages.appendChild(el);
  }

  function autoScrollChat() {
    const el = dom.chatMessages;
    const threshold = CONFIG.chatAutoScrollThreshold;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < threshold) {
      el.scrollTop = el.scrollHeight;
    }
  }

  async function sendMessage() {
    var text = dom.chatInput.value.trim();
    if ((!text && state.pendingFiles.length === 0) || state.isStreaming || state.isUploading) return;

    if (!state.wsConnected) {
      appendSystemMessage('Not connected to server. Please wait for reconnection.', 'error');
      return;
    }

    // Show user message with attachment info
    var attachmentMeta = state.pendingFiles.map(function (f) {
      return { name: f.name, size: f.size, type: f.type };
    });
    createUserMessage(text, attachmentMeta.length > 0 ? attachmentMeta : null);

    var fileIds = [];

    // Upload files if any
    if (state.pendingFiles.length > 0) {
      state.isUploading = true;
      setInputsDisabled(true);
      showStreamingStatus('Uploading files...');

      try {
        var formData = new FormData();
        for (var i = 0; i < state.pendingFiles.length; i++) {
          formData.append('files', state.pendingFiles[i]);
        }

        var resp = await fetch('/api/upload', {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          body: formData,
        });

        if (!resp.ok) {
          var err = await resp.json().catch(function () { return { detail: 'Upload failed' }; });
          throw new Error(err.detail || 'Upload failed (' + resp.status + ')');
        }

        var result = await resp.json();
        fileIds = result.files.map(function (f) { return f.id; });
      } catch (e) {
        state.isUploading = false;
        setInputsDisabled(false);
        hideStreamingStatus();
        appendSystemMessage('Upload failed: ' + e.message, 'error');
        return; // Keep files for retry
      }

      // Clear pending files on success
      state.pendingFiles = [];
      renderAttachmentPreview();
      state.isUploading = false;
    }

    // Send to server via WebSocket
    var payload = {
      message: text || '(see attached files)',
      messages: state.messages,
    };
    if (fileIds.length > 0) {
      payload.file_ids = fileIds;
    }

    try {
      state.ws.send(JSON.stringify(payload));
    } catch (e) {
      setInputsDisabled(false);
      hideStreamingStatus();
      appendSystemMessage('Failed to send message: ' + e.message, 'error');
      return;
    }

    // Update state
    state.isStreaming = true;
    dom.chatInput.value = '';
    dom.chatInput.style.height = 'auto';
    setInputsDisabled(true);
    showStreamingStatus('Assistant is responding...');
  }

  function setInputsDisabled(disabled) {
    dom.chatInput.disabled = disabled;
    dom.chatSend.disabled = disabled;
    dom.attachBtn.disabled = disabled;
  }

  function showStreamingStatus(text) {
    dom.chatStreaming.classList.remove('hidden');
    var label = dom.chatStreaming.querySelector('.streaming-label');
    if (label) label.textContent = text;
  }

  function hideStreamingStatus() {
    dom.chatStreaming.classList.add('hidden');
  }

  // --------------------------------------------------------
  // Tool Usage UI
  // --------------------------------------------------------

  function createToolUsageElement(name, input, result) {
    var el = document.createElement('div');
    el.className = 'tool-usage';

    // Header
    var header = document.createElement('div');
    header.className = 'tool-usage-header';

    var chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chevron.setAttribute('class', 'tool-usage-chevron');
    chevron.setAttribute('viewBox', '0 0 16 16');
    chevron.setAttribute('fill', 'currentColor');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M6.22 3.22a.75.75 0 011.06 0l4.25 4.25a.75.75 0 010 1.06l-4.25 4.25a.75.75 0 01-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 010-1.06z');
    chevron.appendChild(path);
    header.appendChild(chevron);

    var nameSpan = document.createElement('span');
    nameSpan.className = 'tool-usage-name';
    nameSpan.textContent = name;
    header.appendChild(nameSpan);

    var summarySpan = document.createElement('span');
    summarySpan.className = 'tool-usage-summary';
    if (input) {
      var summary = '';
      if (typeof input === 'object') {
        var vals = Object.values(input).filter(function (v) { return typeof v === 'string'; });
        summary = vals.join(', ');
      } else {
        summary = String(input);
      }
      if (summary.length > 50) summary = summary.slice(0, 50) + '...';
      summarySpan.textContent = summary;
    }
    header.appendChild(summarySpan);

    el.appendChild(header);

    // Body
    var body = document.createElement('div');
    body.className = 'tool-usage-body';

    if (input) {
      var inputSection = document.createElement('div');
      inputSection.className = 'tool-usage-input';
      var inputLabel = document.createElement('div');
      inputLabel.className = 'tool-usage-label';
      inputLabel.textContent = 'Input';
      var inputContent = document.createElement('div');
      inputContent.className = 'tool-usage-content';
      inputContent.textContent = typeof input === 'object' ? JSON.stringify(input, null, 2) : String(input);
      inputSection.appendChild(inputLabel);
      inputSection.appendChild(inputContent);
      body.appendChild(inputSection);
    }

    if (result !== null && result !== undefined) {
      var resultSection = document.createElement('div');
      resultSection.className = 'tool-usage-result';
      var resultLabel = document.createElement('div');
      resultLabel.className = 'tool-usage-label';
      resultLabel.textContent = 'Result';
      var resultContent = document.createElement('div');
      resultContent.className = 'tool-usage-content';
      resultContent.textContent = typeof result === 'object' ? JSON.stringify(result, null, 2) : String(result);
      resultSection.appendChild(resultLabel);
      resultSection.appendChild(resultContent);
      body.appendChild(resultSection);
    }

    el.appendChild(body);

    // Make header keyboard-accessible
    header.setAttribute('role', 'button');
    header.setAttribute('tabindex', '0');
    header.setAttribute('aria-expanded', 'false');

    // Toggle expand/collapse
    function toggleExpand() {
      var expanded = el.classList.toggle('expanded');
      header.setAttribute('aria-expanded', String(expanded));
    }

    header.addEventListener('click', toggleExpand);
    header.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggleExpand();
      }
    });

    return el;
  }

  function appendToolResult(toolEl, result) {
    var body = toolEl.querySelector('.tool-usage-body');
    var resultSection = document.createElement('div');
    resultSection.className = 'tool-usage-result';
    var resultLabel = document.createElement('div');
    resultLabel.className = 'tool-usage-label';
    resultLabel.textContent = 'Result';
    var resultContent = document.createElement('div');
    resultContent.className = 'tool-usage-content';
    resultContent.textContent = typeof result === 'object' ? JSON.stringify(result, null, 2) : String(result);
    resultSection.appendChild(resultLabel);
    resultSection.appendChild(resultContent);
    body.appendChild(resultSection);
  }

  // --------------------------------------------------------
  // Notebook Cells
  // --------------------------------------------------------

  function createCell(code, language) {
    clearNotebookEmpty();

    state.cellCounter++;
    var cellId = generateId();

    var cellData = {
      id: cellId,
      number: state.cellCounter,
      code: code || '',
      language: language || 'python',
      status: 'idle',       // idle | running | done | error
      output: null,
    };

    state.cells.push(cellData);

    var el = buildCellElement(cellData);
    dom.notebookCells.appendChild(el);

    // Auto-scroll notebook to the new cell
    dom.notebookCells.scrollTop = dom.notebookCells.scrollHeight;

    // Focus the textarea
    var textarea = el.querySelector('textarea');
    if (textarea) {
      textarea.focus();
      // Move cursor to end
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    }

    return cellData;
  }

  function buildCellElement(cellData) {
    var el = document.createElement('div');
    el.className = 'cell cell-idle';
    el.id = 'cell-' + cellData.id;
    el.dataset.cellId = cellData.id;
    el.setAttribute('role', 'listitem');
    el.setAttribute('aria-label', 'Cell ' + cellData.number);

    // Cell header
    var header = document.createElement('div');
    header.className = 'cell-header';

    var numberSpan = document.createElement('span');
    numberSpan.className = 'cell-number';
    numberSpan.textContent = '[' + cellData.number + ']';
    header.appendChild(numberSpan);

    var statusIndicator = document.createElement('span');
    statusIndicator.className = 'cell-status-indicator';
    statusIndicator.title = 'idle';
    header.appendChild(statusIndicator);

    var actions = document.createElement('div');
    actions.className = 'cell-actions';

    var runBtn = document.createElement('button');
    runBtn.className = 'cell-btn cell-btn-run';
    runBtn.title = 'Run cell (Ctrl+Enter)';
    runBtn.dataset.action = 'run';
    runBtn.textContent = '\u25B6'; // play triangle
    actions.appendChild(runBtn);

    var interruptBtn = document.createElement('button');
    interruptBtn.className = 'cell-btn cell-btn-interrupt hidden';
    interruptBtn.title = 'Interrupt execution';
    interruptBtn.dataset.action = 'interrupt';
    interruptBtn.textContent = '\u25A0'; // stop square
    actions.appendChild(interruptBtn);

    var deleteBtn = document.createElement('button');
    deleteBtn.className = 'cell-btn cell-btn-delete';
    deleteBtn.title = 'Delete cell';
    deleteBtn.dataset.action = 'delete';
    deleteBtn.textContent = '\u2715'; // multiplication X
    actions.appendChild(deleteBtn);

    header.appendChild(actions);
    el.appendChild(header);

    // Cell editor
    var editor = document.createElement('div');
    editor.className = 'cell-editor';

    var textarea = document.createElement('textarea');
    textarea.placeholder = '# Enter code here...';
    textarea.spellcheck = false;
    textarea.setAttribute('aria-label', 'Code for cell ' + cellData.number);
    textarea.value = cellData.code;
    editor.appendChild(textarea);
    el.appendChild(editor);

    // Cell output
    var output = document.createElement('div');
    output.className = 'cell-output';
    el.appendChild(output);

    // Wire up textarea events
    textarea.addEventListener('keydown', handleCellTextareaKeydown);
    textarea.addEventListener('input', autosizeCellTextarea);

    // Initial autosize
    requestAnimationFrame(function () {
      autosizeCellTextarea.call(textarea);
    });

    // Wire up buttons
    runBtn.addEventListener('click', function () {
      runCell(cellData.id);
    });
    interruptBtn.addEventListener('click', function () {
      interruptKernel(cellData.id);
    });
    deleteBtn.addEventListener('click', function () {
      deleteCell(cellData.id);
    });

    return el;
  }

  function clearNotebookEmpty() {
    var empty = dom.notebookCells.querySelector('.notebook-empty');
    if (empty) empty.remove();
  }

  function showNotebookEmpty() {
    if (state.cells.length === 0 && !dom.notebookCells.querySelector('.notebook-empty')) {
      var emptyDiv = document.createElement('div');
      emptyDiv.className = 'notebook-empty';
      var p = document.createElement('p');
      p.textContent = 'No cells yet. Send code from chat or add a new cell.';
      emptyDiv.appendChild(p);
      dom.notebookCells.appendChild(emptyDiv);
    }
  }

  function getCellData(cellId) {
    return state.cells.find(function (c) { return c.id === cellId; });
  }

  function getCellElement(cellId) {
    return document.getElementById('cell-' + cellId);
  }

  function updateCellStatus(cellId, status) {
    var cellData = getCellData(cellId);
    if (!cellData) return;
    cellData.status = status;

    var el = getCellElement(cellId);
    if (!el) return;

    el.className = 'cell cell-' + status;

    var indicator = el.querySelector('.cell-status-indicator');
    if (indicator) indicator.title = status;

    var runBtn = el.querySelector('[data-action="run"]');
    var interruptBtn = el.querySelector('[data-action="interrupt"]');

    if (status === 'running') {
      runBtn.classList.add('hidden');
      interruptBtn.classList.remove('hidden');
    } else {
      runBtn.classList.remove('hidden');
      interruptBtn.classList.add('hidden');
    }
  }

  function setCellOutput(cellId, output) {
    var el = getCellElement(cellId);
    if (!el) return;

    var outputEl = el.querySelector('.cell-output');
    // Clear previous output using DOM methods
    while (outputEl.firstChild) {
      outputEl.removeChild(outputEl.firstChild);
    }

    var hasContent = false;

    if (output.stdout) {
      hasContent = true;
      var stdoutSection = document.createElement('div');
      stdoutSection.className = 'cell-output-section cell-output-stdout';
      stdoutSection.textContent = output.stdout;
      outputEl.appendChild(stdoutSection);
    }

    if (output.stderr) {
      hasContent = true;
      var stderrSection = document.createElement('div');
      stderrSection.className = 'cell-output-section cell-output-stderr';
      stderrSection.textContent = output.stderr;
      outputEl.appendChild(stderrSection);
    }

    if (output.result !== undefined && output.result !== null && output.result !== '' && output.result !== 'None') {
      hasContent = true;
      var resultSection = document.createElement('div');
      resultSection.className = 'cell-output-section cell-output-result';
      resultSection.textContent = output.result;
      outputEl.appendChild(resultSection);
    }

    if (output.display_data) {
      hasContent = true;
      var displaySection = document.createElement('div');
      displaySection.className = 'cell-output-section cell-output-display';
      if (typeof output.display_data === 'string') {
        // display_data may contain trusted HTML from the kernel (images, etc.)
        displaySection.innerHTML = output.display_data; // eslint-disable-line no-unsanitized/property
      } else {
        displaySection.textContent = JSON.stringify(output.display_data, null, 2);
      }
      outputEl.appendChild(displaySection);
    }

    if (output.error) {
      hasContent = true;
      var errorSection = document.createElement('div');
      errorSection.className = 'cell-output-section cell-output-error';
      errorSection.textContent = typeof output.error === 'object' ? JSON.stringify(output.error, null, 2) : output.error;
      outputEl.appendChild(errorSection);
    }

    if (hasContent) {
      outputEl.classList.add('has-output');
    } else {
      outputEl.classList.remove('has-output');
    }
  }

  async function runCell(cellId) {
    var cellData = getCellData(cellId);
    if (!cellData) return;

    var el = getCellElement(cellId);
    if (!el) return;

    var textarea = el.querySelector('textarea');
    var code = textarea.value.trim();

    if (!code) return;

    cellData.code = code;
    updateCellStatus(cellId, 'running');

    // Clear previous output
    var outputEl = el.querySelector('.cell-output');
    while (outputEl.firstChild) {
      outputEl.removeChild(outputEl.firstChild);
    }
    outputEl.classList.remove('has-output');

    try {
      var response = await fetch('/api/kernel/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code, timeout: 30 }),
      });

      var result = await response.json();

      if (result.status === 'ok' || result.status === 'success') {
        updateCellStatus(cellId, 'done');
      } else {
        updateCellStatus(cellId, 'error');
      }

      setCellOutput(cellId, result);
    } catch (err) {
      updateCellStatus(cellId, 'error');
      setCellOutput(cellId, { error: 'Request failed: ' + err.message });
    }
  }

  async function interruptKernel(cellId) {
    try {
      var response = await fetch('/api/kernel/interrupt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });

      var result = await response.json();
      if (result.success) {
        updateCellStatus(cellId, 'error');
        setCellOutput(cellId, { error: 'Execution interrupted by user.' });
      }
    } catch (err) {
      console.error('Failed to interrupt kernel:', err);
    }
  }

  function deleteCell(cellId) {
    var idx = state.cells.findIndex(function (c) { return c.id === cellId; });
    if (idx === -1) return;

    state.cells.splice(idx, 1);

    var el = getCellElement(cellId);
    if (el) {
      el.remove();
    }

    showNotebookEmpty();
  }

  function clearAllCells() {
    state.cells = [];
    state.cellCounter = 0;
    // Clear all children using DOM methods
    while (dom.notebookCells.firstChild) {
      dom.notebookCells.removeChild(dom.notebookCells.firstChild);
    }
    showNotebookEmpty();
  }

  // --------------------------------------------------------
  // Cell Textarea Handling
  // --------------------------------------------------------

  function handleCellTextareaKeydown(e) {
    var textarea = e.target;

    // Tab key inserts spaces instead of moving focus
    if (e.key === 'Tab') {
      e.preventDefault();
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;

      if (e.shiftKey) {
        // Dedent: remove up to 4 leading spaces or one tab from the current line
        var val = textarea.value;
        var lineStart = val.lastIndexOf('\n', start - 1) + 1;
        var lineText = val.substring(lineStart, end);

        var removed = 0;
        if (lineText.startsWith('\t')) {
          removed = 1;
        } else {
          var match = lineText.match(/^ {1,4}/);
          if (match) removed = match[0].length;
        }

        if (removed > 0) {
          textarea.value = val.substring(0, lineStart) + val.substring(lineStart + removed);
          textarea.selectionStart = textarea.selectionEnd = start - removed;
        }
      } else {
        // Insert 4 spaces (soft tab)
        textarea.value = textarea.value.substring(0, start) + '    ' + textarea.value.substring(end);
        textarea.selectionStart = textarea.selectionEnd = start + 4;
      }

      // Trigger input event for autosize
      textarea.dispatchEvent(new Event('input'));
    }

    // Ctrl/Cmd + Enter to run cell
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      var cellEl = textarea.closest('.cell');
      if (cellEl) {
        runCell(cellEl.dataset.cellId);
      }
    }
  }

  function autosizeCellTextarea() {
    var textarea = this;
    textarea.style.height = 'auto';
    textarea.style.height = Math.max(60, textarea.scrollHeight) + 'px';
  }

  // --------------------------------------------------------
  // Chat Input Handling
  // --------------------------------------------------------

  function setupChatInput() {
    dom.chatInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-resize input
    dom.chatInput.addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, CONFIG.maxInputHeight) + 'px';
    });

    dom.chatSend.addEventListener('click', function () {
      sendMessage();
    });
  }

  // --------------------------------------------------------
  // File Upload
  // --------------------------------------------------------

  function setupFileUpload() {
    // Paperclip button opens file picker
    dom.attachBtn.addEventListener('click', function () {
      if (!state.isStreaming && !state.isUploading) {
        dom.fileInput.click();
      }
    });

    // File input change
    dom.fileInput.addEventListener('change', function () {
      addFiles(Array.from(this.files));
      this.value = ''; // reset so same file can be re-selected
    });

    // Drag and drop on chat panel
    dom.chatPanel.addEventListener('dragenter', function (e) {
      e.preventDefault();
      state.dragCounter++;
      if (state.dragCounter === 1) {
        dom.dropOverlay.classList.remove('hidden');
      }
    });

    dom.chatPanel.addEventListener('dragleave', function (e) {
      e.preventDefault();
      state.dragCounter--;
      if (state.dragCounter === 0) {
        dom.dropOverlay.classList.add('hidden');
      }
    });

    dom.chatPanel.addEventListener('dragover', function (e) {
      e.preventDefault();
    });

    dom.chatPanel.addEventListener('drop', function (e) {
      e.preventDefault();
      state.dragCounter = 0;
      dom.dropOverlay.classList.add('hidden');
      if (e.dataTransfer && e.dataTransfer.files.length > 0) {
        addFiles(Array.from(e.dataTransfer.files));
      }
    });

    // Clipboard paste for images
    dom.chatInput.addEventListener('paste', function (e) {
      if (!e.clipboardData || !e.clipboardData.items) return;
      var imageFiles = [];
      for (var i = 0; i < e.clipboardData.items.length; i++) {
        var item = e.clipboardData.items[i];
        if (item.type.indexOf('image/') === 0) {
          var file = item.getAsFile();
          if (file) {
            // Give pasted images a meaningful name
            var ext = file.type.split('/')[1] || 'png';
            var named = new File([file], 'clipboard-' + Date.now() + '.' + ext, { type: file.type });
            imageFiles.push(named);
          }
        }
      }
      if (imageFiles.length > 0) {
        e.preventDefault();
        addFiles(imageFiles);
      }
      // If no images found, let the default paste (text) happen
    });
  }

  function addFiles(files) {
    var errors = [];

    for (var i = 0; i < files.length; i++) {
      var file = files[i];

      // Check total count
      if (state.pendingFiles.length >= CONFIG.maxFiles) {
        errors.push('Maximum ' + CONFIG.maxFiles + ' files allowed');
        break;
      }

      // Check size
      if (file.size > CONFIG.maxFileSize) {
        errors.push(file.name + ' exceeds ' + formatFileSize(CONFIG.maxFileSize) + ' limit');
        continue;
      }

      // Check type
      if (!isAllowedFile(file)) {
        errors.push(file.name + ': unsupported file type');
        continue;
      }

      // Check for duplicate filename
      var isDuplicate = state.pendingFiles.some(function (f) { return f.name === file.name; });
      if (isDuplicate) {
        errors.push(file.name + ': already attached');
        continue;
      }

      state.pendingFiles.push(file);
    }

    if (errors.length > 0) {
      appendSystemMessage(errors.join('. '), 'error');
    }

    renderAttachmentPreview();
    announceFiles();
  }

  function removeFile(index) {
    var file = state.pendingFiles[index];
    state.pendingFiles.splice(index, 1);
    renderAttachmentPreview();
    announceFiles();
  }

  function announceFiles() {
    var count = state.pendingFiles.length;
    if (count === 0) {
      dom.fileAnnounce.textContent = 'All files removed';
    } else {
      dom.fileAnnounce.textContent = count + ' file' + (count !== 1 ? 's' : '') + ' attached';
    }
  }

  function renderAttachmentPreview() {
    var container = dom.attachmentPreview;
    // Clear existing
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }

    if (state.pendingFiles.length === 0) {
      container.classList.add('hidden');
      return;
    }

    container.classList.remove('hidden');

    for (var i = 0; i < state.pendingFiles.length; i++) {
      (function (index) {
        var file = state.pendingFiles[index];
        var item = document.createElement('div');
        item.className = 'attachment-item';
        item.setAttribute('role', 'listitem');
        item.setAttribute('aria-label', file.name + ', ' + formatFileSize(file.size));

        if (file.type && file.type.indexOf('image/') === 0) {
          var thumb = document.createElement('img');
          thumb.className = 'attachment-thumb';
          var url = URL.createObjectURL(file);
          thumb.src = url;
          thumb.alt = file.name;
          thumb.onload = function () { URL.revokeObjectURL(url); };
          item.appendChild(thumb);
        } else {
          var icon = document.createElement('span');
          icon.className = 'attachment-icon';
          icon.textContent = '\uD83D\uDCC4'; // file emoji as fallback
          icon.setAttribute('aria-hidden', 'true');
          item.appendChild(icon);
        }

        var nameSpan = document.createElement('span');
        nameSpan.className = 'attachment-name';
        nameSpan.textContent = file.name;
        item.appendChild(nameSpan);

        var sizeSpan = document.createElement('span');
        sizeSpan.className = 'attachment-size';
        sizeSpan.textContent = formatFileSize(file.size);
        item.appendChild(sizeSpan);

        var removeBtn = document.createElement('button');
        removeBtn.className = 'attachment-remove';
        removeBtn.setAttribute('aria-label', 'Remove ' + file.name);
        removeBtn.textContent = '\u2715';
        removeBtn.addEventListener('click', function () {
          removeFile(index);
        });
        item.appendChild(removeBtn);

        container.appendChild(item);
      })(i);
    }

    // File count
    var countEl = document.createElement('span');
    countEl.className = 'attachment-count';
    countEl.textContent = state.pendingFiles.length + '/' + CONFIG.maxFiles;
    container.appendChild(countEl);
  }

  // --------------------------------------------------------
  // Notebook Controls
  // --------------------------------------------------------

  function setupNotebookControls() {
    dom.addCell.addEventListener('click', function () {
      createCell('', 'python');
    });

    dom.clearNotebook.addEventListener('click', function () {
      if (state.cells.length === 0) return;
      if (confirm('Clear all notebook cells?')) {
        clearAllCells();
      }
    });
  }

  // --------------------------------------------------------
  // Resizable Divider
  // --------------------------------------------------------

  function setupDivider() {
    var divider = dom.divider;
    var main = dom.main;
    var chatPanel = dom.chatPanel;
    var notebookPanel = dom.notebookPanel;

    var startX = 0;
    var startChatWidth = 0;

    function onMouseDown(e) {
      e.preventDefault();
      state.isDragging = true;
      startX = e.clientX;
      startChatWidth = chatPanel.getBoundingClientRect().width;
      divider.classList.add('dragging');
      document.body.classList.add('no-select');

      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', onMouseUp);
    }

    function onMouseMove(e) {
      if (!state.isDragging) return;

      var dx = e.clientX - startX;
      var totalWidth = main.getBoundingClientRect().width - divider.getBoundingClientRect().width;
      var newChatWidth = startChatWidth + dx;

      var minPanel = 280;
      if (newChatWidth < minPanel || (totalWidth - newChatWidth) < minPanel) return;

      var chatPercent = (newChatWidth / totalWidth) * 100;
      var notebookPercent = 100 - chatPercent;

      chatPanel.style.flex = '0 0 ' + chatPercent + '%';
      notebookPanel.style.flex = '0 0 ' + notebookPercent + '%';
    }

    function onMouseUp() {
      state.isDragging = false;
      divider.classList.remove('dragging');
      document.body.classList.remove('no-select');
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    }

    divider.addEventListener('mousedown', onMouseDown);

    // Touch support
    // Keyboard support for divider
    divider.addEventListener('keydown', function (e) {
      var step = 40;
      var totalWidth = main.getBoundingClientRect().width - divider.getBoundingClientRect().width;
      var chatWidth = chatPanel.getBoundingClientRect().width;
      var minPanel = 280;
      var newChatWidth;

      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        newChatWidth = chatWidth + (e.key === 'ArrowRight' ? step : -step);
        if (newChatWidth < minPanel || (totalWidth - newChatWidth) < minPanel) return;
        var chatPct = (newChatWidth / totalWidth) * 100;
        var notebookPct = 100 - chatPct;
        chatPanel.style.flex = '0 0 ' + chatPct + '%';
        notebookPanel.style.flex = '0 0 ' + notebookPct + '%';
      }
    });

    divider.addEventListener('touchstart', function (e) {
      var touch = e.touches[0];
      onMouseDown({ preventDefault: function () {}, clientX: touch.clientX });

      function onTouchMove(ev) {
        var t = ev.touches[0];
        onMouseMove({ clientX: t.clientX });
      }

      function onTouchEnd() {
        onMouseUp();
        document.removeEventListener('touchmove', onTouchMove);
        document.removeEventListener('touchend', onTouchEnd);
      }

      document.addEventListener('touchmove', onTouchMove);
      document.addEventListener('touchend', onTouchEnd);
    });
  }

  // --------------------------------------------------------
  // Kernel Status Polling
  // --------------------------------------------------------

  async function pollKernelStatus() {
    try {
      var response = await fetch('/api/kernel/status');
      var data = await response.json();

      state.kernelAlive = data.alive;
      state.kernelBusy = data.busy;

      updateKernelStatus(data);
    } catch (_err) {
      state.kernelAlive = false;
      state.kernelBusy = false;
      updateKernelStatus({ alive: false, busy: false });
    }
  }

  function updateKernelStatus(data) {
    var el = dom.kernelStatus;
    var label = el.querySelector('.status-label');

    if (!data.alive) {
      el.className = 'status-badge status-dead';
      label.textContent = 'Kernel: Dead';
    } else if (data.busy) {
      el.className = 'status-badge status-busy';
      label.textContent = 'Kernel: Busy';
    } else {
      el.className = 'status-badge status-alive';
      label.textContent = 'Kernel: Idle';
    }
  }

  function startKernelPolling() {
    pollKernelStatus();
    state.kernelPollTimer = setInterval(pollKernelStatus, CONFIG.kernelPollInterval);
  }

  // --------------------------------------------------------
  // Initialization
  // --------------------------------------------------------

  function init() {
    cacheDom();
    configureMarked();
    setupChatInput();
    setupFileUpload();
    setupNotebookControls();
    setupDivider();
    connectWebSocket();
    startKernelPolling();
  }

  // Run when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
