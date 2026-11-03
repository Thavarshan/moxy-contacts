CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(CAST(display_name AS BLOB)) <= 4096),
    search_text TEXT NOT NULL CHECK (length(CAST(search_text AS BLOB)) <= 131072),
    duplicate_key TEXT NOT NULL CHECK (length(CAST(duplicate_key AS BLOB)) <= 128),
    original_sha256 TEXT NOT NULL CHECK (length(original_sha256) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    original_size INTEGER NOT NULL CHECK (original_size BETWEEN 1 AND 1900000),
    current_size INTEGER NOT NULL CHECK (current_size BETWEEN 1 AND 1900000)
);

CREATE INDEX IF NOT EXISTS contacts_display_name_idx ON contacts(display_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS contacts_original_sha256_idx ON contacts(original_sha256);
CREATE INDEX IF NOT EXISTS contacts_duplicate_key_idx ON contacts(duplicate_key);

CREATE TABLE IF NOT EXISTS contact_documents (
    contact_id TEXT NOT NULL,
    version TEXT NOT NULL CHECK (version IN ('original', 'current')),
    vcard TEXT NOT NULL CHECK (length(CAST(vcard AS BLOB)) BETWEEN 1 AND 1900000),
    PRIMARY KEY (contact_id, version),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);
