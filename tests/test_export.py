"""CSV-экспорт трасс: общая ось, разные оси, комплексные трассы."""
import pytest

from visalab.core import Trace
from visalab.core.export import write_traces_csv


def test_shared_axis_and_complex(tmp_path):
    a = Trace.linear(1e9, 2e9, [-1.0, -2.0, -3.0], y_unit="dB", name="S21")
    b = Trace.linear(1e9, 2e9, [0.1, 0.2, 0.3], im=[0.5, 0.6, 0.7], y_unit="", name="S11")
    path = write_traces_csv(tmp_path / "sub" / "t.csv", [a, b], comments=["hello"])
    assert path.read_text(encoding="utf-8").splitlines() == [
        "# hello", '"Freq, Hz","S21, dB",S11 Re,S11 Im', "1000000000,-1,0.1,0.5", "1500000000,-2,0.2,0.6",
        "2000000000,-3,0.3,0.7"]


def test_different_axes_padded(tmp_path):
    a = Trace.linear(1e9, 2e9, [1.0, 2.0, 3.0], y_unit="dB")
    b = Trace.linear(0.0, 1.0, [5.0, 6.0], x_unit="s", y_unit="dB")
    lines = write_traces_csv(tmp_path / "t.csv", [a, b], ["A", "B"]).read_text(encoding="utf-8").splitlines()
    assert lines[0] == '"Freq 1, Hz","A, dB","Time 2, s","B, dB"'
    assert lines[1] == "1000000000,1,0,5" and lines[3] == "2000000000,3,,"
    with pytest.raises(ValueError):
        write_traces_csv(tmp_path / "e.csv", [])
