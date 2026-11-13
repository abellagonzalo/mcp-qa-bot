import pytest

from qa_bot import tools

SYSTEM = """\
Prefer read-only tools.

## HTTP to in-cluster services
`kubectl curl`, not plain `curl`.

## DB reads
`psql -h <host>`
"""

PROJECT = """\
Project notes.

## db reads
`dbtool -r <db>`

## Auth check
`tsh status`
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_merge_project_overrides_by_heading_and_appends():
    merged = tools.merge(SYSTEM, PROJECT)
    assert merged == (
        "Prefer read-only tools.\n\n"
        "Project notes.\n\n"
        "## HTTP to in-cluster services\n`kubectl curl`, not plain `curl`.\n\n"
        "## db reads\n`dbtool -r <db>`\n\n"
        "## Auth check\n`tsh status`"
    )


def test_merge_with_one_side_empty():
    assert tools.merge(SYSTEM, "") == SYSTEM.strip()
    assert tools.merge("", PROJECT) == PROJECT.strip()
    assert tools.merge("", "") == ""


def test_load_system_and_nearest_project_file(home):
    sys_file = write(home / "config" / "qa-bot" / "tools.md", SYSTEM)
    proj_file = write(home / "repo" / ".qa-bot" / "tools.md", PROJECT)
    session_dir = home / "repo" / "qa" / "pay-123"  # not created yet
    loaded = tools.load(session_dir)
    assert loaded.sources == [sys_file, proj_file.resolve()]
    assert "`dbtool -r <db>`" in loaded.text and "`psql -h <host>`" not in loaded.text
    out = loaded.describe()
    assert out.startswith("Preferred tools (guidance, not enforced; from ")
    assert "## Auth check" in out


def test_load_nothing_configured(home):
    loaded = tools.load(home / "elsewhere")
    assert loaded.text == "" and loaded.sources == []
    assert "none configured" in loaded.describe()


def test_load_system_only(home):
    write(home / "config" / "qa-bot" / "tools.md", SYSTEM)
    assert tools.load(None).text == SYSTEM.strip()
