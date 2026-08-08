import aiosqlite

_FORMATS = frozenset({"video", "audio"})
_QUALITIES = frozenset({"best", "1080", "720", "480", "360"})
_AUDIO_MODES = frozenset({"original", "ru_if_available"})


class PreferenceStore:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    format TEXT NOT NULL CHECK(format IN ('video', 'audio')),
                    quality TEXT NOT NULL CHECK(quality IN ('best', '1080', '720', '480', '360')),
                    audio_mode TEXT NOT NULL CHECK(audio_mode IN ('original', 'ru_if_available')),
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'preferences'"
            )
            if await cursor.fetchone():
                await db.execute(
                    """
                    INSERT OR IGNORE INTO user_preferences (
                        user_id, format, quality, audio_mode, updated_at
                    )
                    SELECT user_id, format, quality, audio_mode,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    FROM preferences
                    """
                )
            await db.commit()

    async def get(self, user_id: int) -> dict[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT format, quality, audio_mode FROM user_preferences WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {"format": row[0], "quality": row[1], "audio_mode": row[2]}

    async def save(
        self, user_id: int, *, format: str, quality: str, audio_mode: str
    ) -> None:
        await self.update(
            user_id, format=format, quality=quality, audio_mode=audio_mode
        )

    async def update(
        self,
        user_id: int,
        *,
        format: str | None = None,
        quality: str | None = None,
        audio_mode: str | None = None,
    ) -> None:
        if format is not None and format not in _FORMATS:
            raise ValueError("format must be video or audio")
        if quality is not None and quality not in _QUALITIES:
            raise ValueError("quality must be best, 1080, 720, 480, or 360")
        if audio_mode is not None and audio_mode not in _AUDIO_MODES:
            raise ValueError("audio_mode must be original or ru_if_available")

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO user_preferences (
                    user_id, format, quality, audio_mode, updated_at
                )
                VALUES (
                    ?, COALESCE(?, 'video'), COALESCE(?, 'best'),
                    COALESCE(?, 'original'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
                ON CONFLICT(user_id) DO UPDATE SET
                    format = COALESCE(?, format),
                    quality = COALESCE(?, quality),
                    audio_mode = COALESCE(?, audio_mode),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (user_id, format, quality, audio_mode, format, quality, audio_mode),
            )
            await db.commit()

    async def clear(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
            await db.commit()
