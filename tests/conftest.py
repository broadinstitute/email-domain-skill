from pathlib import Path

import pytest

from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV, WORKDIR_ENV, Staging
from email_domain_scrubber.workbook import AnalysisWorkbook

from .fakes import Recorder


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's real work directory."""
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / 'work'))
    monkeypatch.delenv(ANALYSIS_WORKBOOK_ENV, raising=False)


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
