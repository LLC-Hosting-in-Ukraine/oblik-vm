// Рядки документа: додавання/видалення, нумерація, поля номерів для поштучного майна.
// Таблиць може бути дві (акт якісного стану: «Списати» і «Оприбуткувати») — кожна сама по собі.
(function () {
  var tables = document.querySelectorAll('table[data-lines]');
  if (!tables.length) { return; }
  // Найменування з довідника: шукаємо за підписом («назва [код]») або за самою назвою.
  var byLabel = {};
  (window.NOMS || []).forEach(function (n) {
    byLabel[n.label.trim().toLowerCase()] = n;
    var bare = n.label.replace(/\s*\[[^\]]*\]$/, '').trim().toLowerCase();
    if (!(bare in byLabel)) { byLabel[bare] = n; }
  });

  function findNom(text) {
    return byLabel[text.replace(/\s+/g, ' ').trim().toLowerCase()] || null;
  }

  function renumber(body) {
    Array.prototype.forEach.call(body.rows, function (tr, i) {
      tr.querySelector('.n').textContent = i + 1;
    });
  }

  // Введена назва → id найменування або панель «нове найменування».
  function syncName(tr) {
    var name = tr.querySelector('.ln-name');
    if (!name) { return; }
    var hidden = tr.querySelector('.ln-nom');
    var info = tr.querySelector('.ln-info');
    var panel = tr.querySelector('.ln-new');
    var nom = findNom(name.value);
    hidden.value = nom ? nom.id : '';
    panel.hidden = !!nom || !name.value.trim();
    info.textContent = nom ? ('од. виміру: ' + nom.uom + (nom.serial ? ' · поштучний облік' : '')) : '';
    // У наряді поштучних одиниць немає: кількість довільна, номери — в акті.
    var serial = nom ? nom.serial : tr.querySelector('.ln-new-serial').value === '1';
    syncSerial(tr, serial && !window.IS_ORDER);
  }

  function syncSerial(tr, serial) {
    tr.querySelectorAll('.ln-serial').forEach(function (inp) {
      inp.readOnly = !serial;
      inp.placeholder = serial ? '' : '—';
      if (!serial) { inp.value = ''; }
    });
    var qty = tr.querySelector('.ln-qty');
    if (serial) { qty.value = '1'; qty.readOnly = true; } else { qty.readOnly = false; }
  }

  function bind(tr, body) {
    var name = tr.querySelector('.ln-name');
    if (name) {
      name.addEventListener('input', function () { syncName(tr); });
      tr.querySelector('.ln-new-serial').addEventListener('change', function () { syncName(tr); });
      syncName(tr);
    }
    var key = tr.querySelector('.ln-key');
    if (key) {
      key.addEventListener('change', function () {
        // Поштучна одиниця: у ключі заповнена друга частина (id одиниці).
        var parts = key.value.split('|');
        var qty = tr.querySelector('.ln-qty');
        if (parts[1]) { qty.value = '1'; qty.readOnly = true; } else { qty.readOnly = false; }
      });
      key.dispatchEvent(new Event('change'));
    }
    tr.querySelector('[data-remove]').addEventListener('click', function () {
      if (body.rows.length > 1) { tr.remove(); } else {
        tr.querySelectorAll('input').forEach(function (i) { i.value = ''; });
        tr.querySelectorAll('select').forEach(function (s) { s.selectedIndex = 0; });
      }
      renumber(body);
    });
  }

  Array.prototype.forEach.call(tables, function (table) {
   var body = table.tBodies[0];
   document.querySelector('[data-add-line="' + table.id + '"]').addEventListener('click', function () {
    var last = body.rows[body.rows.length - 1];
    var row = last.cloneNode(true);
    row.querySelectorAll('input').forEach(function (i) { i.value = ''; i.readOnly = false; });
    row.querySelectorAll('select').forEach(function (s) { s.selectedIndex = 0; });
    row.querySelector('.ln-sum').textContent = '';
    var uom = row.querySelector('.ln-new-uom');
    if (uom) { uom.value = 'шт.'; }
    var cls = row.querySelector('.ln-new-class');
    if (cls) { cls.value = 'inventory'; }
    body.appendChild(row);
    bind(row, body);
    renumber(body);
    row.querySelector('select, input').focus();
   });

   Array.prototype.forEach.call(body.rows, function (tr) { bind(tr, body); });
   renumber(body);
  });
})();
