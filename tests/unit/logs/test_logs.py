"""Server logs, read as PostgreSQL 16 wrote them.

The fixtures are real server output, excerpted, not edited:

- ``pg16-stderr.log``: the test server's log (``log_line_prefix = '%m [%p] '``),
  with checkpoint warnings and autovacuum skips it logged on its own, and a
  deadlock, lock waits (``log_lock_waits`` set for the waiting sessions),
  temporary files and an autovacuum run provoked in a scratch database.
- ``pg16-user-at-db.log``, ``pg16.csv``, ``pg16.json``: one throwaway server
  writing ``stderr,csvlog,jsonlog`` at once with ``'%m [%p] %q%u@%d '``, so the
  three formats hold the same events.
"""

import gzip
from pathlib import Path

import pytest

from pg_debug.errors import InvalidArgumentError
from pg_debug.logs import analyze_log_lines
from pg_debug.logs import analyze_logs
from pg_debug.logs import prefix_pattern
from pg_debug.logs.parse import detect_format

FIXTURES = Path(__file__).parent / "fixtures"
SAME_EVENTS = ["pg16-user-at-db.log", "pg16.csv", "pg16.json"]


def read(name, **options):
    return analyze_logs([FIXTURES / name], **options)


@pytest.mark.parametrize(
    ("name", "fmt"), [("pg16-stderr.log", "stderr"), ("pg16-user-at-db.log", "stderr"), ("pg16.csv", "csvlog"), ("pg16.json", "jsonlog")]
)
def test_the_format_is_detected(name, fmt):
    report = read(name)
    assert list(report.sources.values()) == [fmt]
    assert report.unparsed == 0


@pytest.mark.parametrize("name", SAME_EVENTS)
def test_a_deadlock_names_both_processes_their_queries_and_the_relation(name):
    [deadlock] = read(name).deadlocks

    assert deadlock.cancelled_pid == 4344
    assert [(p.pid, p.waits_for, p.on, p.blocked_by) for p in deadlock.processes] == [
        (4344, "ShareLock", "transaction 747", 4335),
        (4335, "ShareLock", "transaction 748", 4344),
    ]
    assert deadlock.processes[1].query == "UPDATE accounts SET balance = balance + 10 WHERE id = 2;"
    assert deadlock.relations == ["accounts"]
    assert (deadlock.database, deadlock.user) == ("shop", "postgres")
    assert deadlock.statement == "UPDATE accounts SET balance = balance + 10 WHERE id = 1;"


@pytest.mark.parametrize("name", SAME_EVENTS)
def test_lock_waits_say_how_long_behind_whom_and_how_they_ended(name):
    waits = read(name).lock_waits

    assert [(w.pid, w.outcome) for w in waits] == [(4344, "deadlock"), (4359, "acquired"), (4375, "cancelled")]
    acquired = waits[1]
    assert (acquired.mode, acquired.target, acquired.waited_ms, acquired.holders) == (
        "AccessShareLock",
        "relation 16389 of database 16388",
        1706.342,
        [4347],
    )
    assert waits[2].cancelled_by == "canceling statement due to statement timeout"


@pytest.mark.parametrize("name", SAME_EVENTS)
def test_temporary_files_are_summed_per_statement(name):
    [use] = read(name).temp_files

    assert (use.files, use.total_bytes, use.largest_bytes) == (2, 8069120 + 2800000, 8069120)
    assert use.statement is not None and use.statement.startswith("SELECT count(*) FROM (SELECT md5(g::text)")


def test_skipped_vacuums_tell_autovacuum_from_a_manual_command():
    skips = read("pg16-stderr.log").skipped_vacuums

    assert [(s.relation, s.by, s.times) for s in skips] == [("pp_big", "autovacuum", 4), ("accounts", "a manual command", 1)]
    assert skips[0].reason == "lock not available"


@pytest.mark.parametrize("name", [*SAME_EVENTS, "pg16-stderr.log"])
def test_autovacuum_runs_carry_their_elapsed_time_and_tuples(name):
    runs = {(r.table, r.operation): r for r in read(name).autovacuum}

    if name == "pg16-stderr.log":
        vacuum = runs[("pgdebug_scratch_logs.public.events", "vacuum")]
        assert (vacuum.runs, vacuum.longest_seconds, vacuum.tuples_removed, vacuum.dead_not_removable) == (1, 0.01, 10000, 0)
        assert ("pgdebug_scratch_logs.public.events", "analyze") in runs
    else:
        assert runs[("shop.public.filler", "vacuum")].longest_seconds == 4.31


@pytest.mark.parametrize("name", SAME_EVENTS)
def test_checkpoint_warnings_and_what_started_each_checkpoint(name):
    report = read(name)

    assert [w.seconds_apart for w in report.checkpoint_warnings] == [23, 0, 0, 0, 1, 0]
    assert report.checkpoints["wal"] == 6


def test_the_findings_carry_their_rule_and_where_they_were_read():
    payload = read("pg16-stderr.log").to_dict()

    assert payload["deadlocks"][0]["rule"] == "deadlock"
    assert payload["deadlocks"][0]["docs"].endswith("rules.md#deadlock")
    assert payload["deadlocks"][0]["source"].endswith("pg16-stderr.log:48")
    assert payload["checkpoint_warnings"][0]["source"].endswith("pg16-stderr.log:9")


def test_what_the_log_cannot_show_is_said():
    report = analyze_log_lines(["2026-10-14 21:35:28.002 UTC [9179] LOG:  database system is ready to accept connections"])

    assert report.entries == 1
    assert any("log_lock_waits" in note for note in report.notes)
    assert any("log_temp_files" in note for note in report.notes)
    assert any("lc_messages" in note for note in report.notes)


def test_a_log_nothing_could_be_read_from_says_so():
    report = analyze_log_lines(["Oct 14 21:35:28 host postgres[9179]: something"])

    assert report.entries == 0 and report.unparsed == 1
    assert "--prefix" in report.notes[0]


def test_a_given_prefix_reads_every_field_it_holds():
    pattern = prefix_pattern("%m [%p] %q%u@%d ")

    with_session = pattern.match("2026-10-14 21:48:16.807 UTC [4359] postgres@shop LOG:  process 4359 still waiting")
    background = pattern.match("2026-10-14 21:48:23.152 UTC [2682] LOG:  checkpoints are occurring too frequently (23 seconds apart)")

    assert with_session is not None and with_session.group("user", "database", "pid") == ("postgres", "shop", "4359")
    assert background is not None and background.group("database") is None and background.group("level") == "LOG"


def test_a_prefix_with_an_escape_postgresql_does_not_have_is_refused():
    with pytest.raises(InvalidArgumentError, match="%z"):
        prefix_pattern("%t %z ")


def test_the_same_log_read_with_its_prefix_gives_the_same_report():
    detected = read("pg16-user-at-db.log").to_dict()
    given = read("pg16-user-at-db.log", prefix="%m [%p] %q%u@%d ").to_dict()

    assert given == detected


def test_a_compressed_log_is_read(tmp_path):
    path = tmp_path / "postgresql.log.gz"
    path.write_bytes(gzip.compress((FIXTURES / "pg16-stderr.log").read_bytes()))

    assert len(analyze_logs([path]).deadlocks) == 1


def test_a_missing_file_is_an_error():
    with pytest.raises(InvalidArgumentError, match="Could not read"):
        analyze_logs(["/nonexistent/postgresql.log"])


def test_detection_falls_back_to_stderr():
    assert detect_format(["", "plain text"]) == "stderr"
