CREATE TABLE IF NOT EXISTS customers (
    id INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    ssn VARCHAR(32) NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    total DECIMAL(12, 2) NOT NULL,
    status VARCHAR(32) NOT NULL,
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS mutation_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    action VARCHAR(64) NOT NULL,
    details TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO customers (id, name, email, ssn, note)
VALUES
    (1, 'Ada Lovelace', 'ada@example.com', '123-45-6789', 'cacheable alpha'),
    (2, 'Grace Hopper', 'grace@example.com', '987-65-4321', 'cacheable beta')
ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    email = VALUES(email),
    ssn = VALUES(ssn),
    note = VALUES(note);

INSERT INTO orders (id, customer_id, total, status)
VALUES
    (100, 1, 42.50, 'open'),
    (101, 2, 101.25, 'closed')
ON DUPLICATE KEY UPDATE
    customer_id = VALUES(customer_id),
    total = VALUES(total),
    status = VALUES(status);
