// WebSocket transport + message dispatch. Preserves the existing protocol:
// client_id identification on the first message, ?token= auth param cached in
// sessionStorage, 25s keepalive ping, and auto-reconnect (§ preserve behavior).
const CLIENT_ID = sessionStorage.getItem('client_id') || crypto.randomUUID();
sessionStorage.setItem('client_id', CLIENT_ID);

let ws = null;
let identifiedThisConnection = false;
const handlers = {};
const openCallbacks = [];
let statusDotFn = null;

// Feature modules register their handlers at import time.
export function onMessage(type, fn) { handlers[type] = fn; }
export function onOpen(fn) { openCallbacks.push(fn); }

export function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    // Attach client_id on the first message so the server can reattach the engine.
    if (!identifiedThisConnection) {
      obj.client_id = CLIENT_ID;
      identifiedThisConnection = true;
    }
    ws.send(JSON.stringify(obj));
  }
}

export function wsOpen() { return !!ws && ws.readyState === WebSocket.OPEN; }

export function connect(onStatusDot) {
  if (onStatusDot) statusDotFn = onStatusDot;
  const urlParams = new URLSearchParams(location.search);
  const urlToken = urlParams.get('token');
  if (urlToken) sessionStorage.setItem('ws_token', urlToken);
  const wsToken = sessionStorage.getItem('ws_token');

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const baseUrl = `${proto}://${location.host}/ws`;
  const url = wsToken ? `${baseUrl}?token=${encodeURIComponent(wsToken)}` : baseUrl;
  ws = new WebSocket(url);

  ws.onopen = () => {
    statusDotFn?.('online');
    identifiedThisConnection = false;
    // First send triggers identification + state restore on the server.
    send({ type: 'ping' });
    clearInterval(window._wsPing);
    window._wsPing = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
    }, 25000);
    for (const cb of openCallbacks) cb();
  };

  ws.onerror = () => statusDotFn?.('offline');

  ws.onclose = () => {
    clearInterval(window._wsPing);
    statusDotFn?.('offline');
    setTimeout(() => connect(), 2000);
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handlers[msg.type]?.(msg);
  };
}
