// Show the effective URL while accepting domains without a protocol.
function normalizeProbeUrl(value) {
  const url = value.trim();
  if (!url) return '';
  if (url.startsWith('//')) return 'https:' + url;
  return /^[a-z][a-z\d+.-]*:\/\//i.test(url) ? url : 'https://' + url;
}

for (const [field, preview] of [
  ['url', 'url'], ['method', 'method'], ['interval_minutes', 'interval'],
  ['timeout_seconds', 'timeout'], ['notification_channel', 'channel'],
]) {
  const input = document.getElementById('id_' + field);
  const output = document.getElementById('preview-' + preview);
  if (!input || !output) continue;
  const updatePreview = () => {
    output.textContent = (field === 'url' ? normalizeProbeUrl(input.value) : input.value) || '—';
  };
  input.addEventListener('input', updatePreview);
  if (field === 'url') {
    input.addEventListener('blur', () => {
      input.value = normalizeProbeUrl(input.value);
      updatePreview();
    });
    updatePreview();
  }
}
