-- Довідники.
-- Гроші — у копійках (INTEGER), кількість — у тисячних частках одиниці (INTEGER).
-- Записи, на які вже є посилання, не видаляються, а позначаються неактивними.

-- Служби забезпечення (ТЗО, зв'язку, речова, ...): чия номенклатура майна.
CREATE TABLE services (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    short_name  TEXT,
    note        TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1
);

-- Організаційна структура: військова частина, служби, підрозділи, склади.
-- is_accounting = 1 — місце обліку майна (своя книга обліку, своє МВО).
CREATE TABLE units (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    short_name     TEXT,                    -- умовне найменування
    kind           TEXT NOT NULL CHECK (kind IN
                       ('military_unit', 'service', 'subunit', 'warehouse', 'other')),
    parent_id      INTEGER REFERENCES units(id),
    is_accounting  INTEGER NOT NULL DEFAULT 0,
    mvo_person_id  INTEGER REFERENCES persons(id),
    edrpou         TEXT,
    note           TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    CHECK (parent_id IS NULL OR parent_id <> id)
);

-- Військовослужбовці та працівники.
CREATE TABLE persons (
    id           INTEGER PRIMARY KEY,
    last_name    TEXT NOT NULL,
    first_name   TEXT NOT NULL,
    middle_name  TEXT,
    rank         TEXT,
    position     TEXT,
    unit_id      INTEGER REFERENCES units(id),
    note         TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1
);

-- Ролі осіб у документах.
CREATE TABLE person_roles (
    person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN
                   ('mvo', 'recipient', 'commission_member', 'commission_head',
                    'approver', 'service_head', 'finance')),
    PRIMARY KEY (person_id, role)
);

-- Зовнішні сторони: джерела надходження й одержувачі за межами частини.
CREATE TABLE counterparties (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN
                   ('higher_unit', 'military_unit', 'supplier', 'charity',
                    'international', 'authority', 'other')),
    edrpou     TEXT,
    address    TEXT,
    note       TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1
);

-- Номенклатура (найменування військового майна).
CREATE TABLE nomenclature (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    code              TEXT,                 -- код номенклатури (номенклатурний номер)
    nato_code         TEXT,                 -- номенклатурний номер НАТО
    uom               TEXT NOT NULL,        -- одиниця виміру (ДК 011-96, скорочено)
    accounting_class  TEXT NOT NULL CHECK (accounting_class IN
                          ('fixed', 'low_value', 'inventory', 'mshp', 'off_balance')),
    account           TEXT,                 -- субрахунок (для бухгалтерії, необов'язково)
    service_id        INTEGER REFERENCES services(id),
    is_categorized    INTEGER NOT NULL DEFAULT 1,  -- облік за категоріями I–V
    serial_tracked    INTEGER NOT NULL DEFAULT 0,  -- поштучний облік за номерами
    note              TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_nomenclature_code ON nomenclature(code);

-- Поштучні одиниці (зразки з заводськими / інвентарними номерами).
-- Місце і категорія одиниці не зберігаються тут — вони визначаються журналом руху.
CREATE TABLE items (
    id                INTEGER PRIMARY KEY,
    nomenclature_id   INTEGER NOT NULL REFERENCES nomenclature(id),
    serial_no         TEXT,                 -- заводський номер
    inventory_no      TEXT,                 -- інвентарний номер
    passport_no       TEXT,                 -- номер паспорта (формуляра)
    manufacturer      TEXT,
    year_made         INTEGER CHECK (year_made IS NULL OR year_made BETWEEN 1900 AND 2100),
    initial_cost_kop  INTEGER CHECK (initial_cost_kop IS NULL OR initial_cost_kop >= 0),
    note              TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX ux_items_inventory_no ON items(inventory_no)
    WHERE inventory_no IS NOT NULL AND inventory_no <> '';
CREATE INDEX ix_items_nomenclature ON items(nomenclature_id);

-- Склад (комплектність) одиниці: складові частини системи / комплекту.
CREATE TABLE item_components (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    line_no    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    qty_m      INTEGER NOT NULL CHECK (qty_m > 0),
    uom        TEXT,
    serial_no  TEXT,
    note       TEXT
);
CREATE INDEX ix_item_components_item ON item_components(item_id);

-- Місця, між якими рухається майно. Створюються програмою автоматично:
-- для підрозділу (місця обліку), особи, контрагента, а також службові.
CREATE TABLE locations (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('unit', 'person', 'counterparty', 'system')),
    unit_id          INTEGER UNIQUE REFERENCES units(id),
    person_id        INTEGER UNIQUE REFERENCES persons(id),
    counterparty_id  INTEGER UNIQUE REFERENCES counterparties(id),
    system_code      TEXT UNIQUE,
    system_name      TEXT,
    CHECK ((kind = 'unit')         = (unit_id IS NOT NULL)),
    CHECK ((kind = 'person')       = (person_id IS NOT NULL)),
    CHECK ((kind = 'counterparty') = (counterparty_id IS NOT NULL)),
    CHECK ((kind = 'system')       = (system_code IS NOT NULL))
);
INSERT INTO locations(kind, system_code, system_name) VALUES
    ('system', 'opening', 'Початкові залишки'),
    ('system', 'written_off', 'Списано');

-- Журнал змін: хто, коли і що змінив. Записи не редагуються і не видаляються.
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    operator   TEXT,
    action     TEXT NOT NULL,     -- create / update / delete / ...
    entity     TEXT NOT NULL,     -- назва довідника або документа
    entity_id  INTEGER,
    summary    TEXT,
    details    TEXT               -- JSON: [{"field", "label", "old", "new"}, ...]
);
CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Журнал змін не редагується');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Записи журналу змін не видаляються');
END;
