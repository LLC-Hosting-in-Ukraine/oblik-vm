// Підказка «Що це за сторінка» — згорнута/розгорнута, запам'ятовується в браузері.
(function () {
  document.querySelectorAll('details.page-hint').forEach(function (el) {
    var key = 'hint:' + el.dataset.hintId;
    try {
      if (localStorage.getItem(key) === 'closed') { el.open = false; }
    } catch (e) { /* сховище недоступне — просто показуємо */ }
    el.addEventListener('toggle', function () {
      try { localStorage.setItem(key, el.open ? 'open' : 'closed'); } catch (e) {}
    });
  });
})();

// Значок «?»: підказка показується поверх сторінки і завжди в межах вікна —
// навіть у таблиці з прокруткою, яка інакше обрізала б її.
(function () {
  var GAP = 8;
  function place(hint) {
    var body = hint.querySelector('.hint-body');
    if (!body) { return; }
    body.classList.add('hint-fixed');
    var mark = hint.getBoundingClientRect();
    var w = Math.min(320, window.innerWidth - 2 * GAP);
    body.style.width = w + 'px';
    var left = mark.left + mark.width / 2 - w / 2;
    left = Math.max(GAP, Math.min(left, window.innerWidth - w - GAP));
    body.style.left = left + 'px';
    body.style.top = '0px';
    var h = body.offsetHeight;
    var top = mark.bottom + 6;
    if (top + h > window.innerHeight - GAP && mark.top - h - 6 > GAP) { top = mark.top - h - 6; }
    body.style.top = top + 'px';
  }
  document.querySelectorAll('.hint').forEach(function (hint) {
    hint.addEventListener('mouseenter', function () { place(hint); });
    hint.addEventListener('focusin', function () { place(hint); });
  });
  // Прокрутка зсуває значок — сховати відкриту підказку, щоб не «висіла» окремо.
  window.addEventListener('scroll', function () {
    var a = document.activeElement;
    if (a && a.classList && a.classList.contains('hint')) { a.blur(); }
  }, true);
})();
