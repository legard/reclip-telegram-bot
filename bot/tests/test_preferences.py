import asyncio
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preferences import PreferenceStore


@pytest.mark.asyncio
async def test_initialize_creates_user_preferences_schema_with_updated_at(tmp_path):
    store = PreferenceStore(str(tmp_path / "bot.db"))

    await store.initialize()

    async with aiosqlite.connect(store.path) as db:
        cursor = await db.execute("PRAGMA table_info(user_preferences)")
        columns = {row[1]: row[2] for row in await cursor.fetchall()}

    assert columns == {
        "user_id": "INTEGER",
        "format": "TEXT",
        "quality": "TEXT",
        "audio_mode": "TEXT",
        "updated_at": "TEXT",
    }


@pytest.mark.asyncio
async def test_preferences_survive_a_new_store_instance(tmp_path):
    first = PreferenceStore(str(tmp_path / "bot.db"))
    await first.initialize()
    await first.save(42, format="video", quality="720", audio_mode="ru_if_available")

    second = PreferenceStore(str(tmp_path / "bot.db"))

    assert await second.get(42) == {
        "format": "video",
        "quality": "720",
        "audio_mode": "ru_if_available",
    }


@pytest.mark.asyncio
async def test_clear_removes_saved_preferences(tmp_path):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()
    await store.save(42, format="audio", quality="best", audio_mode="original")

    await store.clear(42)

    assert await store.get(42) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preferences",
    [
        {"format": "document", "quality": "best", "audio_mode": "original"},
        {"format": "video", "quality": "144", "audio_mode": "original"},
        {"format": "audio", "quality": "best", "audio_mode": "russian"},
    ],
)
async def test_save_rejects_preferences_outside_the_supported_values(tmp_path, preferences):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()

    with pytest.raises(ValueError):
        await store.save(42, **preferences)


@pytest.mark.asyncio
async def test_concurrent_partial_updates_preserve_quality_and_format(tmp_path):
    store = PreferenceStore(str(tmp_path / "bot.db"))
    await store.initialize()

    await asyncio.gather(
        store.update(42, quality="720"),
        store.update(42, format="audio"),
    )

    assert await store.get(42) == {
        "format": "audio",
        "quality": "720",
        "audio_mode": "original",
    }
