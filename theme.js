'use strict';

(function initializeTheme() {
  const STORAGE_KEY = 'axis_manager_theme';
  const THEMES = Object.freeze(['shadcn-dark', 'shadcn-light', 'matrix', 'aurora', 'obsidian']);
  const THEME_COLORS = Object.freeze({ 'shadcn-dark': '#0a0a0a', 'shadcn-light': '#ffffff', matrix: '#090c0d', aurora: '#080b16', obsidian: '#0e0d0b' });

  function normalizedTheme(value) { return THEMES.includes(value) ? value : 'shadcn-dark'; }
  function readStoredTheme() {
    try { return localStorage.getItem(STORAGE_KEY); }
    catch (error) { console.warn('Unable to read the saved display style.', error); return null; }
  }
  function writeStoredTheme(value) {
    try { localStorage.setItem(STORAGE_KEY, value); }
    catch (error) { console.warn('Unable to save the display style.', error); }
  }
  let theme = normalizedTheme(readStoredTheme());

  function syncOptions() {
    document.querySelectorAll('[data-theme-option]').forEach((option) => {
      const selected = option.dataset.themeOption === theme;
      option.classList.toggle('selected', selected);
      option.setAttribute('aria-pressed', String(selected));
    });
    document.querySelectorAll('[data-theme-select]').forEach((select) => { select.value = theme; });
  }

  function applyTheme(next, persist = true) {
    theme = normalizedTheme(next);
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[theme]);
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme === 'shadcn-light' ? 'light' : 'dark');
    syncOptions();
    if (persist) writeStoredTheme(theme);
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme } }));
    return theme;
  }

  applyTheme(theme, false);
  document.addEventListener('DOMContentLoaded', () => {
    syncOptions();
    document.querySelectorAll('[data-theme-option]').forEach((option) => option.addEventListener('click', () => applyTheme(option.dataset.themeOption)));
    document.querySelectorAll('[data-theme-select]').forEach((select) => select.addEventListener('change', () => applyTheme(select.value)));
  });

  window.axisTheme = { applyTheme, get theme() { return theme; }, themes: THEMES };
}());
