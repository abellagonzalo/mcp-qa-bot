from logfake import end, entry, start

from qa_bot import transcript


def test_parse_entries_and_clean_output():
    log = (
        entry(1, "echo hi; false", b"hi\r\n", code=1)
        + entry(2, "printf 201", b"201")
        + entry(3, "cat <<'SQL'\nselect 1;\nSQL", b"select 1;\r\n")
    )
    t = transcript.parse(log)
    assert [e.id for e in t.entries] == [1, 2, 3]
    assert not t.stray
    e1, e2, e3 = t.entries
    assert e1.command == "echo hi; false" and e1.exit_code == 1 and e1.lines == ["hi"]
    assert e1.duration_ms == 312
    assert e2.lines == ["201"]
    assert e3.command == "cat <<'SQL'\nselect 1;\nSQL" and e3.lines == ["select 1;"]


def test_cleaning_colours_and_carriage_returns():
    raw = b"\x1b[32mOK\x1b[0m\r\n  0%\r 50%\r100%\r\nx\tb\r\n\x1b]0;title\x07done\r\n"
    assert transcript.clean_output(raw) == ["OK", "100%", "x    b", "done"]


def test_forged_marker_inside_output_is_flagged():
    forged = start(9, "rm -rf /") + b"evil\r\n" + end(9)
    log = entry(1, "curl x", b"before\r\n" + forged + b"after\r\n") + entry(2, "echo ok", b"ok\r\n")
    t = transcript.parse(log)
    assert [e.id for e in t.entries] == [1, 2]
    assert [(s.kind, s.inside) for s in t.stray] == [("start", 1), ("end", 1)]
    assert t.entries[0].lines == ["before", "evil", "after"]


def test_unterminated_entry():
    t = transcript.parse(entry(1, "a", b"x\r\n") + start(2, "sleep 100") + b"partial\r\n")
    assert t.entries[1].exit_code is None
    assert t.entries[1].lines == ["partial"]
    assert t.entries[1].flags

