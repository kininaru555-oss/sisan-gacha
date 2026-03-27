from __future__ import annotations

from db import db_transaction


def init_db():
    with db_transaction() as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                password_hash TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                token_version INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                points INTEGER NOT NULL DEFAULT 0,
                free_gacha INTEGER NOT NULL DEFAULT 0,
                locked_points INTEGER NOT NULL DEFAULT 0,
                post_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS free_gacha INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_points INTEGER NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS post_count INTEGER NOT NULL DEFAULT 0")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_refresh_tokens (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                refresh_token_hash TEXT NOT NULL UNIQUE,
                csrf_token_hash TEXT NOT NULL,
                token_family TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_used_at TIMESTAMP NULL,
                user_agent TEXT,
                ip_address TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                original_creator_user_id TEXT,
                title TEXT,
                content TEXT,
                category TEXT,
                url TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                review_status TEXT NOT NULL DEFAULT 'accepted',
                is_visible BOOLEAN NOT NULL DEFAULT TRUE,
                bundle_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                resale_offer_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                bundle_consented_at TIMESTAMP NULL,
                reviewed_at TIMESTAMP NULL,
                review_note TEXT
            )
            """
        )

        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS original_creator_user_id TEXT")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS is_visible BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS bundle_entry_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS resale_offer_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS bundle_consented_at TIMESTAMP NULL")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP NULL")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS review_note TEXT")
        cur.execute("ALTER TABLE prompts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NULL")

        cur.execute(
            """
            UPDATE prompts
            SET created_at = NOW()
            WHERE created_at IS NULL
            """
        )

        cur.execute(
            """
            UPDATE prompts
            SET original_creator_user_id = user_id
            WHERE original_creator_user_id IS NULL
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gacha_logs (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute("ALTER TABLE gacha_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NULL")
        cur.execute(
            """
            UPDATE gacha_logs
            SET created_at = NOW()
            WHERE created_at IS NULL
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS creator_wallets (
                user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                yen INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                amount_yen INTEGER,
                method TEXT,
                destination TEXT,
                withdraw_code TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP NULL
            )
            """
        )

        cur.execute("ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NULL")
        cur.execute(
            """
            UPDATE withdrawal_requests
            SET created_at = NOW()
            WHERE created_at IS NULL
            """
        )

        # =========================
        # 🔥 修正対象：payments
        # =========================
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                product_code TEXT NOT NULL,
                points_to_add INTEGER NOT NULL,
                amount_jpy INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMP NULL
            )
            """
        )

        # 🔧 既存環境マイグレーション
        cur.execute(
            "ALTER TABLE payments ALTER COLUMN created_at TYPE TIMESTAMP USING created_at::TIMESTAMP"
        )
        cur.execute(
            "ALTER TABLE payments ALTER COLUMN completed_at TYPE TIMESTAMP USING completed_at::TIMESTAMP"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdraw_codes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundles (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                target_article_count INTEGER NOT NULL DEFAULT 1,
                genre TEXT NOT NULL DEFAULT 'その他',
                price_points INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'recruiting',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                published_at TIMESTAMP NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_items (
                id SERIAL PRIMARY KEY,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                entry_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                original_creator_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                entry_type TEXT NOT NULL DEFAULT 'own',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (bundle_id, prompt_id, entry_user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_purchases (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                price_points INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, bundle_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bundle_reward_distributions (
                id SERIAL PRIMARY KEY,
                bundle_id INTEGER NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
                entry_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                original_creator_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                sales_yen INTEGER NOT NULL,
                entry_yen INTEGER NOT NULL,
                creator_yen INTEGER NOT NULL
            )
            """
)
