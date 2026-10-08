import asyncio
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ecommerce_scraper.cli import DEFAULT_START_URL, main
from tests.support import FakeSite, listing_html, product_html

ROOT = "https://shop.test/store"

HDD_SWATCHES = (
    '<div class="swatches">'
    '<button class="btn swatch active" value="128">128</button>'
    '<button class="btn swatch" value="256">256</button>'
    '<button class="btn swatch" value="1024" disabled>1024</button>'
    "</div>"
)
COLOR_SELECT = (
    '<div class="dropdown"><select aria-label="color">'
    '<option value="">Select color</option>'
    '<option value="Gold">Gold</option><option value="White">White</option>'
    "</select></div>"
)


def store() -> FakeSite:
    return FakeSite(
        {
            ROOT: listing_html(products=["/store/product/2", "/store/product/1"]),
            f"{ROOT}/product/1": product_html("Laptop", "$100.50", extra=HDD_SWATCHES),
            f"{ROOT}/product/2": product_html("Phone", "$9.99", extra=COLOR_SELECT),
        }
    )


def run(site: FakeSite, *args: str) -> int:
    defaults = ["--start-url", ROOT, "--max-attempts", "1", "--rate-limit", "0"]
    return main([*defaults, *args], transport=site.transport())


def test_prints_the_report_as_json_on_stdout_and_logs_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(store())

    out, err = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(out) == {
        "results": [
            {"name": "Laptop 128 GB", "description": "About Laptop", "price": 100.5},
            {"name": "Laptop 256 GB", "description": "About Laptop", "price": 120.5},
            {
                "name": "Phone",
                "description": "About Phone",
                "price": 9.99,
                "colors": ["gold", "white"],
            },
        ],
        "total": 230.99,
    }
    assert "3 results" in err


def test_writes_to_output_file_instead_of_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "report.json"

    exit_code = run(store(), "--output", str(destination))

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert json.loads(destination.read_text())["total"] == 230.99
    assert list(tmp_path.iterdir()) == [destination]  # no temp files left behind


def test_failed_scrape_exits_nonzero_and_keeps_previous_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = store()
    site.pages[f"{ROOT}/product/2"] = 500
    destination = tmp_path / "report.json"
    destination.write_text("previous good report")

    exit_code = run(site, "--output", str(destination))

    out, err = capsys.readouterr()
    assert exit_code == 1
    assert out == ""
    assert "/store/product/2" in err
    assert destination.read_text() == "previous good report"


def test_missing_output_directory_is_rejected_before_any_request(tmp_path: Path) -> None:
    site = store()

    with pytest.raises(SystemExit) as exc_info:
        run(site, "--output", str(tmp_path / "missing" / "report.json"))

    assert exc_info.value.code == 2
    assert not site.hits


@pytest.mark.parametrize(
    "args",
    [
        ["--concurrency", "0"],
        ["--concurrency", "many"],
        ["--max-attempts", "0"],
        ["--max-pages", "0"],
        ["--timeout", "0"],
        ["--timeout", "-5"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--rate-limit", "-1"],
        ["--rate-limit", "nan"],
        ["--deadline", "0"],
        ["--start-url", "ftp://shop.test/store"],
        ["--start-url", "not a url"],
    ],
)
def test_invalid_options_are_usage_errors(args: list[str]) -> None:
    site = store()

    with pytest.raises(SystemExit) as exc_info:
        run(site, *args)

    assert exc_info.value.code == 2
    assert not site.hits


def test_compact_output_is_a_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    run(store(), "--compact")

    assert capsys.readouterr().out.count("\n") == 1


def test_identifies_itself_with_a_descriptive_user_agent() -> None:
    site = store()

    run(site)

    (user_agent,) = site.user_agents
    assert user_agent.startswith("ecommerce-scraper/")
    assert "(+https://github.com/" in user_agent  # how a site operator can reach us


@pytest.mark.parametrize("make_destination", ["directory", "empty string"])
def test_output_that_cannot_be_a_file_is_rejected_before_any_request(
    tmp_path: Path, make_destination: str
) -> None:
    if make_destination == "directory":
        destination = tmp_path / "report.json"
        destination.mkdir()
        output = str(destination)
    else:
        output = ""
    site = store()

    with pytest.raises(SystemExit) as exc_info:
        run(site, "--output", output)

    assert exc_info.value.code == 2
    assert not site.hits


def test_new_output_file_gets_the_usual_permissions(tmp_path: Path) -> None:
    # Readable by e.g. a web server or sidecar, like a file created by `>` would be.
    destination = tmp_path / "report.json"
    previous_umask = os.umask(0o022)
    try:
        run(store(), "--output", str(destination))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o644


def test_existing_output_file_keeps_its_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    destination.write_text("old")
    destination.chmod(0o640)

    run(store(), "--output", str(destination))

    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert json.loads(destination.read_text())["total"] == 230.99


def test_report_is_flushed_to_disk_before_it_replaces_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "report.json"
    destination.write_text("previous good report")
    synced: list[str] = []

    def record_fsync(fd: int) -> None:
        synced.append(destination.read_text())  # the old report is still in place

    monkeypatch.setattr("ecommerce_scraper.output.os.fsync", record_fsync)

    assert run(store(), "--output", str(destination)) == 0
    assert synced == ["previous good report"]


def test_disk_full_during_write_keeps_old_report_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "report.json"
    destination.write_text("previous good report")

    def disk_full(fd: int) -> None:  # space typically runs out when data is flushed
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("ecommerce_scraper.output.os.fsync", disk_full)

    assert run(store(), "--output", str(destination)) == 1
    assert "No space left on device" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [destination]
    assert destination.read_text() == "previous good report"


def test_output_to_a_device_such_as_dev_null_is_written_directly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Devices and pipes cannot be atomically replaced; writing through them is fine.
    exit_code = run(store(), "--output", os.devnull)

    assert exit_code == 0
    assert "Could not write" not in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_unwritable_output_directory_is_rejected_before_any_request(tmp_path: Path) -> None:
    # e.g. a non-root container writing into a host directory it doesn't own.
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    site = store()

    try:
        with pytest.raises(SystemExit) as exc_info:
            run(site, "--output", str(locked / "report.json"))
    finally:
        locked.chmod(0o755)

    assert exc_info.value.code == 2
    assert not site.hits


def test_rate_limit_option_spaces_out_requests() -> None:
    site = store()  # three pages: the listing and two products
    started = time.monotonic()

    run(site, "--rate-limit", "5")

    # Starts at 0, 0.2 and 0.4 s at the earliest; a lower bound is not timing-flaky.
    assert time.monotonic() - started >= 0.4
    assert sum(site.hits.values()) == 3


def test_log_level_is_case_insensitive(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(store(), "--log-level", "debug") == 0
    assert "GET https://shop.test/store" in capsys.readouterr().err


def test_ctrl_c_exits_130_without_writing_a_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ecommerce_scraper.cli.scrape", interrupted)

    assert run(store()) == 130
    out, err = capsys.readouterr()
    assert out == ""
    assert "Interrupted" in err


def test_help_shows_meaningful_defaults_only(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    help_text = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "(default: 10.0)" in help_text  # --rate-limit
    assert "default: False" not in help_text


def test_runs_as_a_python_module() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ecommerce_scraper", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("ecommerce-scraper ")


def test_deadline_bounds_the_whole_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def stalls(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(30)  # e.g. a server trickling bytes just under the timeout

    monkeypatch.setattr("ecommerce_scraper.cli.scrape", stalls)
    started = time.monotonic()

    exit_code = run(store(), "--deadline", "0.2")

    assert exit_code == 1
    assert time.monotonic() - started < 5
    assert "0.2s deadline" in capsys.readouterr().err


def test_internal_errors_exit_70_with_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def buggy(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug in our code")

    monkeypatch.setattr("ecommerce_scraper.cli.scrape", buggy)

    assert run(store()) == 70  # distinct from 1, which means "the site let us down"
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "bug in our code" in err


def test_help_shows_the_actual_start_url(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert DEFAULT_START_URL in "".join(capsys.readouterr().out.split())


def test_a_report_that_cannot_be_rendered_exactly_is_an_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = store()
    site.pages[f"{ROOT}/product/2"] = product_html("Phone", "$12345678901234567.89")

    assert run(site) == 70  # a JSON float cannot carry 19 significant digits
    out, err = capsys.readouterr()
    assert out == ""
    assert "Traceback" in err


def test_sigterm_exits_143_without_writing_a_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Schedulers such as Kubernetes stop a job with SIGTERM before killing it.
    async def terminated_mid_run(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(5)

    monkeypatch.setattr("ecommerce_scraper.cli.scrape", terminated_mid_run)
    handler_before = signal.getsignal(signal.SIGTERM)
    destination = tmp_path / "report.json"

    assert run(store(), "--output", str(destination)) == 143
    assert not destination.exists()
    assert "Terminated" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) == handler_before  # restored for the caller
