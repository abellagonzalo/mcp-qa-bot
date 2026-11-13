import shlex

from qa_bot import hooks


def test_markers_round_trip_through_scan():
    log = (
        b"noise"
        + hooks.encode_start(3, 1_700_000_000.25, "echo 'a\x07b'\x1b;x")
        + b"out\r\n"
        + hooks.encode_end(3, 42, 1_700_000_001.5)
    )
    start, end = hooks.scan(log)
    assert start == hooks.Start("3", 1_700_000_000.25, "echo 'ab';x", 5, start.end)
    assert log[start.end : end.offset] == b"out\r\n"
    assert (end.id, end.exit_code, end.epoch, end.end) == ("3", 42, 1_700_000_001.5, len(log))


def test_scan_tolerates_unreadable_fields():
    start, end = hooks.scan(b"\x1b]777;qa;start;x\x07\x1b]777;qa;end;1;oops\x1b\\")
    assert (start.id, start.epoch, start.command) == ("x", 0.0, "")
    assert (end.exit_code, end.epoch) == (None, None)


def test_source_line_passes_the_env_keys(tmp_path):
    words = shlex.split(hooks.source_line(tmp_path / "rt dir", "chan"))
    assert words == ["source", str(hooks.SCRIPT), str(tmp_path / "rt dir"), "chan", *hooks.ENV_KEYS]
    assert set(hooks.DB_ENV_KEYS) | {"KUBECONFIG", "PWD"} <= set(hooks.ENV_KEYS)


def test_runtime_reads_the_side_channels(tmp_path):
    rt = hooks.Runtime(tmp_path)
    assert (rt.state(), rt.last_done(), rt.env()) == ("unknown", 0, {})
    (tmp_path / hooks.STATE_FILE).write_text("running")
    (tmp_path / hooks.DONE_FILE).write_text("7")
    (tmp_path / hooks.ENV_FILE).write_text("PGHOST=db.staging\nPGSERVICE=\nKUBECONFIG=/a=b\njunk\n")
    assert (rt.state(), rt.last_done()) == ("running", 7)
    assert rt.env() == {"PGHOST": "db.staging", "KUBECONFIG": "/a=b"}
