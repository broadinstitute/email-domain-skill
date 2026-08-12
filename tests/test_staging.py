"""The local work directory: paths, name safety, and download freshness."""

from pathlib import Path

from email_domain_scrubber.staging import (
    ANALYSIS_WORKBOOK_ENV,
    WORKDIR_ENV,
    Staging,
    analysis_workbook_path,
    safe_name,
    workdir,
)


def test_the_work_directory_honours_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / 'elsewhere'))

    assert workdir() == tmp_path / 'elsewhere'
    assert workdir().is_dir()


def test_the_work_directory_expands_a_tilde(monkeypatch):
    monkeypatch.setenv(WORKDIR_ENV, '~/scrubber-test-dir')
    assert str(workdir()).startswith(str(Path.home()))


def test_the_analysis_path_prefers_the_argument(tmp_path, monkeypatch):
    monkeypatch.setenv(ANALYSIS_WORKBOOK_ENV, str(tmp_path / 'from-env.xlsx'))

    assert analysis_workbook_path(str(tmp_path / 'explicit.xlsx')) == tmp_path / 'explicit.xlsx'


def test_the_analysis_path_falls_back_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ANALYSIS_WORKBOOK_ENV, str(tmp_path / 'from-env.xlsx'))

    assert analysis_workbook_path() == tmp_path / 'from-env.xlsx'
    assert analysis_workbook_path('') == tmp_path / 'from-env.xlsx'


def test_the_analysis_path_defaults_into_the_work_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(WORKDIR_ENV, str(tmp_path / 'work'))

    assert analysis_workbook_path() == tmp_path / 'work' / 'analysis.xlsx'


def test_awkward_names_are_made_safe():
    assert safe_name('Q1/Q2: metrics *final*.xlsx') == 'Q1_Q2_ metrics _final_.xlsx'
    assert '/' not in safe_name('a/b/c')


def test_path_traversal_is_defused():
    """A workbook name is user-supplied; flattening it must never escape the work directory."""
    for hostile in ('../../etc/passwd', '..', '../secrets', '/etc/passwd'):
        cleaned = safe_name(hostile)
        assert '/' not in cleaned
        assert not cleaned.startswith('.')
        assert (Path('/base') / cleaned).parent == Path('/base')


def test_an_empty_name_still_yields_something_writable():
    assert safe_name('') == 'workbook'
    assert safe_name('...') == 'workbook'


def test_long_names_are_truncated():
    assert len(safe_name('x' * 500)) == 120


def test_the_anonymized_path_never_overwrites(tmp_path):
    staging = Staging(tmp_path)

    first = staging.anonymized_path('/reports/Q1 Metrics.xlsx', 'Q1 Metrics.xlsx')
    assert first.name == 'Q1 Metrics (anonymized).xlsx'

    first.write_bytes(b'already shared')
    second = staging.anonymized_path('/reports/Q1 Metrics.xlsx', 'Q1 Metrics.xlsx')

    assert second.name == 'Q1 Metrics (anonymized) 2.xlsx'


def test_copies_are_kept_apart_by_source_path(tmp_path):
    """Two same-named reports from different folders must not collide in the work directory."""
    staging = Staging(tmp_path)

    first = staging.anonymized_path('/a/Metrics.xlsx', 'Metrics.xlsx')
    second = staging.anonymized_path('/b/Metrics.xlsx', 'Metrics.xlsx')

    assert first != second
    assert first.name == second.name
