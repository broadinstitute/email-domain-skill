import os

import pytest

from email_domain_scrubber.workbook import AnalysisWorkbook

from .fakes import FakeBackend

#: Alternative to `--live`, for running the live tests from an editor or CI job that cannot
#: easily pass extra pytest arguments.
LIVE_ENV = 'EMAIL_DOMAIN_LIVE_TESTS'


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--live',
        action='store_true',
        default=False,
        help='Run the tests that talk to real Google Sheets and Drive. Needs credentials; '
        'creates and trashes scratch files in the signed-in user\'s My Drive.',
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        'markers', 'live: talks to real Google; needs credentials and --live to run'
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the live tests unless they were asked for.

    Opt-in rather than skip-if-unauthenticated: a live test that quietly skips because a token
    expired would report success for a run that verified nothing against Google.
    """
    if config.getoption('--live') or os.environ.get(LIVE_ENV) == '1':
        return
    skip = pytest.mark.skip(reason=f'live Google test; run with --live or {LIVE_ENV}=1')
    for item in items:
        if 'live' in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def analysis(backend: FakeBackend) -> AnalysisWorkbook:
    return AnalysisWorkbook.create(backend, 'Email Domain Analysis')
