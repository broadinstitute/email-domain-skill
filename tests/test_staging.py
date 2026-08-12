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
    """A Drive file name is attacker-controlled if a report was shared in; it must stay a name."""
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


def test_the_workbook_path_always_ends_in_xlsx(tmp_path):
    staging = Staging(tmp_path)

    assert staging.workbook_path('id1', 'Q1 Metrics.xlsx').name == 'Q1 Metrics.xlsx'
    assert staging.workbook_path('id1', 'Q1 Metrics').name == 'Q1 Metrics.xlsx'
    assert staging.workbook_path('id1', 'Q1.XLSX').name == 'Q1.xlsx'


def test_workbooks_are_kept_apart_by_drive_id(tmp_path):
    staging = Staging(tmp_path)

    first = staging.workbook_path('id1', 'Metrics.xlsx')
    second = staging.workbook_path('id2', 'Metrics.xlsx')

    assert first != second
    assert first.name == second.name


def test_the_anonymized_path_never_overwrites(tmp_path):
    staging = Staging(tmp_path)

    first = staging.anonymized_path('id1', 'Q1 Metrics.xlsx')
    assert first.name == 'Q1 Metrics (anonymized).xlsx'

    first.write_bytes(b'published already')
    second = staging.anonymized_path('id1', 'Q1 Metrics.xlsx')

    assert second.name == 'Q1 Metrics (anonymized) 2.xlsx'


def test_freshness_needs_both_the_file_and_a_matching_marker(tmp_path):
    staging = Staging(tmp_path)
    path = staging.workbook_path('id1', 'Q1.xlsx')

    assert not staging.is_current(path, '2026-01-01T00:00:00Z')

    staging.write(path, b'content', '2026-01-01T00:00:00Z')
    assert staging.is_current(path, '2026-01-01T00:00:00Z')
    assert not staging.is_current(path, '2026-06-01T00:00:00Z')


def test_an_empty_modified_time_is_never_current(tmp_path):
    """Freshness cannot be proven without one, so re-download rather than serve a stale file."""
    staging = Staging(tmp_path)
    path = staging.write(staging.workbook_path('id1', 'Q1.xlsx'), b'content', '')

    assert not staging.is_current(path, '')


def test_write_returns_the_path_and_stores_the_bytes(tmp_path):
    staging = Staging(tmp_path)

    path = staging.write(staging.workbook_path('id1', 'Q1.xlsx'), b'abc', '2026-01-01T00:00:00Z')

    assert path.read_bytes() == b'abc'
