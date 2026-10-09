-- Службові дані бази: ідентифікатор, лічильник змін, версія.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Налаштування користувача: реквізити військової частини, підписанти тощо.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
