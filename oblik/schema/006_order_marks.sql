-- Ручні відмітки про виконання наряду (графи 8–9 дод. 5): коли майно видає не ваша частина
-- (наряд між іншими частинами) і вам повертають виконаний примірник (пп. 30, 51 розд. IV
-- Інструкції), або документ про видачу оформлено поза програмою. Документи, оформлені
-- в програмі за нарядом, рахуються самі (documents.order_id) — для них відмітка не потрібна.

CREATE TABLE order_marks (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES documents(id),
    line_id     INTEGER NOT NULL REFERENCES document_lines(id),
    qty_m       INTEGER NOT NULL CHECK (qty_m > 0),   -- фактично видано (прийнято), тисячні
    doc_ref     TEXT NOT NULL,                        -- назва, номер і дата документа видачі
    mark_date   TEXT NOT NULL,                        -- коли зроблено відмітку (РРРР-ММ-ДД)
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_order_marks_order ON order_marks(order_id);

-- Відмітка — лише до підписаного (проведеного) наряду і до його ж рядка.
CREATE TRIGGER order_marks_for_posted_orders BEFORE INSERT ON order_marks
WHEN (SELECT doc_type || '/' || status FROM documents WHERE id = NEW.order_id) IS NOT 'order/posted'
  OR (SELECT document_id FROM document_lines WHERE id = NEW.line_id) IS NOT NEW.order_id
BEGIN
    SELECT RAISE(ABORT, 'Відмітку про виконання можна зробити лише до рядка підписаного наряду');
END;

-- Відмітку не правлять: помилкову видаляють (із записом у журналі змін) і вносять заново.
CREATE TRIGGER order_marks_no_update BEFORE UPDATE ON order_marks
BEGIN
    SELECT RAISE(ABORT, 'Відмітку про виконання не змінюють — видаліть помилкову і внесіть нову');
END;
