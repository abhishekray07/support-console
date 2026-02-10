# File Upload in Chat Interface — Design

## Goal

Allow users to attach images and text/code files in the chat interface, passing them as context to Claude via the Anthropic Messages API.

## Supported File Types

- **Images**: png, jpg, gif, webp
- **Text/code**: txt, log, csv, json, xml, yaml, py, js, ts, html, css, md, sh, sql, toml, ini, cfg, conf

Note: `.env` files are excluded — too easy to accidentally upload secrets.

## Limits

- Max file size: 10MB per file
- Max files per message: 5
- Max text file content sent to Claude: 500KB (truncated with notice)
- File store global memory cap: 200MB
- File TTL: 1 hour (safety net — files are evicted after consumption)

---

## Architecture

### 1. Upload Endpoint (Backend)

New HTTP POST endpoint in `server.py`:

```
POST /api/upload
Content-Type: multipart/form-data
Headers: X-Requested-With: XMLHttpRequest (required — CSRF protection)
Body: files[] (one or more files)

Response 200:
{
  "files": [
    { "id": "abc123", "name": "screenshot.png", "type": "image/png", "size": 1234 }
  ]
}

Response 413: { "detail": "File exceeds 10MB limit" }
Response 415: { "detail": "Unsupported file type" }
Response 400: { "detail": "Too many files (max 5)" }
Response 429: { "detail": "Upload rate limit exceeded" }
```

Processing is **all-or-nothing**: if any file in the batch fails validation, the entire request is rejected with details about which file failed.

**Storage**: In-memory dict keyed by file ID, with 200MB global cap.

```python
@dataclass
class FileEntry:
    id: str           # uuid4() or secrets.token_urlsafe(16)
    name: str         # sanitized basename only
    media_type: str   # validated against magic bytes
    data: bytes       # raw file bytes
    created_at: float # time.monotonic()
    size: int         # len(data)
```

File store lives on `AppState`. Files are evicted:
1. **After consumption** — once file IDs are resolved in a WebSocket send, they are deleted from the store. This is the primary cleanup mechanism.
2. **By background task** — asyncio task runs every 60s, purges entries older than 1 hour. This is the safety net for files that are uploaded but never sent.
3. **On memory cap** — uploads rejected when `total_bytes >= 200MB`.

Note: FastAPI's `UploadFile` uses `SpooledTemporaryFile` which may spill to disk after 1MB. This is acceptable (ephemeral temp files managed by the OS).

**Rate limiting**: Dedicated rate limiter for this endpoint — max 10 uploads/minute per IP, separate from the WebSocket rate limiter.

### 2. File Validation (Server-Side)

Validation runs on every uploaded file before storing:

1. **Filename sanitization**: extract basename only, strip path separators (`/`, `\`), null bytes, control characters. Truncate to 255 chars.
2. **Extension check**: must be in the allowlist.
3. **Magic byte validation**: use `python-magic` (libmagic) to detect actual file type. If detected type disagrees with extension, **reject the file**.
4. **Size check**: read and measure actual bytes (don't trust Content-Length).
5. **Text file validation**: attempt UTF-8 decode, strip null bytes. Reject if not valid text.
6. **Image validation**: optionally run `PIL.Image.verify()` to catch malformed/polyglot images (v2 — not required for initial implementation).

### 3. WebSocket Message Format Change

Current:
```json
{ "message": "text", "messages": [...] }
```

New:
```json
{ "message": "text", "file_ids": ["abc123", "def456"], "messages": [...] }
```

`file_ids` is optional. When absent, behavior is unchanged (backward-compatible).

**File ID resolution happens in `server.py`** (the WebSocket handler), NOT in `chat.py`. The handler resolves all file IDs to `FileEntry` objects before calling `chat_stream()`. If any ID is missing or expired, an error event is sent immediately and the message is not processed:

```python
# In server.py WebSocket handler:
file_ids = data.get("file_ids", [])
resolved_files = []
for fid in file_ids:
    entry = app_state.file_store.get(fid)
    if entry is None:
        await ws.send_json({
            "type": "error",
            "error": "Attached files have expired. Please re-attach and resend."
        })
        break
else:
    # All resolved — evict from store and pass to chat engine
    for fid in file_ids:
        resolved_files.append(app_state.file_store.pop(fid))
    async for event in engine.chat_stream(messages, files=resolved_files):
        await ws.send_json(event)
```

### 4. Chat Engine Changes (`chat.py`)

`chat_stream()` gains an optional `files` parameter:

```python
async def chat_stream(
    self, messages: list[dict], files: list[FileEntry] | None = None
) -> AsyncGenerator[dict, None]:
```

When files are present, the user message content block becomes a multi-part array:

```python
{"role": "user", "content": [
    # Images — as base64 image blocks
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
    # Text files — in structured delimiters (not code fences)
    {"type": "text", "text": "<attached-file name=\"app.log\">\n<content>\n</attached-file>"},
    # User message last
    {"type": "text", "text": "User's actual message text"},
]}
```

Key details:
- **Images**: base64 image content blocks. Claude sees them visually.
- **Text files**: wrapped in `<attached-file>` XML tags (not code fences) to mitigate prompt injection. The system prompt is updated to instruct Claude to treat content within these tags as raw data, not instructions.
- **Text truncation**: files larger than 500KB are truncated using the existing `_truncate()` helper, with a note appended.
- **User message comes last** so Claude treats it as the primary question.
- **History** includes file content so follow-up turns retain full context. This means base64 images persist in the messages array. Acceptable for short support sessions; for v2, consider trimming file content from older turns.

### 5. Frontend UI (`app.js`, `index.html`, `style.css`)

**Layout**: `[paperclip button] [textarea] [send button]`

The paperclip button is to the **left of the textarea**, following the convention of Slack/Discord/etc. where attachment triggers are on the left and send is on the right.

**Paperclip button**:
- `<button id="attach-btn" aria-label="Attach files" title="Attach files">` with an SVG icon.
- Clicking opens a hidden `<input type="file" multiple accept="...">`.
- Disabled while `state.isStreaming` or `state.isUploading`.

**Drag-and-drop**:
- Drop zone is the **entire `#chat-panel`** (not just the input area).
- Use a drag enter/leave counter to handle nested element events correctly.
- Full-panel overlay on drag-over: semi-transparent with `--accent-blue`, dashed border, "Drop files here" text, `pointer-events: none`.
- Client-side filtering: reject invalid types immediately with an error message.

**Clipboard paste**:
- `paste` event listener on the textarea.
- Check `event.clipboardData.items` for `type.startsWith('image/')`.
- Only `preventDefault()` if an image was captured; plain text paste continues normally.
- Generated filename: `clipboard-image-{timestamp}.png`.

**Attachment preview strip**:
- `<div id="attachment-preview" class="attachment-preview hidden">` between `#chat-streaming` and `#chat-input-area`.
- `overflow-x: auto` for horizontal scrolling.
- Images: 48px thumbnail via `URL.createObjectURL(file)` with `object-fit: cover`. Call `revokeObjectURL()` on removal.
- Text files: file icon + filename (truncated with ellipsis) + formatted size.
- Each item: `<button aria-label="Remove {filename}">` X button, keyboard-accessible.
- File count indicator: "3/5 files" visible near the strip.
- `role="list"` on container, `role="listitem"` on each item.

**Client-side validation** (on file select, before upload):
- File size > 10MB → inline error in preview strip area.
- File count > 5 → inline error.
- Invalid file type → inline error naming the rejected file.
- These are pre-send errors, shown in the preview area, not in the chat log.

**State additions**:
```javascript
state.pendingFiles = [];  // Array of File objects
state.isUploading = false;
```

**Send flow**:
1. User clicks Send (or Enter).
2. If `pendingFiles.length > 0`: set `isUploading = true`, disable all inputs, show "Uploading files..." status.
3. Build `FormData`, POST to `/api/upload` with `X-Requested-With: XMLHttpRequest` header.
4. On 200: extract `file_ids`, send WebSocket message `{ message, file_ids, messages }`, clear preview strip and `pendingFiles`.
5. On error (4xx, 5xx, network): show error via `appendSystemMessage(text, 'error')`, do NOT clear files (user can retry), re-enable inputs.
6. On WS disconnect after upload succeeds: show error, keep state for retry after reconnect.
7. If no pending files: send WebSocket message directly (existing behavior).

**Chat history display**:
- User messages with attachments show small chips below the message text.
- Images: 32px thumbnail in the chat bubble.
- Text files: chip like `[app.log 2.3 KB]`.
- All filenames rendered with `textContent` (never `innerHTML`).

**Accessibility**:
- Paperclip button: `aria-label="Attach files"`.
- Preview strip: `role="list"`, items `role="listitem"` with `aria-label="{filename}, {size}, {type}"`.
- Remove buttons: `aria-label="Remove {filename}"`.
- `aria-live="polite"` announcements when files are added/removed: "2 files attached", "File removed, 1 file remaining".
- Upload progress: `aria-live="assertive"` for "Uploading files..." status.
- All new interactive elements in natural tab order.

**Config additions**:
```javascript
CONFIG.maxFileSize = 10 * 1024 * 1024;
CONFIG.maxFiles = 5;
CONFIG.allowedImageTypes = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
CONFIG.allowedTextExtensions = ['.txt', '.log', '.csv', '.json', '.xml', '.yaml', ...];
```

New code goes inside the existing IIFE. New `setupFileUpload()` function called from `init()`. New DOM refs cached in `cacheDom()`.

### 6. What Doesn't Change

- Tool definitions (read_file, grep, glob_search) — unchanged
- Session persistence format — messages array already supports content arrays (note: session DB size will grow with image uploads)
- Notebook/kernel — no changes
- Streaming response format — no changes

---

## Key Files to Modify

| File | Changes |
|------|---------|
| `server.py` | New `/api/upload` endpoint, `FileEntry` dataclass, file store on `AppState`, background cleanup task, file ID resolution in WS handler, CSRF header check, upload rate limiter |
| `chat.py` | Accept `files` param in `chat_stream()`, build multi-part content blocks, update system prompt with `<attached-file>` handling instruction |
| `app.js` | `setupFileUpload()`, paperclip button, drag-drop, paste, preview strip, upload-then-send flow, attachment chips in history, new state/config |
| `index.html` | Paperclip button, hidden file input, preview strip container |
| `style.css` | Attachment preview strip, drop zone overlay, file chips, paperclip button |

## Security Measures

1. **File type validation**: extension allowlist + magic byte detection (`python-magic`). Reject on mismatch.
2. **File size**: enforced server-side by reading actual bytes.
3. **File IDs**: `uuid.uuid4()` (cryptographically random, matching existing `sessions.py` pattern).
4. **Memory cap**: 200MB global limit on file store. Reject uploads when exceeded.
5. **TTL + eviction**: files evicted after consumption; background task purges stragglers every 60s.
6. **Filename sanitization**: server strips path separators, null bytes, control chars; client uses `textContent`.
7. **CSRF protection**: upload endpoint requires `X-Requested-With: XMLHttpRequest` header.
8. **Rate limiting**: dedicated limiter for uploads — 10/min per IP.
9. **Prompt injection mitigation**: text files wrapped in `<attached-file>` XML tags; system prompt instructs Claude to treat as raw data.
10. **No file serving**: file store is write-once/read-internal-only. No GET endpoint serves raw bytes. If preview/download is ever added, must include `X-Content-Type-Options: nosniff` and `Content-Disposition: attachment`.
11. **Text file validation**: UTF-8 decode check, null byte stripping, 500KB truncation before sending to Claude.

## v2 Considerations (Out of Scope)

- Trim file content from history after N turns to reduce payload size
- Abort/cancel for slow uploads (AbortController)
- Expandable image thumbnails in chat history
- Per-file error reporting in batch uploads (currently all-or-nothing)
- `PIL.Image.verify()` as secondary image validation
- PDF support
- Determinate upload progress bar (requires XMLHttpRequest instead of fetch)
