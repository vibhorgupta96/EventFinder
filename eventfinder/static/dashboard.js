const form = document.querySelector('#filter-form');
const results = document.querySelector('#filter-results');
const container = results.querySelector('.cards');

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
}
function eventCard(event) {
  const link = event.registration_url || event.official_url;
  const date = event.starts_at_ist || 'Time not stated';
  const badges = [event.format.replace('_', ' '), event.price_status === 'not_stated' ? 'Price not stated' : event.price_text || 'Free', ...(event.approval_required ? ['Approval required'] : []), ...(event.topics || [])].map(value => `<span>${escapeHtml(value)}</span>`).join('');
  return `<article class="card"><div class="card-top"><p class="type">${escapeHtml(event.event_type)}</p><p class="state">${escapeHtml(event.registration_state)}</p></div><h3><a href="${escapeHtml(event.official_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(event.title)}</a></h3><p class="organizer">${escapeHtml(event.organizer || '')}</p><p class="when">${escapeHtml(date)}</p><p>${escapeHtml(event.venue || event.city || (event.format === 'online' ? 'Online' : 'Venue not stated'))}</p><p class="summary">${escapeHtml(event.summary || '')}</p><div class="badges">${badges}</div><a class="registration" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">View registration ↗</a></article>`;
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  const params = new URLSearchParams(new FormData(form));
  [...params.keys()].forEach(key => { if (!params.get(key)) params.delete(key); });
  results.hidden = false;
  try {
    const response = await fetch(`/api/events?${params}`);
    if (!response.ok) throw new Error('Could not load matching events. Check the filters and try again.');
    const payload = await response.json();
    container.innerHTML = payload.events.length ? payload.events.map(eventCard).join('') : '<p class="empty">No matching events.</p>';
  } catch {
    container.innerHTML = '<p class="empty" role="alert">Could not load matching events. Check the filters and try again.</p>';
  }
  results.scrollIntoView({ behavior: 'smooth', block: 'start' });
});
form.addEventListener('reset', () => { results.hidden = true; container.innerHTML = ''; });
