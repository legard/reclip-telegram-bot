import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preferences import PreferenceStore


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
