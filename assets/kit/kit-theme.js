/* popovantondev kit: sets data-theme before the first paint (put it in <head>, not deferred). */
(function () {
  var root = document.documentElement, theme = null;
  try { theme = localStorage.getItem('ppk-theme'); } catch (e) {}
  if (theme !== 'light' && theme !== 'dark') {
    theme = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  root.setAttribute('data-theme', theme);
})();
