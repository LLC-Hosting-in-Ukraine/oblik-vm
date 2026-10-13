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

  var CAT = {1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V'};

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
    var text = nom ? ('од. виміру: ' + nom.uom + (nom.serial ? ' · поштучний облік' : '')) : '';
    if (nom && nom.stock && nom.stock.length) {
      text += ' · наявно: ' + nom.stock.map(function (s) {
        return s.qty + ' × ' + s.price + ' грн' + (s.cat ? ', кат. ' + CAT[s.cat] : '');
      }).join('; ');
    } else if (nom && nom.last) {
      text += ' · остання ціна: ' + nom.last.price + ' грн' + (nom.last.cat ? ', кат. ' + CAT[nom.last.cat] : '');
    }
    info.textContent = text;
    // Ціна й категорія — з найбільшого наявного залишку (інакше з останньої ціни); те, що
    // користувач уже вписав, не чіпаємо.
    var hint = nom ? ((nom.stock && nom.stock[0]) || nom.last) : null;
    var price = tr.querySelector('.ln-price');
    var cat = tr.querySelector('select[name$="cat"]');
    if (hint && price && !price.value.trim() && hint.price !== '0,00') { price.value = hint.price; }
    if (hint && hint.cat && cat && !cat.value) { cat.value = String(hint.cat); }
    var qtyInput = tr.querySelector('.ln-qty');
    if (qtyInput) { qtyInput.placeholder = (nom && nom.stock && nom.stock[0]) ? 'до ' + nom.stock[0].qty : ''; }
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

  // Акт якісного стану: сума списаного (рядки «Списати») підставляється як вартість єдиної
  // системи в «Оприбуткувати». Ціну, вписану вручну, не чіпаємо.
  var outTable = document.getElementById('lines');
  var inTable = document.getElementById('in-lines');

  function num(text) {
    var v = parseFloat(String(text).replace(/\s/g, '').replace(',', '.'));
    return isNaN(v) ? 0 : v;
  }

  function fmt(kop) {
    var s = Math.round(kop) / 100;
    return s.toFixed(2).replace('.', ',').replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

  function outTotalKop() {
    var total = 0;
    Array.prototype.forEach.call(outTable.tBodies[0].rows, function (tr) {
      var key = tr.querySelector('.ln-key');
      var qty = num((tr.querySelector('.ln-qty') || {}).value);
      var price = 0;
      if (key && key.value) { price = parseInt(key.value.split('|')[3], 10) || 0; }
      else if (tr.querySelector('.ln-price')) { price = Math.round(num(tr.querySelector('.ln-price').value) * 100); }
      total += qty * price;
    });
    return total;
  }

  function recalcCondition() {
    if (!outTable || !inTable) { return; }
    var total = outTotalKop();
    var label = document.getElementById('out-sum-live');
    if (label) { label.textContent = fmt(total); }
    var rows = Array.prototype.filter.call(inTable.tBodies[0].rows, function (tr) {
      var n = tr.querySelector('.ln-name');
      return n && n.value.trim();
    });
    if (rows.length !== 1) { return; }
    var price = rows[0].querySelector('.ln-price');
    var qty = num(rows[0].querySelector('.ln-qty').value) || 1;
    if (price && (!price.value.trim() || price.dataset.auto === '1')) {
      price.value = fmt(total / qty).replace(/ /g, '');
      price.dataset.auto = '1';
      var sumCell = rows[0].querySelector('.ln-sum');
      if (sumCell) { sumCell.textContent = fmt(total); }
    }
  }

  if (outTable && inTable) {
    document.addEventListener('input', function (e) {
      if (e.target.classList && e.target.classList.contains('ln-price') && inTable.contains(e.target)) {
        e.target.dataset.auto = '0';   // вручну — більше не перераховуємо
      }
      recalcCondition();
    });
    document.addEventListener('change', recalcCondition);
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
  recalcCondition();
})();
