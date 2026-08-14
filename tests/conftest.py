from pathlib import Path

import pytest

from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV, WORKDIR_ENV, Staging
from email_domain_scrubber.workbook import AnalysisWorkbook


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's real work directory."""
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / 'work'))
    monkeypatch.delenv(ANALYSIS_WORKBOOK_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly rather than reaching a real API if a test forgets to inject a fetcher."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError('a test tried to open a network connection; inject a fetch instead')

    monkeypatch.setattr('urllib.request.urlopen', refuse)


@pytest.fixture
def staging(tmp_path: Path) -> Staging:
    return Staging(tmp_path / 'work')


@pytest.fixture
def analysis(tmp_path: Path) -> AnalysisWorkbook:
    return AnalysisWorkbook.open(tmp_path / 'analysis.xlsx')
