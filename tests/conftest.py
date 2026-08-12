import os
from pathlib import Path

import pytest

from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV, WORKDIR_ENV, Staging
from email_domain_scrubber.workbook import AnalysisWorkbook

from .fakes import FakeDrive, Recorder

#: Alternative to `--live`, for running the live tests from an editor or CI job that cannot
#: easily pass extra pytest arguments.
LIVE_ENV = 'EMAIL_DOMAIN_LIVE_TESTS'


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--live',
        action='store_true',
        default=False,
        help='Run the tests that talk to the real Google Drive MCP connector. Needs credentials; '
        "creates scratch files in the signed-in user's My Drive.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        'markers', 'live: talks to the real Drive MCP connector; needs credentials and --live'
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the live tests unless they were asked for.

    Opt-in rather than skip-if-unauthenticated: a live test that quietly skips because a token
    expired would report success for a run that verified nothing against Google.
    """
    if config.getoption('--live') or os.environ.get(LIVE_ENV) == '1':
        return
    skip = pytest.mark.skip(reason=f'live Drive MCP test; run with --live or {LIVE_ENV}=1')
    for item in items:
        if 'live' in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's real work directory, credentials, and Drive."""
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / 'work'))
    monkeypatch.delenv(ANALYSIS_WORKBOOK_ENV, raising=False)
    monkeypatch.delenv('EMAIL_DOMAIN_RCLONE_REMOTE', raising=False)


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def staging(tmp_path: Path) -> Staging:
    return Staging(tmp_path / 'work')


@pytest.fixture
def analysis(tmp_path: Path) -> AnalysisWorkbook:
    return AnalysisWorkbook.open(tmp_path / 'analysis.xlsx')


@pytest.fixture
def excel() -> Recorder:
    """Stands in for the Excel MCP server."""
    return Recorder()
