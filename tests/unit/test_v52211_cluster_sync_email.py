"""v5.22.11 — users.email must replicate across the cluster.

Found live on 2026-08-12: after deploying v5.22.10 (login by email), the
email identifier worked on tmrwww01 and returned 401 on tmrwww02. Both nodes
held the same user row with an IDENTICAL last_user_edit_at — cluster sync had
replicated the row but not the column, because `email` (added in v5.22.7) was
never added to the sync payload or the merge handler.

Consequence while broken: self-service password reset, SSO account matching
and email login all worked only on whichever node the address happened to be
set on, with no error anywhere to indicate why.

These tests are deliberately source-level and field-level rather than
end-to-end: the failure was a silently missing dict key, which no behavioural
test on a single node can catch.
"""
from pathlib import Path


class TestPayloadCarriesEmail:
    def test_snapshot_builder_emits_email(self):
        src = Path("app/cluster/manager.py").read_text(encoding="utf-8")
        # locate the users payload comprehension
        i = src.index('{"id": u.id, "username": u.username')
        block = src[i:i + 700]
        assert '"email": u.email' in block, (
            "the users cluster-sync payload must include email, or the column "
            "replicates as NULL"
        )

    def test_every_user_column_that_matters_is_synced(self):
        """Guard against the NEXT column being forgotten the same way."""
        src = Path("app/cluster/manager.py").read_text(encoding="utf-8")
        i = src.index('{"id": u.id, "username": u.username')
        block = src[i:i + 700]
        for field in ("id", "username", "password_hash", "role", "email",
                      "deleted_at", "last_user_edit_at"):
            assert f'"{field}"' in block, f"users sync payload is missing {field!r}"


class TestMergeAppliesEmail:
    def test_insert_path_sets_email(self):
        src = Path("app/cluster/sync.py").read_text(encoding="utf-8")
        i = src.index("db.add(User(")
        block = src[i:i + 500]
        assert "email=u_data.get(\"email\")" in block, (
            "a user inserted from a peer must carry the peer's email"
        )

    def test_update_path_sets_email(self):
        src = Path("app/cluster/sync.py").read_text(encoding="utf-8")
        assert "existing.email = u_data.get(\"email\")" in src, (
            "an LWW update from a peer must carry email across"
        )

    def test_update_path_tolerates_older_peers(self):
        """A peer running < v5.22.11 omits the key entirely.

        Treat that as 'no opinion' and keep the local address, rather than
        blanking a locally-set email during a rolling upgrade.
        """
        src = Path("app/cluster/sync.py").read_text(encoding="utf-8")
        i = src.index("existing.email = u_data.get(\"email\")")
        preceding = src[max(0, i - 200):i]
        assert 'if "email" in u_data' in preceding, (
            "email must only be overwritten when the peer actually sent the "
            "key, or a rolling upgrade wipes addresses"
        )


class TestModelStillHasTheColumn:
    def test_email_column_exists(self):
        from app.models.db_user import User
        assert "email" in User.__table__.columns
