/* A progressively enhanced drawer: keyboard focus stays inside while it is open. */
(() => {
  const toggle = document.querySelector('.menu-toggle');
  const drawer = document.getElementById('site-navigation');
  const close = document.querySelector('.menu-close');
  const backdrop = document.querySelector('.navigation-backdrop');
  const content = document.querySelector('.shell');
  const header = document.querySelector('.mobile-header');
  if (!toggle || !drawer || !close || !backdrop || !content) return;

  const root = document.documentElement;
  const mobile = window.matchMedia('(max-width: 960px)');
  let opened = false;
  let previousFocus = null;
  root.classList.add('navigation-enhanced');
  toggle.hidden = false;
  close.hidden = false;

  function update(restoreFocus = false) {
    const active = mobile.matches && opened;
    root.classList.toggle('navigation-open', active);
    toggle.setAttribute('aria-expanded', String(active));
    backdrop.hidden = !active;
    drawer.inert = mobile.matches && !active;
    content.inert = active;
    if (header) header.inert = active;
    if (active) {
      drawer.setAttribute('role', 'dialog');
      drawer.setAttribute('aria-modal', 'true');
    } else {
      drawer.removeAttribute('role');
      drawer.removeAttribute('aria-modal');
    }
    if (drawer.inert) drawer.setAttribute('aria-hidden', 'true');
    else drawer.removeAttribute('aria-hidden');
    if (restoreFocus && mobile.matches) (previousFocus || toggle).focus();
  }

  function dismiss(restoreFocus = true) {
    opened = false;
    update(restoreFocus);
  }

  toggle.addEventListener('click', () => {
    if (opened) return dismiss();
    previousFocus = document.activeElement;
    opened = true;
    update();
    close.focus();
  });
  close.addEventListener('click', () => dismiss());
  backdrop.addEventListener('click', () => dismiss());
  drawer.addEventListener('click', (event) => {
    if (mobile.matches && event.target.closest('a[href]')) dismiss(false);
  });
  document.addEventListener('keydown', (event) => {
    if (!mobile.matches || !opened) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      return dismiss();
    }
    if (event.key !== 'Tab') return;
    const controls = [...drawer.querySelectorAll(
      'a[href], button:not([disabled]), input:not([type=hidden]), select, textarea, [tabindex="0"]'
    )].filter((element) => element.getClientRects().length > 0);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  mobile.addEventListener('change', () => {
    const closeWasFocused = document.activeElement === close;
    opened = false;
    update();
    if (!mobile.matches && closeWasFocused) drawer.querySelector('nav a').focus();
  });
  update();
})();
