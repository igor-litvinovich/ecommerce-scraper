import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ecommerce_scraper.validate import main, validate_report


def valid_report() -> dict[str, Any]:
    return {
        "results": [
            {"name": "Dell Latitude 5580 128 GB", "description": "15.6in", "price": 1178.19},
            {
                "name": "Samsung Galaxy",
                "description": "5 mpx. Android 5.0",
                "price": 93.99,
                "colors": ["gold", "white", "black"],
            },
            {"name": "MSI GL72M 7RDX 128 GB", "description": "17.3in", "price": 1099},
        ],
        "total": 2371.18,
    }


def with_change(path: str, value: Any) -> dict[str, Any]:
    """A valid report with one field replaced (``"results.1.colors"``) or removed (...)."""
    report = valid_report()
    *parents, last = path.split(".")
    target: Any = report
    for key in parents:
        target = target[int(key)] if key.isdigit() else target[key]
    if value is ...:
        del target[last]
    else:
        target[int(last) if last.isdigit() else last] = value
    return report


def test_a_valid_report_has_no_problems() -> None:
    assert validate_report(valid_report()) == []


def test_exact_decimal_values_are_accepted() -> None:
    report = json.loads(json.dumps(valid_report()), parse_float=Decimal)

    assert validate_report(report) == []


@pytest.mark.parametrize(
    ("document", "problem"),
    [
        ([], "report must be a JSON object"),
        ({"results": []}, "missing 'total'"),
        ({"results": [], "total": 0, "count": 0}, "unexpected field 'count'"),
        ({"results": {}, "total": 0}, "'results' must be an array"),
        ({"results": [], "total": 0}, "'results' is empty"),
        (with_change("total", "2371.18"), "'total' must be a number"),
        (with_change("total", 2000), "'total' is 2000 but the prices sum to 2371.18"),
        (with_change("results.0.name", "  "), "results[0].name must be a non-empty string"),
        (with_change("results.0.name", ...), "results[0] is missing 'name'"),
        (with_change("results.0.description", None), "results[0].description must be a string"),
        (with_change("results.0.price", "1178.19"), "results[0].price must be a number"),
        (with_change("results.0.price", True), "results[0].price must be a number"),
        (with_change("results.0.price", 0), "results[0].price must be positive"),
        (with_change("results.0.price", 1178.195), "results[0].price has more than 2 decimals"),
        (with_change("results.0.url", "https://x"), "results[0] has unexpected field 'url'"),
        (with_change("results.1.colors", ["gold"]), "results[1].colors needs at least 2"),
        (with_change("results.1.colors", ["Gold", "white"]), "results[1].colors must be lowercase"),
        (with_change("results.1.colors", ["gold", "gold"]), "results[1].colors has duplicates"),
        (with_change("results.1.colors", "gold"), "results[1].colors must be an array of strings"),
        (with_change("results.2", "MSI"), "results[2] must be an object"),
    ],
)
def test_reports_each_contract_violation(document: Any, problem: str) -> None:
    assert any(problem in found for found in validate_report(document)), validate_report(document)


def test_duplicate_entries_are_reported() -> None:
    report = valid_report()
    report["results"].append(dict(report["results"][0]))
    report["total"] = 3549.37

    assert validate_report(report) == ["results[3] duplicates results[0]"]


def test_cli_accepts_a_valid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(valid_report()))

    assert main([str(path)]) == 0
    assert capsys.readouterr().out == "OK: 3 results, total 2371.18\n"


def test_cli_lists_problems_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(with_change("total", 1)))

    assert main([str(path)]) == 1
    assert "'total' is 1 but the prices sum to 2371.18" in capsys.readouterr().err


def test_cli_rejects_malformed_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "report.json"
    path.write_text('{"results": [')

    assert main([str(path)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_reports_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "missing.json")]) == 2
    assert "missing.json" in capsys.readouterr().err


def test_cli_reads_stdin_for_piping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(valid_report())))

    assert main(["-"]) == 0
    assert capsys.readouterr().out.startswith("OK: 3 results")


def test_cli_reports_undecodable_file_as_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(b'{"results": [], "total": \xff}')  # not UTF-8

    assert main([str(path)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_help_documents_the_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "0 valid, 1 invalid, 2 unreadable" in capsys.readouterr().out
