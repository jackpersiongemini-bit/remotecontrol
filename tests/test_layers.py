"""Слои и порты: каждый слой говорит только с соседями и только через их порты.

    UI (5) <> координатор (4) <> абстрактные приборы (3) <> драйверы (2) <> транспорт (1: pyvisa и симуляторы)

Модуль слоя N может импортировать:

* модули своего слоя;
* входной порт слоя N-1 - его пакет целиком (``visalab.instruments``, но не ``visalab.instruments.proxy``);
* общий словарь портов - не слой: ``visalab.core`` (параметры, профили, трассы, единицы), ``visalab.profiles``
  (профили классов приборов), ``visalab.applog`` (журнал программы). Сам словарь не импортирует ни одного слоя.

Перескок через слой, импорт вверх или внутренностей соседа - ошибка. Исключение возможно только с
записанной причиной в ``EXCEPTIONS`` (сейчас их нет). Кроме импортов проверяется, что слой не лезет
в объекты соседей в обход их порта: UI - в абстрактный прибор под прибором координатора (``_proxy``),
в драйвер (``_worker``), в транспорт; координатор - в драйвер и транспорт; pyvisa - только в транспорте.
Тесты (эта папка) не проверяются: им можно заглянуть в любой слой.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: пакет / модуль -> слой (самый длинный совпавший префикс); точки входа run_*.py - UI
LAYERS = {
    "visalab.transport": 1, "visalab.sim": 1,
    "visalab.drivers": 2,
    "visalab.instruments": 3,
    "visalab.coord": 4,
    "visalab.front": 5, "visalab.gui": 5, "visalab.ui": 5, "visalab.quick": 5, "visalab.check": 5,
}
LAYER_NAMES = {1: "транспорт", 2: "драйверы", 3: "абстрактные приборы", 4: "координатор", 5: "UI"}
#: входной порт слоя - то, что можно импортировать слою выше
PORTS = {1: {"visalab.transport", "visalab.sim"}, 2: {"visalab.drivers"}, 3: {"visalab.instruments"},
         4: {"visalab.coord"}}
#: общий словарь портов (не слой)
SHARED = ("visalab.core", "visalab.profiles", "visalab.applog")
#: атрибуты, через которые слой полез бы в объект соседа в обход его порта
FORBIDDEN_ATTRS = {
    5: {"_proxy": "абстрактный прибор под прибором координатора", "_worker": "драйвер",
        "transport": "транспорт", "driver_cls": "класс драйвера"},
    4: {"_worker": "драйвер", "transport": "транспорт", "driver_cls": "класс драйвера"},
    3: {"transport": "транспорт"},
}
#: сторонние библиотеки, которые принадлежат одному слою
OWNED = {"pyvisa": 1}
#: (модуль, импортируемый модуль или атрибут) -> почему без этого нельзя. Пусто - и пусть так и остаётся.
EXCEPTIONS: dict[tuple[str, str], str] = {}


def _modules() -> dict[str, Path]:
    out = {}
    for path in sorted((ROOT / "visalab").rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        out[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    for path in sorted(ROOT.glob("run_*.py")):
        out[path.stem] = path
    return out


MODULES = _modules()


def layer_of(module: str) -> int | None:
    if module.startswith("run_"):
        return 5
    best = max((p for p in LAYERS if module == p or module.startswith(p + ".")), key=len, default=None)
    return LAYERS[best] if best else None


def is_shared(module: str) -> bool:
    return module == "visalab" or any(module == p or module.startswith(p + ".") for p in SHARED)


def _imports(module: str, tree: ast.AST, is_package: bool):
    """[(строка, импортируемый модуль)]; ``from пакет import подмодуль`` - подмодуль."""
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                target = ".".join(base[:len(base) - node.level + 1] + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            for alias in node.names:
                sub = f"{target}.{alias.name}"
                yield node.lineno, sub if sub in MODULES else target


def _import_problem(module: str, target: str) -> str | None:
    layer, top = layer_of(module), target.split(".")[0]
    if top in OWNED:
        own = OWNED[top]
        return None if layer == own else f"{target} - это слой {own} ({LAYER_NAMES[own]})"
    if top != "visalab" and not target.startswith("run_"):
        return None                                          # PyQt6, стандартная библиотека
    if is_shared(module):
        return None if is_shared(target) else f"общий словарь импортирует слой: {target}"
    if is_shared(target):
        return None
    to = layer_of(target)
    if layer is None or to is None:
        return f"модуль вне слоёв: {module if layer is None else target}"
    if to == layer or (to == layer - 1 and target in PORTS[to]):
        return None
    if to == layer - 1:
        return f"{target} - внутренности слоя {to}; можно только его порт {' / '.join(sorted(PORTS[to]))}"
    if to > layer:
        return f"импорт вверх: {LAYER_NAMES[layer]} ({layer}) -> {target} ({LAYER_NAMES[to]}, {to})"
    return (f"перескок слоя: {LAYER_NAMES[layer]} ({layer}) -> {target} ({LAYER_NAMES[to]}, {to}); "
            f"только через порт слоя {layer - 1}")


def check_module(module: str, path: Path) -> list[str]:
    """Нарушения одного модуля: ['файл:строка: что не так']."""
    rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for line, target in _imports(module, tree, path.name == "__init__.py"):
        problem = None if (module, target) in EXCEPTIONS else _import_problem(module, target)
        if problem:
            out.append(f"{rel}:{line}: {problem}")
    forbidden = FORBIDDEN_ATTRS.get(layer_of(module), {})
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden and (module, node.attr) not in EXCEPTIONS:
            out.append(f"{rel}:{node.lineno}: .{node.attr} - {forbidden[node.attr]} в обход порта")
    return out


def test_every_module_belongs_to_a_layer_or_the_vocabulary():
    loose = [m for m in MODULES if layer_of(m) is None and not is_shared(m)]
    assert loose == [], f"модули вне слоёв: {loose} - впишите их в LAYERS или SHARED"


def test_layers_talk_only_to_neighbours_through_their_ports():
    bad = [v for module, path in MODULES.items() for v in check_module(module, path)]
    assert bad == [], "слои говорят в обход портов:\n" + "\n".join(bad)


def test_exceptions_have_reasons():
    assert all(isinstance(why, str) and len(why.split()) >= 5 for why in EXCEPTIONS.values()), \
        "у каждого исключения - причина, почему без перескока нельзя"


def test_the_check_catches_skips(tmp_path):
    """Сама проверка ловит перескоки: окно, которое создаёт драйвер, берёт внутренности слоя 3,
    лезет в драйвер прибора и в pyvisa; драйвер, который зовёт координатор."""
    window = tmp_path / "bad_window.py"
    window.write_text("import pyvisa\nfrom visalab.drivers import create_driver\n"
                      "from visalab.coord.scenario import Device\n"
                      "def f(w):\n    return w.proxy._worker\n", encoding="utf-8")
    found = check_module("visalab.front.bad_window", window)
    assert any("pyvisa" in v and "слой 1" in v for v in found), found
    assert any("перескок слоя" in v and "visalab.drivers" in v for v in found), found
    assert any("внутренности слоя 4" in v for v in found), found
    assert any("._worker" in v for v in found), found
    driver = tmp_path / "bad_driver.py"
    driver.write_text("from ..coord import COORD\nfrom ..core.params import Param\n", encoding="utf-8")
    found = check_module("visalab.drivers.bad_driver", driver)
    assert len(found) == 1 and "импорт вверх" in found[0], found
