"""Автоподключение: прибор, потерявший связь (выключили, перезагрузили, выдернули кабель) или выключенный
при запуске программы, подключается сам, когда снова отвечает.

* попытки - с растущими паузами (``RECONNECT_DELAYS_S``), журнал не засоряется: первая ошибка и одна строка;
* узел LAN, который не отвечает, не открывается библиотекой VISA (там open ждал бы десятки секунд);
* выключатель окна в это время - «включён, связи нет» (ручка справа, красный), клик - перестать ждать;
* ошибка программы при подключении - ERROR (а не вечное «подключение»), без попыток.
"""
import socket

import pytest
from PyQt6.QtCore import QSettings

from visalab.coord import ActivityMonitor, ProxyLinks
from visalab.core import ConnectionState, InstrumentProxy, SimTransport
from visalab.core.transport import PyVisaTransport, lan_endpoint
from visalab.drivers import KeysightE8267D
from visalab.front import RFGenWindow
from visalab.front.main_window import MainWindow
from visalab.sim import KeysightE8267DSim

FAST = (0.05, 0.1)


@pytest.fixture
def gen(qtbot):
    """make(powered=True) -> (proxy, sim, transport, comm errors, reconnect_pending values)."""
    made = []

    def make(powered=True, driver=KeysightE8267D, delays=FAST):
        sim = KeysightE8267DSim()
        sim.powered = powered
        tr = SimTransport(sim, address="SIM::GEN::INSTR")
        proxy = InstrumentProxy(driver, tr)
        proxy._worker.RECONNECT_DELAYS_S = delays          # до первой команды: поток прибора ещё ничего не делал
        comm, pending = [], []
        proxy.comm_error.connect(comm.append)
        proxy.reconnect_pending.connect(pending.append)
        made.append(proxy)
        return proxy, sim, tr, comm, pending

    yield make
    for p in made:
        p.shutdown()


def _power_off(sim, tr):
    sim.powered = False
    tr.fail_after = tr._ops          # следующий обмен - ошибка связи


def _power_on(sim, tr):
    sim.powered, tr.fail_after = True, None


def test_lost_connection_comes_back_by_itself(qtbot, gen):
    proxy, sim, tr, comm, pending = gen()
    proxy.open()
    qtbot.waitUntil(lambda: proxy.is_connected and proxy.value("rf.frequency") is not None, timeout=5000)
    _power_off(sim, tr)
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.ERROR and proxy.reconnecting, timeout=5000)
    qtbot.wait(400)                                       # несколько попыток, пока прибор выключен
    assert proxy.state is ConnectionState.ERROR and proxy.reconnecting
    _power_on(sim, tr)
    qtbot.waitUntil(lambda: proxy.is_connected and proxy.value("rf.frequency") is not None, timeout=5000)
    assert not proxy.reconnecting and pending[-1] == 0
    # журнал: обрыв и одна строка о переподключении, попытки не повторяются
    assert sum("переподключение" in m for m in comm) == 1 and len(comm) == 2, comm


def test_pauses_grow_and_stay_at_the_last(qtbot, gen):
    proxy, sim, tr, _comm, pending = gen(powered=False)
    proxy.open()
    qtbot.waitUntil(lambda: len(pending) >= 4, timeout=5000)
    assert pending[:4] == [0.05, 0.1, 0.1, 0.1]


def test_instrument_off_at_start_connects_when_switched_on(qtbot, gen):
    proxy, sim, tr, comm, _pending = gen(powered=False)
    proxy.open()
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.ERROR and proxy.reconnecting, timeout=5000)
    sim.powered = True
    qtbot.waitUntil(lambda: proxy.is_connected, timeout=5000)
    assert not proxy.reconnecting


def test_disconnect_stops_reconnecting(qtbot, gen):
    proxy, sim, tr, _comm, pending = gen(powered=False)
    proxy.open()
    qtbot.waitUntil(lambda: proxy.reconnecting, timeout=5000)
    proxy.close()
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.DISCONNECTED and not proxy.reconnecting, timeout=5000)
    sim.powered = True
    qtbot.wait(400)
    assert proxy.state is ConnectionState.DISCONNECTED and pending[-1] == 0


def test_no_auto_reconnect_when_turned_off(qtbot, gen):
    proxy, sim, tr, _comm, pending = gen(powered=False)
    proxy.set_auto_reconnect(False)                       # проверка стенда: нужна сама ошибка
    proxy.open()
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.ERROR, timeout=5000)
    sim.powered = True
    qtbot.wait(400)
    assert proxy.state is ConnectionState.ERROR and not proxy.reconnecting and not pending


def test_unreachable_instrument_is_not_opened(qtbot, gen):
    """Узел не отвечает (проверка доступности) - open не вызывается: у VISA он ждал бы десятки секунд."""
    proxy, sim, tr, _comm, pending = gen(powered=False)
    opens = []
    real_open = tr.open
    tr.open = lambda: (opens.append(1), real_open())[1]
    proxy.open()
    qtbot.waitUntil(lambda: len(pending) >= 4, timeout=5000)
    assert len(opens) == 1                                # только подключение по запросу


def test_failed_attempts_do_not_flood_the_log(qtbot, gen):
    proxy, sim, tr, comm, _pending = gen(powered=False)
    opens = []
    real_open = tr.open
    tr.reachable = lambda: True                           # узел отвечает, но открыть не удаётся
    tr.open = lambda: (opens.append(1), real_open())[1]
    proxy.open()
    qtbot.waitUntil(lambda: len(opens) >= 4, timeout=5000)
    assert len(comm) == 2, comm                           # «не найден» + «переподключение автоматически»


class _BrokenOnConnect(KeysightE8267D):
    def on_connected(self):
        raise ValueError("ответ прибора не разобран")


def test_program_error_on_connect_is_error_not_connecting(qtbot, gen):
    proxy, sim, tr, comm, pending = gen(driver=_BrokenOnConnect)
    proxy.open()
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.ERROR, timeout=5000)
    qtbot.wait(300)
    assert proxy.state is ConnectionState.ERROR and not proxy.reconnecting and not pending
    assert any("не разобран" in m for m in comm)
    assert not tr.is_open


def test_window_switch_shows_retry_and_click_stops(qtbot, gen, tmp_path):
    proxy, sim, tr, _comm, _pending = gen()
    win = RFGenWindow(proxy, QSettings(str(tmp_path / "rf.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    assert win.on_button.status() == "on" or win.on_button.isChecked()
    _power_off(sim, tr)
    qtbot.waitUntil(lambda: win.on_button.status() == "retry", timeout=5000)
    assert win.on_button.isChecked() and "Click - stop" in win.on_button.toolTip()   # «включён, связи нет»
    win.on_button.click()                                 # отключить: перестать переподключаться
    qtbot.waitUntil(lambda: proxy.state is ConnectionState.DISCONNECTED and not proxy.reconnecting, timeout=5000)
    assert not win.on_button.isChecked()
    _power_on(sim, tr)
    qtbot.wait(300)
    assert proxy.state is ConnectionState.DISCONNECTED


def test_main_window_tile_shows_retry(qtbot, gen, tmp_path):
    proxy, sim, tr, _comm, _pending = gen()
    links = ProxyLinks()
    links.add(proxy)
    win = MainWindow(QSettings(str(tmp_path / "main.ini"), QSettings.Format.IniFormat), ActivityMonitor(links))
    qtbot.addWidget(win)
    with qtbot.waitSignal(proxy.sync_done, timeout=5000):
        proxy.open()
    _power_off(sim, tr)
    qtbot.waitUntil(lambda: win.tiles[proxy].text().endswith("RETRY"), timeout=5000)
    _power_on(sim, tr)
    qtbot.waitUntil(lambda: proxy.is_connected, timeout=5000)
    qtbot.waitUntil(lambda: not win.tiles[proxy].text().endswith("RETRY"), timeout=5000)


# ------------------------------------------------------------------ проверка доступности узла LAN
def test_lan_endpoint_from_visa_address():
    assert lan_endpoint("TCPIP0::192.168.0.3::INSTR") == ("192.168.0.3", 111)            # VXI-11: portmapper
    assert lan_endpoint("TCPIP0::192.168.0.3::inst0::INSTR") == ("192.168.0.3", 111)
    assert lan_endpoint("TCPIP0::10.0.0.1::gpib0,5::INSTR") == ("10.0.0.1", 111)         # шлюз LAN-GPIB
    assert lan_endpoint("TCPIP::pna::hislip0::INSTR") == ("pna", 4880)
    assert lan_endpoint("TCPIP0::pna::hislip0,4881::INSTR") == ("pna", 4881)
    assert lan_endpoint("TCPIP0::192.168.0.3::5025::SOCKET") == ("192.168.0.3", 5025)
    assert lan_endpoint("USB0::0x2A8D::0x7F18::MY56040018::0::INSTR") is None
    assert lan_endpoint("GPIB0::18::INSTR") is None


def test_reachable_probe(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert PyVisaTransport(f"TCPIP0::127.0.0.1::{port}::SOCKET").reachable()          # слушает
    finally:
        srv.close()
    assert PyVisaTransport(f"TCPIP0::127.0.0.1::{port}::SOCKET").reachable()              # отказ: узел жив
    assert PyVisaTransport("USB0::0x2A8D::0x7F18::MY56040018::0::INSTR").reachable()       # USB - пробовать

    def silent(*_a, **_kw):
        raise socket.timeout("timed out")
    monkeypatch.setattr(socket, "create_connection", silent)
    assert not PyVisaTransport("TCPIP0::192.168.0.20::INSTR").reachable()                # узел молчит
