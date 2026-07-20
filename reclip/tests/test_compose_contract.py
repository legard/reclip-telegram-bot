from pathlib import Path


def test_compose_preserves_legacy_download_timeout_when_job_timeout_is_unset():
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()

    assert "JOB_TIMEOUT=${JOB_TIMEOUT:-${DOWNLOAD_TIMEOUT:-9000}}" in compose
