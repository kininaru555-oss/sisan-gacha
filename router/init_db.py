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
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
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
                creator_yen INTEGER NOT NULL,
                distribution_round INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (bundle_id, entry_user_id, original_creator_user_id, distribution_round)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_stop_requests (
                id SERIAL PRIMARY KEY,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP NULL
            )
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_prompts_review_status'
                ) THEN
                    ALTER TABLE prompts
                    ADD CONSTRAINT chk_prompts_review_status
                    CHECK (review_status IN ('pending_review', 'accepted', 'rejected'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_bundles_status'
                ) THEN
                    ALTER TABLE bundles DROP CONSTRAINT chk_bundles_status;
                END IF;
                ALTER TABLE bundles
                ADD CONSTRAINT chk_bundles_status
                CHECK (status IN ('recruiting', 'active', 'closed'));
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_withdrawal_requests_status'
                ) THEN
                    ALTER TABLE withdrawal_requests
                    ADD CONSTRAINT chk_withdrawal_requests_status
                    CHECK (status IN ('pending', 'approved', 'paid', 'rejected'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_withdrawal_requests_method'
                ) THEN
                    ALTER TABLE withdrawal_requests
                    ADD CONSTRAINT chk_withdrawal_requests_method
                    CHECK (method IN ('paypay', 'amazon_gift'));
                END IF;
            END $$;
            """
        )

        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_prompt_stop_requests_status'
                ) THEN
                    ALTER TABLE prompt_stop_requests
                    ADD CONSTRAINT chk_prompt_stop_requests_status
                    CHECK (status IN ('pending', 'approved', 'rejected'));
                END IF;
            END $$;
            """
        )

        index_statements = [
            "CREATE INDEX IF NOT EXISTS ix_users_role ON users(role)",
            "CREATE INDEX IF NOT EXISTS ix_users_is_active ON users(is_active)",
            "CREATE INDEX IF NOT EXISTS ix_user_refresh_tokens_user_id ON user_refresh_tokens(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_user_refresh_tokens_expires_at ON user_refresh_tokens(expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_user_refresh_tokens_revoked_at ON user_refresh_tokens(revoked_at)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_user_id ON prompts(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_category ON prompts(category)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_created_at ON prompts(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_review_status ON prompts(review_status)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_is_visible ON prompts(is_visible)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_bundle_entry_enabled ON prompts(bundle_entry_enabled)",
            "CREATE INDEX IF NOT EXISTS ix_prompts_resale_offer_enabled ON prompts(resale_offer_enabled)",
            "CREATE INDEX IF NOT EXISTS ix_gacha_logs_user_id ON gacha_logs(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_gacha_logs_prompt_id ON gacha_logs(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_user_id ON payments(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status)",
            "CREATE INDEX IF NOT EXISTS ix_withdraw_codes_user_id ON withdraw_codes(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_withdraw_codes_code ON withdraw_codes(code)",
            "CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_user_id ON withdrawal_requests(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_status ON withdrawal_requests(status)",
            "CREATE INDEX IF NOT EXISTS ix_bundles_status ON bundles(status)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_bundle_id ON bundle_items(bundle_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_prompt_id ON bundle_items(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_entry_user_id ON bundle_items(entry_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_items_original_creator_user_id ON bundle_items(original_creator_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_reward_distributions_bundle_id ON bundle_reward_distributions(bundle_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_reward_distributions_entry_user_id ON bundle_reward_distributions(entry_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_bundle_reward_distributions_original_creator_user_id ON bundle_reward_distributions(original_creator_user_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_prompt_id ON prompt_stop_requests(prompt_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_user_id ON prompt_stop_requests(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_prompt_stop_requests_status ON prompt_stop_requests(status)",
        ]
        for stmt in index_statements:
            cur.execute(stmt)
