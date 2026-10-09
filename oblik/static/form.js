// Рядки таблиці складових (комплектності) у формі поштучної одиниці.
function renumber() {
  document.querySelectorAll('#comps tbody tr').forEach(function (tr, i) {
    tr.querySelector('.n').textContent = i + 1;
  });
}

function addRow() {
  var body = document.querySelector('#comps tbody');
  var last = body.querySelector('tr:last-child');
  var row = last.cloneNode(true);
  row.querySelectorAll('input').forEach(function (inp) { inp.value = ''; });
  body.appendChild(row);
  renumber();
  row.querySelector('input').focus();
}

function removeRow(btn) {
  var body = document.querySelector('#comps tbody');
  var tr = btn.closest('tr');
  if (body.rows.length > 1) {
    tr.remove();
  } else {
    tr.querySelectorAll('input').forEach(function (inp) { inp.value = ''; });
  }
  renumber();
}

if (document.querySelector('#comps')) { renumber(); }
