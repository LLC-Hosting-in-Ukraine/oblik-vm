-- Документи і журнал руху майна.
-- Майно рухається тільки через документи. Залишки рахуються з журналу руху (movements).
-- Проведений документ не змінюється; помилку виправляє сторнувальний документ.

CREATE TABLE documents (
    id                   INTEGER PRIMARY KEY,
    doc_type             TEXT NOT NULL CHECK (doc_type IN
                             ('opening', 'receipt', 'transfer', 'dispatch', 'writeoff', 'storno')),
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
    note                 TEXT,
    extra_json           TEXT,                 -- реквізити конкретної форми документа
    created_at           TEXT NOT NULL,
    posted_at            TEXT,
    UNIQUE (reg_year, reg_no),
    CHECK (from_location_id <> to_location_id),
    CHECK ((doc_type = 'storno') = (reversal_of_id IS NOT NULL))
);
CREATE INDEX ix_documents_op_date ON documents(op_date);

CREATE TABLE document_lines (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    nomenclature_id  INTEGER NOT NULL REFERENCES nomenclature(id),
    item_id          INTEGER REFERENCES items(id),
    qty_requested_m  INTEGER CHECK (qty_requested_m IS NULL OR qty_requested_m >= 0),  -- відправлено (вимагається)
    qty_m            INTEGER NOT NULL CHECK (qty_m > 0),                               -- прийнято (відпущено)
    price_kop        INTEGER NOT NULL CHECK (price_kop >= 0),
    category         INTEGER CHECK (category IS NULL OR category BETWEEN 1 AND 5),
    batch_no         TEXT,
    note             TEXT
);
CREATE INDEX ix_document_lines_doc ON document_lines(document_id);

CREATE TABLE document_signers (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role         TEXT NOT NULL,       -- approver, commission_head, commission_member, handed, received, ...
    person_id    INTEGER NOT NULL REFERENCES persons(id),
    ord          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_document_signers_doc ON document_signers(document_id);

-- Журнал руху: подвійний запис «звідки → куди».
-- Залишок місця = Σ надходжень − Σ вибуттів у розрізі
-- (номенклатура, поштучна одиниця, категорія, ціна).
CREATE TABLE movements (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id),
    line_id           INTEGER NOT NULL REFERENCES document_lines(id),
    op_date           TEXT NOT NULL,
    from_location_id  INTEGER NOT NULL REFERENCES locations(id),
    to_location_id    INTEGER NOT NULL REFERENCES locations(id),
    nomenclature_id   INTEGER NOT NULL REFERENCES nomenclature(id),
    item_id           INTEGER REFERENCES items(id),
    category          INTEGER,
    price_kop         INTEGER NOT NULL,
    qty_m             INTEGER NOT NULL CHECK (qty_m > 0),
    CHECK (from_location_id <> to_location_id)
);
CREATE INDEX ix_movements_from ON movements(from_location_id, nomenclature_id);
CREATE INDEX ix_movements_to ON movements(to_location_id, nomenclature_id);
CREATE INDEX ix_movements_item ON movements(item_id);
CREATE INDEX ix_movements_doc ON movements(document_id);

-- --- Захист цілісності на рівні бази ------------------------------------------

-- Проведений документ змінювати не можна; дозволено лише відмітку «сторновано».
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

-- Рядки змінюються тільки в чернетці.
CREATE TRIGGER lines_draft_only_insert BEFORE INSERT ON document_lines
WHEN (SELECT status FROM documents WHERE id = NEW.document_id) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Рядки можна змінювати тільки в чернетці');
END;

CREATE TRIGGER lines_draft_only_update BEFORE UPDATE ON document_lines
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Рядки можна змінювати тільки в чернетці');
END;

CREATE TRIGGER lines_draft_only_delete BEFORE DELETE ON document_lines
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Рядки можна змінювати тільки в чернетці');
END;

-- Рух створюється тільки під час проведення (документ ще чернетка в цій транзакції)
-- і ніколи не змінюється й не видаляється.
CREATE TRIGGER movements_insert_on_posting BEFORE INSERT ON movements
WHEN (SELECT status FROM documents WHERE id = NEW.document_id) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'Рух створюється тільки під час проведення документа');
END;

CREATE TRIGGER movements_no_update BEFORE UPDATE ON movements
BEGIN
    SELECT RAISE(ABORT, 'Журнал руху не редагується');
END;

CREATE TRIGGER movements_no_delete BEFORE DELETE ON movements
BEGIN
    SELECT RAISE(ABORT, 'Записи журналу руху не видаляються');
END;

-- Місце обліку підрозділу можна створити лише для підрозділу з позначкою «Місце обліку».
CREATE TRIGGER locations_unit_must_be_accounting BEFORE INSERT ON locations
WHEN NEW.kind = 'unit' AND (SELECT is_accounting FROM units WHERE id = NEW.unit_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'Підрозділ не позначено як місце обліку');
END;
