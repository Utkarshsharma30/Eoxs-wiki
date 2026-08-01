-- Reference tables: clients and contacts.
-- Source of truth: tools/config.yaml (odoo_implementation.clients) and
-- tools/contact_registry.yaml (clients.<slug>.contacts) in the vault repo.

CREATE TABLE clients (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,        -- e.g. 'eastern-states-steel'
    display_name    TEXT NOT NULL,                -- e.g. 'Eastern States Steel'
    domains         TEXT[] NOT NULL DEFAULT '{}', -- email domains associated with this client
    odoo_base_url   TEXT,
    odoo_db         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
    id              SERIAL PRIMARY KEY,
    client_id       INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    email           TEXT,                          -- nullable: contact_registry.yaml has null emails
    is_relay_inbox  BOOLEAN NOT NULL DEFAULT false, -- per-client personal-gmail relay used for Zoho routing
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_contacts_client_id ON contacts(client_id);
CREATE INDEX idx_contacts_email ON contacts(email);
