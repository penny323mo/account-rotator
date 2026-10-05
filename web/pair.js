// Pairing: the Mac's QR code opens /agy/pair#CODE (the fragment never reaches a server log); a code can also be typed.
const status = document.getElementById('pairStatus');
const messages = {PAIRING_CODE_INVALID: '配對碼唔啱、已經過期或者用咗。請喺 Mac 再撳一次「配對手機」。',
                  TOO_MANY_ATTEMPTS: '試得太多次，請一分鐘後再試。',
                  REMOTE_OFF: '呢部 Mac 未開手機遙控。'};
async function pair(code) {
  status.textContent = '配對緊…';
  try {
    const res = await fetch('/agy/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                              body: JSON.stringify({code})});
    const data = await res.json().catch(() => ({}));
    if (res.ok) { status.textContent = '配對成功，打開緊控制台…'; location.replace('/agy/'); return; }
    status.textContent = messages[data.error] || `配對唔成功（${data.error || res.status}）。`;
  } catch {
    status.textContent = '連唔到部 Mac。請確認手機同 Mac 喺同一個網絡（或者都開咗 Tailscale）。';
  }
}
document.getElementById('pairForm').addEventListener('submit', e => {
  e.preventDefault();
  const code = document.getElementById('pairCode').value.trim();
  if (code) pair(code);
});
const fromLink = location.hash.slice(1) || new URLSearchParams(location.search).get('c');
if (fromLink) {
  history.replaceState(null, '', location.pathname);  // the code must not stay in history or a bookmark
  pair(fromLink);
}
