-- foreign_keys: off
-- Видане в користування рахується за тим місцем обліку, яке його видало
-- (п. 11, 16 розд. V Інструкції: видане військовослужбовцям обліковується в підрозділі,
-- з обліку підрозділу не знімається). Тому для особи може бути кілька місць руху —
-- по одному на кожне місце обліку, що їй видавало: locations.unit_id для kind = 'person'.

CREATE TABLE locations_new (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('unit', 'person', 'counterparty', 'system')),
    -- 'unit' — сам підрозділ (місце обліку);
    -- 'person' — місце обліку, за яким рахується видане особі (може бути невідоме — NULL).
    unit_id          INTEGER REFERENCES units(id),
    person_id        INTEGER REFERENCES persons(id),
    counterparty_id  INTEGER UNIQUE REFERENCES counterparties(id),
    system_code      TEXT UNIQUE,
    system_name      TEXT,
    CHECK (kind <> 'unit' OR (unit_id IS NOT NULL AND person_id IS NULL)),
    CHECK ((kind = 'person') = (person_id IS NOT NULL)),
    CHECK (kind IN ('unit', 'person') OR unit_id IS NULL),
    CHECK ((kind = 'counterparty') = (counterparty_id IS NOT NULL)),
    CHECK ((kind = 'system') = (system_code IS NOT NULL))
);

INSERT INTO locations_new(id, kind, unit_id, person_id, counterparty_id, system_code, system_name)
SELECT id, kind, unit_id, person_id, counterparty_id, system_code, system_name FROM locations;

-- Для наявних осіб: як було раніше — найближче вгору по структурі місце обліку.
UPDATE locations_new SET unit_id = (
    SELECT CASE
        WHEN u0.is_accounting = 1 THEN u0.id
        WHEN u1.is_accounting = 1 THEN u1.id
        WHEN u2.is_accounting = 1 THEN u2.id
        WHEN u3.is_accounting = 1 THEN u3.id
        WHEN u4.is_accounting = 1 THEN u4.id
        WHEN u5.is_accounting = 1 THEN u5.id
    END
    FROM persons p
    LEFT JOIN units u0 ON u0.id = p.unit_id
    LEFT JOIN units u1 ON u1.id = u0.parent_id
    LEFT JOIN units u2 ON u2.id = u1.parent_id
    LEFT JOIN units u3 ON u3.id = u2.parent_id
    LEFT JOIN units u4 ON u4.id = u3.parent_id
    LEFT JOIN units u5 ON u5.id = u4.parent_id
    WHERE p.id = locations_new.person_id)
WHERE kind = 'person';

-- Не знайшли, а місце обліку в частині одне — воно і видавало.
UPDATE locations_new SET unit_id = (SELECT id FROM units WHERE is_accounting = 1)
WHERE kind = 'person' AND unit_id IS NULL
  AND (SELECT COUNT(*) FROM units WHERE is_accounting = 1) = 1;

DROP TABLE locations;
ALTER TABLE locations_new RENAME TO locations;

CREATE UNIQUE INDEX ux_locations_unit ON locations(unit_id) WHERE kind = 'unit';
CREATE UNIQUE INDEX ux_locations_person ON locations(person_id, IFNULL(unit_id, 0)) WHERE kind = 'person';

-- Тригери старої таблиці зникли разом з нею — відтворюємо.
CREATE TRIGGER locations_unit_must_be_accounting BEFORE INSERT ON locations
WHEN NEW.kind = 'unit' AND (SELECT is_accounting FROM units WHERE id = NEW.unit_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'Підрозділ не позначено як місце обліку');
END;

-- Видане особі може рахуватися лише за місцем обліку.
CREATE TRIGGER locations_person_owner_accounting BEFORE INSERT ON locations
WHEN NEW.kind = 'person' AND NEW.unit_id IS NOT NULL
     AND (SELECT is_accounting FROM units WHERE id = NEW.unit_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'Видане особі може рахуватися лише за місцем обліку');
END;
