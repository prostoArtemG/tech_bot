function setLang(lang) {
  localStorage.setItem('site_lang', lang);
  applyLang();
}

function applyLang() {
  var lang = localStorage.getItem('site_lang') || 'uk';

  // Highlight active button
  document.querySelectorAll('.site-lang-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.lang === lang);
  });

  // Translate textContent
  document.querySelectorAll('[data-ru][data-uk]').forEach(function(el) {
    el.textContent = lang === 'uk' ? el.dataset.uk : el.dataset.ru;
  });

  // Translate placeholders
  document.querySelectorAll('[data-placeholder-ru][data-placeholder-uk]').forEach(function(el) {
    el.placeholder = lang === 'uk' ? el.dataset.placeholderUk : el.dataset.placeholderRu;
  });
}

// Call immediately (DOM is already ready when script is at end of page)
applyLang();
// Also listen in case script is moved to <head>
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', applyLang);
}
