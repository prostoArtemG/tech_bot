function setLang(lang) {
  localStorage.setItem('site_lang', lang);
  applyLang();
}

function applyLang() {
  var lang = localStorage.getItem('site_lang') || 'ru';

  // Highlight active button
  document.querySelectorAll('.lang-btn').forEach(function(btn) {
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

document.addEventListener('DOMContentLoaded', applyLang);
