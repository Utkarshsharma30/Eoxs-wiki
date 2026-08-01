-- Extensions needed across the schema.
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy/ILIKE search support, used later for title matching
