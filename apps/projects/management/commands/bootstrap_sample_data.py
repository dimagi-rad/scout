"""
Management command to bootstrap sample data for testing.

Creates a sample e-commerce schema with realistic data,
a project pointing to it, and links it to the test user.

Usage:
    python manage.py bootstrap_sample_data

    # Specify a user email
    python manage.py bootstrap_sample_data --user test@test.com

    # Reset existing sample data first
    python manage.py bootstrap_sample_data --reset
"""

import psycopg2
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.knowledge.models import KnowledgeEntry, TableKnowledge
from apps.projects.models import DatabaseConnection, Project, ProjectMembership, ProjectRole
from apps.users.models import User

SAMPLE_SCHEMA = "sample_ecommerce"
SAMPLE_PROJECT_SLUG = "sample-ecommerce"

# DDL for the sample schema
SCHEMA_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SAMPLE_SCHEMA};

SET search_path TO {SAMPLE_SCHEMA};

CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    plan VARCHAR(20) NOT NULL DEFAULT 'free',
    city VARCHAR(100),
    country VARCHAR(100) DEFAULT 'US',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMP
);
COMMENT ON TABLE customers IS 'Registered customers of the e-commerce platform';
COMMENT ON COLUMN customers.status IS 'Account status: active, churned, suspended';
COMMENT ON COLUMN customers.plan IS 'Subscription plan: free, pro, enterprise';

CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    parent_id INTEGER REFERENCES categories(id),
    description TEXT
);
COMMENT ON TABLE categories IS 'Product category hierarchy';

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    sku VARCHAR(50) UNIQUE NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    price NUMERIC(10, 2) NOT NULL,
    cost NUMERIC(10, 2) NOT NULL,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE products IS 'Product catalog with pricing and inventory';
COMMENT ON COLUMN products.cost IS 'Wholesale cost used for margin calculations';

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    total_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
    discount_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
    shipping_amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
    payment_method VARCHAR(30),
    ordered_at TIMESTAMP NOT NULL DEFAULT NOW(),
    shipped_at TIMESTAMP,
    delivered_at TIMESTAMP
);
COMMENT ON TABLE orders IS 'Customer orders with status tracking';
COMMENT ON COLUMN orders.status IS 'Order lifecycle: pending, confirmed, shipped, delivered, cancelled, refunded';

CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price NUMERIC(10, 2) NOT NULL,
    total_price NUMERIC(10, 2) NOT NULL
);
COMMENT ON TABLE order_items IS 'Individual line items within an order';

CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title VARCHAR(255),
    body TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE reviews IS 'Product reviews and ratings from customers';
"""

# Sample data inserts
SAMPLE_DATA_SQL = f"""
SET search_path TO {SAMPLE_SCHEMA};

-- Categories
INSERT INTO categories (id, name, description, parent_id) VALUES
    (1, 'Electronics', 'Electronic devices and accessories', NULL),
    (2, 'Clothing', 'Apparel and fashion', NULL),
    (3, 'Books', 'Books and publications', NULL),
    (4, 'Laptops', 'Laptop computers', 1),
    (5, 'Phones', 'Mobile phones', 1),
    (6, 'Audio', 'Audio equipment', 1),
    (7, 'Men', 'Mens clothing', 2),
    (8, 'Women', 'Womens clothing', 2)
ON CONFLICT DO NOTHING;

-- Products
INSERT INTO products (id, name, sku, category_id, price, cost, stock_quantity) VALUES
    (1, 'ProBook Laptop 15"', 'ELEC-LAP-001', 4, 1299.99, 850.00, 45),
    (2, 'ProBook Laptop 13"', 'ELEC-LAP-002', 4, 999.99, 650.00, 62),
    (3, 'SmartPhone X1', 'ELEC-PHN-001', 5, 899.99, 520.00, 120),
    (4, 'SmartPhone Lite', 'ELEC-PHN-002', 5, 499.99, 280.00, 200),
    (5, 'Wireless Headphones Pro', 'ELEC-AUD-001', 6, 249.99, 95.00, 300),
    (6, 'Bluetooth Speaker', 'ELEC-AUD-002', 6, 79.99, 32.00, 500),
    (7, 'Classic Denim Jacket', 'CLO-MEN-001', 7, 89.99, 35.00, 150),
    (8, 'Slim Fit Chinos', 'CLO-MEN-002', 7, 59.99, 22.00, 200),
    (9, 'Summer Dress', 'CLO-WMN-001', 8, 69.99, 25.00, 180),
    (10, 'Yoga Pants', 'CLO-WMN-002', 8, 49.99, 18.00, 250),
    (11, 'Python Programming', 'BOOK-001', 3, 44.99, 15.00, 400),
    (12, 'Data Science Handbook', 'BOOK-002', 3, 39.99, 12.00, 350)
ON CONFLICT DO NOTHING;

-- Customers
INSERT INTO customers (id, email, first_name, last_name, status, plan, city, country, created_at, last_login_at) VALUES
    (1, 'alice@example.com', 'Alice', 'Johnson', 'active', 'pro', 'New York', 'US', '2024-01-15', '2025-04-20'),
    (2, 'bob@example.com', 'Bob', 'Smith', 'active', 'enterprise', 'San Francisco', 'US', '2024-02-01', '2025-04-19'),
    (3, 'carol@example.com', 'Carol', 'Williams', 'active', 'free', 'London', 'UK', '2024-03-10', '2025-04-18'),
    (4, 'dave@example.com', 'Dave', 'Brown', 'churned', 'pro', 'Berlin', 'DE', '2024-01-20', '2025-01-15'),
    (5, 'eve@example.com', 'Eve', 'Davis', 'active', 'pro', 'Paris', 'FR', '2024-04-05', '2025-04-20'),
    (6, 'frank@example.com', 'Frank', 'Miller', 'active', 'free', 'Toronto', 'CA', '2024-05-12', '2025-04-17'),
    (7, 'grace@example.com', 'Grace', 'Wilson', 'active', 'enterprise', 'Sydney', 'AU', '2024-06-01', '2025-04-20'),
    (8, 'henry@example.com', 'Henry', 'Taylor', 'suspended', 'free', 'Chicago', 'US', '2024-07-15', '2025-02-01'),
    (9, 'iris@example.com', 'Iris', 'Anderson', 'active', 'pro', 'Seattle', 'US', '2024-08-20', '2025-04-19'),
    (10, 'jack@example.com', 'Jack', 'Thomas', 'active', 'free', 'Austin', 'US', '2024-09-01', '2025-04-16'),
    (11, 'karen@example.com', 'Karen', 'Martinez', 'active', 'pro', 'Miami', 'US', '2024-10-10', '2025-04-15'),
    (12, 'leo@example.com', 'Leo', 'Garcia', 'churned', 'enterprise', 'Tokyo', 'JP', '2024-02-28', '2024-12-01'),
    (13, 'mia@example.com', 'Mia', 'Robinson', 'active', 'free', 'Boston', 'US', '2024-11-15', '2025-04-14'),
    (14, 'noah@example.com', 'Noah', 'Clark', 'active', 'pro', 'Denver', 'US', '2024-12-01', '2025-04-20'),
    (15, 'olivia@example.com', 'Olivia', 'Lee', 'active', 'enterprise', 'Singapore', 'SG', '2025-01-10', '2025-04-20')
ON CONFLICT DO NOTHING;

-- Orders (mix of statuses and dates)
INSERT INTO orders (id, customer_id, status, total_amount, discount_amount, shipping_amount, payment_method, ordered_at, shipped_at, delivered_at) VALUES
    (1, 1, 'delivered', 1549.98, 0, 9.99, 'credit_card', '2025-01-15 10:30:00', '2025-01-16', '2025-01-20'),
    (2, 2, 'delivered', 899.99, 50.00, 0, 'credit_card', '2025-01-20 14:00:00', '2025-01-21', '2025-01-25'),
    (3, 3, 'delivered', 129.98, 0, 12.99, 'paypal', '2025-02-01 09:15:00', '2025-02-02', '2025-02-08'),
    (4, 1, 'delivered', 249.99, 25.00, 0, 'credit_card', '2025-02-10 16:45:00', '2025-02-11', '2025-02-14'),
    (5, 5, 'delivered', 999.99, 0, 9.99, 'credit_card', '2025-02-15 11:20:00', '2025-02-16', '2025-02-20'),
    (6, 7, 'delivered', 1389.98, 100.00, 0, 'bank_transfer', '2025-02-20 08:00:00', '2025-02-21', '2025-02-28'),
    (7, 2, 'delivered', 159.98, 0, 9.99, 'credit_card', '2025-03-01 13:30:00', '2025-03-02', '2025-03-05'),
    (8, 9, 'delivered', 44.99, 0, 4.99, 'paypal', '2025-03-05 17:10:00', '2025-03-06', '2025-03-10'),
    (9, 4, 'cancelled', 499.99, 0, 0, 'credit_card', '2025-03-08 12:00:00', NULL, NULL),
    (10, 6, 'delivered', 89.99, 10.00, 7.99, 'paypal', '2025-03-10 10:00:00', '2025-03-11', '2025-03-15'),
    (11, 10, 'delivered', 579.98, 0, 0, 'credit_card', '2025-03-15 09:00:00', '2025-03-16', '2025-03-19'),
    (12, 11, 'shipped', 329.98, 0, 9.99, 'credit_card', '2025-03-20 15:00:00', '2025-03-21', NULL),
    (13, 14, 'shipped', 1299.99, 0, 0, 'credit_card', '2025-03-25 11:00:00', '2025-03-26', NULL),
    (14, 15, 'confirmed', 1799.98, 150.00, 0, 'bank_transfer', '2025-04-01 08:30:00', NULL, NULL),
    (15, 3, 'pending', 79.99, 0, 5.99, 'paypal', '2025-04-10 16:00:00', NULL, NULL),
    (16, 13, 'delivered', 109.98, 0, 9.99, 'credit_card', '2025-03-28 14:20:00', '2025-03-29', '2025-04-02'),
    (17, 1, 'delivered', 499.99, 0, 0, 'credit_card', '2025-04-05 10:15:00', '2025-04-06', '2025-04-09'),
    (18, 7, 'confirmed', 539.98, 0, 9.99, 'bank_transfer', '2025-04-12 09:00:00', NULL, NULL),
    (19, 5, 'refunded', 249.99, 0, 0, 'credit_card', '2025-03-18 12:30:00', '2025-03-19', '2025-03-22'),
    (20, 2, 'delivered', 84.98, 0, 7.99, 'credit_card', '2025-04-08 11:45:00', '2025-04-09', '2025-04-12')
ON CONFLICT DO NOTHING;

-- Order items
INSERT INTO order_items (order_id, product_id, quantity, unit_price, total_price) VALUES
    (1, 1, 1, 1299.99, 1299.99),
    (1, 5, 1, 249.99, 249.99),
    (2, 3, 1, 899.99, 899.99),
    (3, 7, 1, 89.99, 89.99),
    (3, 11, 1, 39.99, 39.99),
    (4, 5, 1, 249.99, 249.99),
    (5, 2, 1, 999.99, 999.99),
    (6, 3, 1, 899.99, 899.99),
    (6, 4, 1, 499.99, 489.99),
    (7, 8, 1, 59.99, 59.99),
    (7, 10, 2, 49.99, 99.99),
    (8, 11, 1, 44.99, 44.99),
    (9, 4, 1, 499.99, 499.99),
    (10, 7, 1, 89.99, 89.99),
    (11, 4, 1, 499.99, 499.99),
    (11, 6, 1, 79.99, 79.99),
    (12, 5, 1, 249.99, 249.99),
    (12, 6, 1, 79.99, 79.99),
    (13, 1, 1, 1299.99, 1299.99),
    (14, 1, 1, 1299.99, 1299.99),
    (14, 4, 1, 499.99, 499.99),
    (15, 6, 1, 79.99, 79.99),
    (16, 9, 1, 69.99, 69.99),
    (16, 12, 1, 39.99, 39.99),
    (17, 4, 1, 499.99, 499.99),
    (18, 3, 1, 899.99, 489.99),
    (18, 10, 1, 49.99, 49.99),
    (19, 5, 1, 249.99, 249.99),
    (20, 11, 1, 44.99, 44.99),
    (20, 12, 1, 39.99, 39.99)
ON CONFLICT DO NOTHING;

-- Reviews
INSERT INTO reviews (product_id, customer_id, rating, title, body, created_at) VALUES
    (1, 1, 5, 'Excellent laptop', 'Best laptop I have ever owned. Fast and reliable.', '2025-01-25'),
    (3, 2, 4, 'Great phone', 'Good camera and battery life. A bit pricey.', '2025-01-30'),
    (5, 1, 5, 'Amazing sound', 'Noise cancellation is top notch.', '2025-02-20'),
    (7, 3, 3, 'Decent jacket', 'Fits well but material could be better.', '2025-02-15'),
    (11, 3, 5, 'Must read', 'Comprehensive guide to Python. Highly recommended.', '2025-02-10'),
    (2, 5, 4, 'Solid machine', 'Great for development work. Lightweight and fast.', '2025-02-25'),
    (4, 7, 4, 'Good value', 'Great phone for the price. Camera is decent.', '2025-03-05'),
    (6, 6, 5, 'Love this speaker', 'Surprisingly loud for its size. Great bass.', '2025-03-18'),
    (1, 14, 5, 'Worth every penny', 'Using it for data science work. Handles everything.', '2025-04-01'),
    (5, 9, 4, 'Very comfortable', 'Wear them all day. Sound quality is excellent.', '2025-03-12'),
    (10, 11, 5, 'Perfect fit', 'Most comfortable pants I own. Great for workouts.', '2025-03-25'),
    (9, 13, 4, 'Pretty dress', 'Nice fabric and flattering cut. Runs a bit small.', '2025-04-03'),
    (4, 10, 3, 'Its okay', 'Does the basics but nothing special. Battery drains fast.', '2025-03-20'),
    (12, 8, 4, 'Informative', 'Good reference book. Some chapters could be deeper.', '2025-02-05'),
    (3, 15, 5, 'Premium quality', 'The display is gorgeous. Fast performance.', '2025-04-10')
ON CONFLICT DO NOTHING;

-- Sequences
SELECT setval('{SAMPLE_SCHEMA}.customers_id_seq', 15, true);
SELECT setval('{SAMPLE_SCHEMA}.categories_id_seq', 8, true);
SELECT setval('{SAMPLE_SCHEMA}.products_id_seq', 12, true);
SELECT setval('{SAMPLE_SCHEMA}.orders_id_seq', 20, true);
"""


class Command(BaseCommand):
    help = "Bootstrap sample e-commerce data for testing the Scout agent"

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            default="test@test.com",
            help="Email of the user to grant project access (default: test@test.com)",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Drop and recreate sample data if it already exists",
        )

    def handle(self, *args, **options):
        user_email = options["user"]
        reset = options["reset"]

        # 1. Verify user exists
        try:
            user = User.objects.get(email=user_email)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(
                f"User '{user_email}' not found. Create it first:\n"
                f"  python manage.py createsuperuser --email {user_email}"
            ))
            return

        self.stdout.write(f"Using user: {user.email}")

        # 2. Create sample schema and tables in the platform database
        self._create_sample_schema(reset)

        # 3. Create or get DatabaseConnection from Django DB settings
        db_settings = settings.DATABASES["default"]
        db_host = db_settings.get("HOST", "localhost")
        db_port = db_settings.get("PORT", 5432)
        db_name = db_settings.get("NAME", "agent_platform")
        db_user_name = db_settings.get("USER", "platform")
        db_password = db_settings.get("PASSWORD", "")

        connection, conn_created = DatabaseConnection.objects.get_or_create(
            name="Platform Database",
            defaults={
                "db_host": db_host,
                "db_port": db_port,
                "db_name": db_name,
                "created_by": user,
            },
        )
        if conn_created:
            connection.db_user = db_user_name
            connection.db_password = db_password
            connection.save()
            self.stdout.write(self.style.SUCCESS("Created database connection: Platform Database"))
        else:
            self.stdout.write("Using existing database connection: Platform Database")

        # 4. Create or update the Project
        project, created = Project.objects.update_or_create(
            slug=SAMPLE_PROJECT_SLUG,
            defaults={
                "name": "Sample E-Commerce",
                "description": (
                    "A sample e-commerce dataset with customers, products, orders, "
                    "and reviews. Use this project to explore Scout's capabilities."
                ),
                "database_connection": connection,
                "db_schema": SAMPLE_SCHEMA,
                "max_rows_per_query": 500,
                "max_query_timeout_seconds": 30,
                "is_active": True,
                "created_by": user,
                "system_prompt": (
                    "You are analyzing an e-commerce platform's database. "
                    "The data includes customers, products, orders, and reviews. "
                    "When answering questions about revenue, use the total_amount "
                    "from the orders table and exclude cancelled and refunded orders "
                    "unless asked otherwise. Monetary values are in USD."
                ),
            },
        )

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} project: {project.name}"))

        # 5. Create ProjectMembership
        membership, mem_created = ProjectMembership.objects.get_or_create(
            user=user,
            project=project,
            defaults={"role": ProjectRole.ADMIN},
        )
        if mem_created:
            self.stdout.write(self.style.SUCCESS(
                f"Granted {user.email} admin access to {project.name}"
            ))
        else:
            self.stdout.write(f"User already has access (role: {membership.role})")

        # 6. Generate data dictionary
        self.stdout.write("Generating data dictionary...")
        from apps.projects.services.data_dictionary import DataDictionaryGenerator

        generator = DataDictionaryGenerator(project)
        generator.generate()
        table_count = len(project.data_dictionary.get("tables", {}))
        self.stdout.write(self.style.SUCCESS(f"Data dictionary: {table_count} tables"))

        # 7. Add knowledge
        self._create_table_knowledge(project, user)
        self._create_metrics(project, user)
        self._create_business_rules(project, user)

        self.stdout.write(self.style.SUCCESS(
            "\nBootstrap complete! Log in and select the "
            f"'{project.name}' project to start chatting."
        ))

    def _create_sample_schema(self, reset):
        """Create the sample schema and tables using a raw DB connection."""
        self.stdout.write("Setting up sample database schema...")

        db_settings = settings.DATABASES["default"]
        conn = psycopg2.connect(
            host=db_settings.get("HOST", "localhost"),
            port=db_settings.get("PORT", 5432),
            dbname=db_settings.get("NAME", "agent_platform"),
            user=db_settings.get("USER", "platform"),
            password=db_settings.get("PASSWORD", ""),
        )
        conn.autocommit = True

        try:
            with conn.cursor() as cur:
                if reset:
                    self.stdout.write("  Dropping existing schema...")
                    cur.execute(f"DROP SCHEMA IF EXISTS {SAMPLE_SCHEMA} CASCADE")

                cur.execute(SCHEMA_DDL)
                self.stdout.write(self.style.SUCCESS("  Schema and tables created"))

                cur.execute(SAMPLE_DATA_SQL)
                self.stdout.write(self.style.SUCCESS("  Sample data inserted"))
        finally:
            conn.close()

    def _create_table_knowledge(self, project, user):
        """Add table knowledge entries."""
        tables = [
            {
                "table_name": "customers",
                "description": (
                    "Core customer table. Each row is a registered user. "
                    "Status tracks lifecycle (active/churned/suspended). "
                    "Plan indicates subscription tier (free/pro/enterprise)."
                ),
                "use_cases": [
                    "Customer segmentation by plan or status",
                    "Geographic distribution analysis",
                    "Churn analysis and retention metrics",
                ],
                "data_quality_notes": [
                    "Some customers have NULL city - they registered before we added that field",
                    "last_login_at can be NULL for customers who never logged in after registration",
                ],
                "column_notes": {
                    "status": "Values: active, churned, suspended. Churned = no activity 90+ days",
                    "plan": "Values: free, pro, enterprise. Determines feature access and pricing",
                },
                "related_tables": [
                    {"table": "orders", "join_hint": "orders.customer_id = customers.id"},
                    {"table": "reviews", "join_hint": "reviews.customer_id = customers.id"},
                ],
            },
            {
                "table_name": "orders",
                "description": (
                    "All customer orders. Total amount is the sum of line items "
                    "before discounts. The net revenue is total_amount - discount_amount. "
                    "Shipping is charged separately."
                ),
                "use_cases": [
                    "Revenue reporting and trends",
                    "Order volume and average order value",
                    "Fulfillment tracking (pending -> shipped -> delivered)",
                ],
                "data_quality_notes": [
                    "Cancelled orders have NULL shipped_at and delivered_at",
                    "Refunded orders were previously delivered - check status, not dates",
                ],
                "column_notes": {
                    "status": "Lifecycle: pending, confirmed, shipped, delivered, cancelled, refunded",
                    "total_amount": "Sum of line item prices, before discount",
                    "discount_amount": "Discount applied - subtract from total for net revenue",
                    "payment_method": "Values: credit_card, paypal, bank_transfer",
                },
                "related_tables": [
                    {"table": "customers", "join_hint": "orders.customer_id = customers.id"},
                    {"table": "order_items", "join_hint": "order_items.order_id = orders.id"},
                ],
            },
            {
                "table_name": "products",
                "description": (
                    "Product catalog. Price is the customer-facing price, cost is "
                    "the wholesale cost. Margin = price - cost."
                ),
                "use_cases": [
                    "Product performance analysis",
                    "Margin and profitability reporting",
                    "Inventory management",
                ],
                "column_notes": {
                    "price": "Customer-facing retail price in USD",
                    "cost": "Wholesale cost for margin calculation",
                    "is_active": "FALSE means product is discontinued, not shown to customers",
                },
                "related_tables": [
                    {"table": "categories", "join_hint": "products.category_id = categories.id"},
                    {"table": "order_items", "join_hint": "order_items.product_id = products.id"},
                    {"table": "reviews", "join_hint": "reviews.product_id = products.id"},
                ],
            },
            {
                "table_name": "order_items",
                "description": (
                    "Line items for each order. An order can have multiple items. "
                    "Unit price is the price at time of purchase (may differ from current product price)."
                ),
                "use_cases": [
                    "Product-level sales analysis",
                    "Items per order metrics",
                    "Revenue attribution by product",
                ],
                "related_tables": [
                    {"table": "orders", "join_hint": "order_items.order_id = orders.id"},
                    {"table": "products", "join_hint": "order_items.product_id = products.id"},
                ],
            },
            {
                "table_name": "reviews",
                "description": "Product reviews with 1-5 star ratings from customers.",
                "use_cases": [
                    "Product quality analysis",
                    "Customer satisfaction scoring",
                    "Identifying products needing improvement",
                ],
                "related_tables": [
                    {"table": "products", "join_hint": "reviews.product_id = products.id"},
                    {"table": "customers", "join_hint": "reviews.customer_id = customers.id"},
                ],
            },
            {
                "table_name": "categories",
                "description": (
                    "Hierarchical product categories. Parent_id is self-referencing "
                    "for subcategories (e.g., Electronics > Laptops)."
                ),
                "use_cases": [
                    "Sales breakdown by category",
                    "Category hierarchy navigation",
                ],
                "related_tables": [
                    {"table": "products", "join_hint": "products.category_id = categories.id"},
                ],
            },
        ]

        count = 0
        for t in tables:
            _, created = TableKnowledge.objects.update_or_create(
                project=project,
                table_name=t["table_name"],
                defaults={**t, "updated_by": user},
            )
            if created:
                count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Table knowledge: {count} created, {len(tables) - count} updated"
        ))

    def _create_metrics(self, project, user):
        """Add metric knowledge entries."""
        metrics = [
            {
                "title": "Gross Revenue",
                "content": (
                    "Total revenue from all non-cancelled, non-refunded orders. "
                    "Calculated as SUM(total_amount) from orders.\n\n"
                    "```sql\n"
                    "SELECT SUM(total_amount) AS gross_revenue\n"
                    "FROM orders\n"
                    "WHERE status NOT IN ('cancelled', 'refunded')\n"
                    "```\n\n"
                    "**Unit:** USD\n\n"
                    "**Caveats:**\n"
                    "- Does not subtract discounts - use Net Revenue for that\n"
                    "- Includes shipping amounts in total"
                ),
                "tags": ["metric", "finance", "revenue"],
            },
            {
                "title": "Net Revenue",
                "content": (
                    "Revenue after discounts, excluding cancelled and refunded orders. "
                    "Calculated as SUM(total_amount - discount_amount).\n\n"
                    "```sql\n"
                    "SELECT SUM(total_amount - discount_amount) AS net_revenue\n"
                    "FROM orders\n"
                    "WHERE status NOT IN ('cancelled', 'refunded')\n"
                    "```\n\n"
                    "**Unit:** USD\n\n"
                    "**Caveats:**\n"
                    "- Does not include shipping revenue"
                ),
                "tags": ["metric", "finance", "revenue"],
            },
            {
                "title": "Average Order Value (AOV)",
                "content": (
                    "Average total amount per order, excluding cancelled and refunded.\n\n"
                    "```sql\n"
                    "SELECT AVG(total_amount) AS aov\n"
                    "FROM orders\n"
                    "WHERE status NOT IN ('cancelled', 'refunded')\n"
                    "```\n\n"
                    "**Unit:** USD"
                ),
                "tags": ["metric", "finance", "orders"],
            },
            {
                "title": "Customer Count",
                "content": (
                    "Total number of active customers (status = 'active').\n\n"
                    "```sql\n"
                    "SELECT COUNT(*) AS active_customers\n"
                    "FROM customers\n"
                    "WHERE status = 'active'\n"
                    "```\n\n"
                    "**Unit:** customers"
                ),
                "tags": ["metric", "customers", "growth"],
            },
            {
                "title": "Churn Rate",
                "content": (
                    "Percentage of customers with status 'churned' out of all customers.\n\n"
                    "```sql\n"
                    "SELECT\n"
                    "  ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'churned') "
                    "/ COUNT(*), 2) AS churn_rate_pct\n"
                    "FROM customers\n"
                    "```\n\n"
                    "**Unit:** percentage\n\n"
                    "**Caveats:**\n"
                    "- Simple lifetime churn rate, not a period-based cohort metric"
                ),
                "tags": ["metric", "customers", "retention"],
            },
        ]

        count = 0
        for m in metrics:
            _, created = KnowledgeEntry.objects.update_or_create(
                project=project,
                title=m["title"],
                defaults={**m, "created_by": user},
            )
            if created:
                count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Metric entries: {count} created, {len(metrics) - count} updated"
        ))

    def _create_business_rules(self, project, user):
        """Add business rule knowledge entries."""
        rules = [
            {
                "title": "Revenue excludes cancelled and refunded orders",
                "content": (
                    "When calculating revenue metrics, always exclude orders with "
                    "status 'cancelled' or 'refunded'. These do not represent "
                    "actual earned revenue.\n\n"
                    "**Applies to:** Tables: orders; "
                    "Metrics: Gross Revenue, Net Revenue, Average Order Value (AOV)"
                ),
                "tags": ["rule", "finance", "revenue"],
            },
            {
                "title": "Net revenue subtracts discounts",
                "content": (
                    "Net revenue is total_amount minus discount_amount. "
                    "When asked about 'revenue' without qualification, "
                    "default to gross revenue but mention the discount impact.\n\n"
                    "**Applies to:** Tables: orders; Metrics: Net Revenue"
                ),
                "tags": ["rule", "finance"],
            },
            {
                "title": "Churned customers defined by status field",
                "content": (
                    "A customer is considered churned when their status field is 'churned'. "
                    "Do not infer churn from last_login_at alone.\n\n"
                    "**Applies to:** Tables: customers; Metrics: Churn Rate"
                ),
                "tags": ["rule", "customers"],
            },
        ]

        count = 0
        for r in rules:
            _, created = KnowledgeEntry.objects.update_or_create(
                project=project,
                title=r["title"],
                defaults={**r, "created_by": user},
            )
            if created:
                count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Rule entries: {count} created, {len(rules) - count} updated"
        ))
