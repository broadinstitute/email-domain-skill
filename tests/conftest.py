import pytest

from email_domain_scrubber.workbook import AnalysisWorkbook

from .fakes import FakeBackend


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def analysis(backend: FakeBackend) -> AnalysisWorkbook:
    return AnalysisWorkbook.create(backend, 'Email Domain Analysis')
