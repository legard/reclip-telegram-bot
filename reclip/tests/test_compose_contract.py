import json
from pathlib import Path
import subprocess



def test_compose_supplies_required_bot_access_and_database_environment():
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json", "--no-interpolate"],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    compose = json.loads(result.stdout)

    bot = compose["services"]["bot"]
    assert "ALLOWED_USER_IDS=${ALLOWED_USER_IDS}" in bot["environment"]
    assert "BOT_DB_PATH=/data/bot.db" in bot["environment"]
    mounts = [f"{volume['source']}:{volume['target']}" for volume in bot["volumes"]]
    assert "bot-data:/data" in mounts


def test_compose_preserves_legacy_download_timeout_when_job_timeout_is_unset():
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()

    assert "JOB_TIMEOUT=${JOB_TIMEOUT:-${DOWNLOAD_TIMEOUT:-9000}}" in compose
