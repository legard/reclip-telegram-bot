import aiosqlite


class PreferenceStore:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    user_id INTEGER PRIMARY KEY,
                    format TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    audio_mode TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def get(self, user_id: int) -> dict[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT format, quality, audio_mode FROM preferences WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return {"format": row[0], "quality": row[1], "audio_mode": row[2]}

    async def save(
        self, user_id: int, *, format: str, quality: str, audio_mode: str
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO preferences (user_id, format, quality, audio_mode)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    format = excluded.format,
                    quality = excluded.quality,
                    audio_mode = excluded.audio_mode
                """,
                (user_id, format, quality, audio_mode),
            )
            await db.commit()

    async def clear(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM preferences WHERE user_id = ?", (user_id,))
            await db.commit()
