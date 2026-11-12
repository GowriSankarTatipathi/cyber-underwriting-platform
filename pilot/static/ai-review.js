(() => {
  const status = document.querySelector('[data-ai-status-url]');
  if (!status) return;
  let remaining = 20;
  const poll = async () => {
    try {
      const response = await fetch(status.dataset.aiStatusUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) return;
      const body = await response.json();
      if (body.status !== status.dataset.aiStatus || body.stalled) {
        window.location.reload();
        return;
      }
      if (--remaining > 0) window.setTimeout(poll, 3000);
    } catch (_) {
      status.textContent = 'Status checking paused. Refresh this page or continue manual review.';
    }
  };
  window.setTimeout(poll, 3000);
})();
