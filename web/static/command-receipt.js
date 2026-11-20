(function () {
  const panel = document.getElementById('command-receipt');
  if (!panel) return;
  let generation = 0;
  const tone = {
    sending: 'text-sky-300',
    waiting: 'text-amber-300',
    confirmed: 'text-emerald-300',
    unconfirmed: 'text-amber-300'
  };

  function show(state) {
    if (panel.dataset.state === state) return;
    panel.classList.remove(...Object.values(tone));
    panel.classList.toggle('hidden', !state);
    panel.dataset.state = state;
    panel.textContent = state ? panel.dataset[state] : '';
    if (state) panel.classList.add(tone[state]);
  }

  window.mateCommandReceipt = {
    begin() {
      generation++;
      show('sending');
    },
    read(xhr) {
      const token = xhr.getResponseHeader('X-Mate-Command-Receipt');
      const request = ++generation;
      if (!token) {
        show('');
        return false;
      }
      show('waiting');
      const deadline = Date.now() + 35000;
      async function poll() {
        if (request !== generation) return;
        try {
          const response = await fetch('api/command-receipt/' + encodeURIComponent(token), {
            cache: 'no-store',
            signal: AbortSignal.timeout(4000)
          });
          const state = response.ok ? (await response.json()).status : 'unconfirmed';
          if (request !== generation) return;
          if (state === 'confirmed' || state === 'unconfirmed') {
            show(state);
            return;
          }
        } catch (error) {
          if (request !== generation) return;
        }
        if (Date.now() >= deadline) show('unconfirmed');
        else setTimeout(poll, 1500);
      }
      poll();
      return true;
    }
  };
})();
