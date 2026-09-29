-- A tiny shop for the quick start: two tables, and one column (email) the PII
-- scanner redacts. Every value is invented; the domains are example.com.

CREATE TABLE customers (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    email   TEXT NOT NULL,
    plan    TEXT NOT NULL
);

CREATE TABLE orders (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    total_cents INTEGER NOT NULL,
    placed_on   DATE NOT NULL
);

INSERT INTO customers (name, email, plan) VALUES
    ('Ada Park',     'ada.park@example.com',     'team'),
    ('Ben Okafor',   'ben.okafor@example.com',   'free'),
    ('Chloe Varga',  'chloe.varga@example.com',  'enterprise'),
    ('Dev Raman',    'dev.raman@example.com',    'team'),
    ('Elin Sato',    'elin.sato@example.com',    'free');

INSERT INTO orders (customer_id, total_cents, placed_on) VALUES
    (1, 4900, '2026-08-02'),
    (1, 4900, '2026-09-02'),
    (3, 129000, '2026-09-10'),
    (4, 4900, '2026-09-12'),
    (5, 0, '2026-09-20');

-- The login InterLock uses to reach this database. Read-only: governance
-- decides what an agent may ask for, and the upstream grant is the backstop.
CREATE ROLE shop_reader LOGIN PASSWORD 'shop-reader-dev-only';
GRANT CONNECT ON DATABASE shop TO shop_reader;
GRANT USAGE ON SCHEMA public TO shop_reader;
GRANT SELECT ON customers, orders TO shop_reader;
