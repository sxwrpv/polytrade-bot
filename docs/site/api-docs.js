(() => {
  const root = document.documentElement;
  const saved = localStorage.getItem('polytrade-docs-theme');
  if (saved) root.dataset.theme = saved;
  else if (matchMedia('(prefers-color-scheme: dark)').matches) root.dataset.theme = 'dark';

  document.getElementById('api-theme-toggle')?.addEventListener('click', () => {
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    localStorage.setItem('polytrade-docs-theme', next);
  });
})();
