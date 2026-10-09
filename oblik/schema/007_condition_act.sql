-- foreign_keys: off
-- Акт якісного (технічного) стану (дод. 1 до Порядку списання військового майна, наказ МОУ № 81):
-- в одному документі списується майно (графи 1–10) і оприбутковується отримане (графи 11–17) —
-- напр., кілька одиниць об'єднують в одну систему. Новий тип документа 'condition' (перебудова
-- таблиці документів — CHECK), напрям рядка (direction = 'in' — оприбуткувати), строки
-- експлуатації (графи 9–10) і службове місце «Оприбутковано за актом».

CREATE TABLE documents_new (
    id                   INTEGER PRIMARY KEY,
    doc_type             TEXT NOT NULL CHECK (doc_type IN
                             ('opening', 'receipt', 'transfer', 'dispatch', 'writeoff', 'storno',
                              'order', 'condition')),
    status               TEXT NOT NULL DEFAULT 'draft'
                             CHECK (status IN ('draft', 'posted', 'cancelled')),
    reg_year             INTEGER NOT NULL,     -- рік реєстрації
    reg_no               INTEGER NOT NULL,     -- реєстраційний номер у межах року
    doc_no               TEXT NOT NULL,        -- номер документа (за замовчуванням = reg_no)
    doc_date             TEXT NOT NULL,        -- дата складання (РРРР-ММ-ДД)
    op_date              TEXT NOT NULL,        -- дата операції
    valid_until          TEXT,                 -- строк дії
    from_location_id     INTEGER NOT NULL REFERENCES locations(id),
    to_location_id       INTEGER NOT NULL REFERENCES locations(id),
    basis                TEXT,                 -- підстава (мета) операції
    service_id           INTEGER REFERENCES services(id),
    recipient_person_id  INTEGER REFERENCES persons(id),  -- відповідальний одержувач
    reversal_of_id       INTEGER UNIQUE REFERENCES documents(id),  -- для сторно: що сторнує
    reversed_by_id       INTEGER UNIQUE REFERENCES documents(id),  -- ким сторновано
    order_id             INTEGER REFERENCES documents(id),         -- за яким нарядом оформлено
    note                 TEXT,
    extra_json           TEXT,                 -- реквізити конкретної форми документа
    created_at           TEXT NOT NULL,
    posted_at            TEXT,
    UNIQUE (reg_year, reg_no),
    CHECK (from_location_id <> to_location_id),
    CHECK ((doc_type = 'storno') = (reversal_of_id IS NOT NULL)),
    CHECK (order_id IS NULL OR doc_type NOT IN ('order', 'storno'))
);

INSERT INTO documents_new(id, doc_type, status, reg_year, reg_no, doc_no, doc_date, op_date, valid_until,
                          from_location_id, to_location_id, basis, service_id, recipient_person_id,
                          reversal_of_id, reversed_by_id, order_id, note, extra_json, created_at, posted_at)
SELECT id, doc_type, status, reg_year, reg_no, doc_no, doc_date, op_date, valid_until,
       from_location_id, to_location_id, basis, service_id, recipient_person_id,
       reversal_of_id, reversed_by_id, order_id, note, extra_json, created_at, posted_at
FROM documents;

-- Тригери інших таблиць (рядки, рух) посилаються на documents: без legacy_alter_table SQLite
-- перевіряє їх під час перейменування, коли старої таблиці вже немає.
PRAGMA legacy_alter_table = ON;
DROP TABLE documents;
ALTER TABLE documents_new RENAME TO documents;
PRAGMA legacy_alter_table = OFF;

CREATE INDEX ix_documents_op_date ON documents(op_date);
CREATE INDEX ix_documents_order ON documents(order_id);

-- Тригери старої таблиці зникли разом з нею — відтворюємо (як у 005). Тригери інших
-- таблиць (рядки, рух, відмітки нарядів) лишаються.
CREATE TRIGGER documents_posted_readonly BEFORE UPDATE ON documents
WHEN OLD.status = 'posted' AND (
       NEW.status IS NOT OLD.status OR NEW.doc_type IS NOT OLD.doc_type
    OR NEW.reg_year IS NOT OLD.reg_year OR NEW.reg_no IS NOT OLD.reg_no
    OR NEW.doc_no IS NOT OLD.doc_no OR NEW.doc_date IS NOT OLD.doc_date
    OR NEW.op_date IS NOT OLD.op_date OR NEW.valid_until IS NOT OLD.valid_until
    OR NEW.from_location_id IS NOT OLD.from_location_id
    OR NEW.to_location_id IS NOT OLD.to_location_id
    OR NEW.basis IS NOT OLD.basis OR NEW.service_id IS NOT OLD.service_id
    OR NEW.recipient_person_id IS NOT OLD.recipient_person_id
    OR NEW.reversal_of_id IS NOT OLD.reversal_of_id
    OR NEW.order_id IS NOT OLD.order_id
    OR NEW.note IS NOT OLD.note OR NEW.extra_json IS NOT OLD.extra_json
    OR NEW.created_at IS NOT OLD.created_at OR NEW.posted_at IS NOT OLD.posted_at
    OR (OLD.reversed_by_id IS NOT NULL AND NEW.reversed_by_id IS NOT OLD.reversed_by_id)
)
BEGIN
    SELECT RAISE(ABORT, 'Проведений документ не можна змінювати. Створіть сторнувальний документ.');
END;

CREATE TRIGGER documents_cancelled_readonly BEFORE UPDATE ON documents
WHEN OLD.status = 'cancelled'
BEGIN
    SELECT RAISE(ABORT, 'Анульований документ не змінюється');
END;

CREATE TRIGGER documents_no_delete BEFORE DELETE ON documents
WHEN OLD.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Проведені й анульовані документи не видаляються');
END;

-- Рядки: 'in' — оприбуткувати (з місця «Оприбутковано за актом» на місце документа); NULL — як
-- у документі. Графи 9–10 акта: експлуатується років за нормою / фактично (текст: «10», «5,5»).
ALTER TABLE document_lines ADD COLUMN direction TEXT CHECK (direction IS NULL OR direction = 'in');
ALTER TABLE document_lines ADD COLUMN years_norm TEXT;
ALTER TABLE document_lines ADD COLUMN years_fact TEXT;

INSERT INTO locations(kind, system_code, system_name) VALUES ('system', 'obtained', 'Оприбутковано за актом');
