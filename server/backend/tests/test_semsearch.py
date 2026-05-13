"""Unit tests for semsearch helpers and the semsearch-enabled store query path.

Pure-Python helpers (build_field_logits, compute_combined_relevance) are tested
without any external dependencies.

Integration tests patch semsearch._ENABLED and semsearch._get_embeddings
(and related callables) so the semsearch code paths run deterministically in CI
without a real embedding server or sqlite-vec loaded.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cq.models import Context, Insight, KnowledgeUnit, Tier, create_knowledge_unit

from cq_server import semsearch
from cq_server.store import SqliteStore


def _make_unit(domain: str = "test", *, summary: str = "s", detail: str = "d", action: str = "a") -> KnowledgeUnit:
    return create_knowledge_unit(
        domains=[domain],
        insight=Insight(summary=summary, detail=detail, action=action),
        context=Context(),
        tier=Tier.PRIVATE,
        created_by="tester",
    )


@pytest_asyncio.fixture()
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(db_path=tmp_path / "test.db")
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Pure-Python helper: build_field_logits
# ---------------------------------------------------------------------------


class TestBuildFieldLogits:
    def test_empty_input_returns_empty(self) -> None:
        assert semsearch.build_field_logits({}) == {}

    def test_row_with_no_numeric_fields_returns_empty(self) -> None:
        result = semsearch.build_field_logits({"a": ()})
        assert result == {}

    def test_uniform_values_produce_zero_logits(self) -> None:
        row_data = {"a": (1.0,), "b": (1.0,)}
        logits = semsearch.build_field_logits(row_data, invert=True)
        assert 0 in logits
        for logit in logits[0].values():
            assert logit == 0.0

    def test_lower_distance_gets_positive_logit_when_inverted(self) -> None:
        row_data = {"near": (0.1,), "far": (0.9,)}
        logits = semsearch.build_field_logits(row_data, invert=True)
        assert 0 in logits
        assert logits[0][0.1] > 0, "nearer unit should be boosted"
        assert logits[0][0.9] < 0, "farther unit should be diminished"
        assert logits[0][0.1] > logits[0][0.9]

    def test_non_inverted_higher_value_gets_higher_logit(self) -> None:
        row_data = {"low": (0.1,), "high": (0.9,)}
        logits = semsearch.build_field_logits(row_data, invert=False)
        assert 0 in logits
        assert logits[0][0.9] > logits[0][0.1]


# ---------------------------------------------------------------------------
# Pure-Python helper: compute_combined_relevance
# ---------------------------------------------------------------------------


class TestComputeCombinedRelevance:
    def test_no_field_logits_returns_base_unchanged(self) -> None:
        result = semsearch.compute_combined_relevance(0.5, (), {})
        assert result == 0.5

    def test_zero_logit_preserves_base(self) -> None:
        field_logits = {0: {0.5: 0.0}}
        result = semsearch.compute_combined_relevance(0.5, (0.5,), field_logits)
        assert result == pytest.approx(0.5)

    def test_positive_logit_boosts_score(self) -> None:
        # logit=1.0 -> combined *= (1 + 1.0) = 2x base
        field_logits = {0: {0.1: 1.0}}
        result = semsearch.compute_combined_relevance(0.5, (0.1,), field_logits)
        assert result == pytest.approx(1.0)

    def test_negative_logit_reduces_score(self) -> None:
        # logit=-0.5 -> combined *= (1 - 0.5) = 0.5x base
        field_logits = {0: {0.9: -0.5}}
        result = semsearch.compute_combined_relevance(0.5, (0.9,), field_logits)
        assert result == pytest.approx(0.25)

    def test_missing_field_value_applies_neutral_modulation(self) -> None:
        # row_data has 0.9 but logit_map only has 0.3; missing key -> logit 0 -> no change
        field_logits = {0: {0.3: 0.5}}
        result = semsearch.compute_combined_relevance(0.5, (0.9,), field_logits)
        assert result == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Store integration: semsearch-enabled query path
# ---------------------------------------------------------------------------


class TestSemsearchQueryPath:
    """Verify SqliteStore.query() routes through semsearch when _ENABLED is True.

    These tests patch semsearch._ENABLED and the async callables
    (_get_embeddings via combined_query, upsert_unit) so the semsearch
    code paths execute without a real embedding server.
    """

    async def test_query_calls_combined_query_when_semsearch_enabled(
        self,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When semsearch is enabled, store.query() delegates to semsearch.combined_query."""
        unit = _make_unit("astronomy", summary="Exoplanet transit photometry")
        await store.insert(unit)
        await store.set_review_status(unit.id, "approved", "reviewer")

        fake_rows = [(unit.model_dump_json(), 0.2)]  # (data_json, cosine_distance)

        monkeypatch.setattr(semsearch, "_ENABLED", True)
        mock_combined = AsyncMock(return_value=fake_rows)
        monkeypatch.setattr(semsearch, "combined_query", mock_combined)
        monkeypatch.setattr(semsearch, "upsert_unit", AsyncMock())

        results = await store.query(["astronomy"])

        mock_combined.assert_awaited_once()
        assert len(results) == 1
        assert results[0].id == unit.id

    async def test_lower_cosine_distance_ranks_first(
        self,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Units with lower cosine distance are boosted and ranked first."""
        u_near = _make_unit("astro", summary="Transit dip exoplanet")
        u_far = _make_unit("astro", summary="Heavy element HII region")
        for u in [u_near, u_far]:
            await store.insert(u)
            await store.set_review_status(u.id, "approved", "reviewer")

        # u_near is semantically closer (lower distance)
        fake_rows = [
            (u_near.model_dump_json(), 0.1),
            (u_far.model_dump_json(), 0.8),
        ]

        monkeypatch.setattr(semsearch, "_ENABLED", True)
        monkeypatch.setattr(semsearch, "combined_query", AsyncMock(return_value=fake_rows))
        monkeypatch.setattr(semsearch, "upsert_unit", AsyncMock())

        results = await store.query(["astro"])

        assert len(results) == 2
        assert results[0].id == u_near.id, "nearer unit should rank first"

    async def test_upsert_called_on_insert_when_semsearch_enabled(
        self,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """store.insert() calls semsearch.upsert_unit when semsearch is enabled."""
        unit = _make_unit("astronomy")

        monkeypatch.setattr(semsearch, "_ENABLED", True)
        mock_upsert = AsyncMock()
        monkeypatch.setattr(semsearch, "upsert_unit", mock_upsert)

        await store.insert(unit)

        mock_upsert.assert_awaited_once()

    async def test_upsert_called_on_update_when_semsearch_enabled(
        self,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """store.update() calls semsearch.upsert_unit when semsearch is enabled."""
        unit = _make_unit("astronomy")
        await store.insert(unit)

        updated = unit.model_copy(update={"insight": Insight(summary="updated", detail="d", action="a")})

        monkeypatch.setattr(semsearch, "_ENABLED", True)
        mock_upsert = AsyncMock()
        monkeypatch.setattr(semsearch, "upsert_unit", mock_upsert)

        await store.update(updated)

        mock_upsert.assert_awaited_once()

    async def test_query_uses_domain_only_path_when_semsearch_disabled(
        self,
        store: SqliteStore,
    ) -> None:
        """When semsearch is disabled (default), store.query() uses the SQL-only path."""
        assert not semsearch.is_enabled(), "semsearch must be disabled for this test"
        unit = _make_unit("astronomy", summary="Exoplanet transit")
        await store.insert(unit)
        await store.set_review_status(unit.id, "approved", "reviewer")

        results = await store.query(["astronomy"])

        assert len(results) == 1
        assert results[0].id == unit.id
