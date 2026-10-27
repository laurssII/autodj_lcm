import ast
import string
import unittest
from pathlib import Path

from autodj import i18n
from autodj.catalog_en import EN
from autodj.session import STYLES
from autodj.strategies import STRATEGIES

PKG = Path(i18n.__file__).parent


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def _source_keys() -> set[str]:
    keys: set[str] = set()
    for path in PKG.rglob("*.py"):
        if path.name == "catalog_en.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("tr", "replan"):
                keys.add(first.value)
    return keys


class CatalogTest(unittest.TestCase):
    def setUp(self):
        i18n.set_lang("es")

    def tearDown(self):
        i18n.set_lang("es")

    def test_every_message_has_an_english_text(self):
        missing = sorted(k for k in _source_keys() if k not in EN)
        self.assertEqual(missing, [])

    def test_labels_and_styles_are_translated(self):
        for s in STRATEGIES.values():
            if s.label:
                self.assertIn(s.label, EN)
        for v in STYLES.values():
            self.assertIn(v, EN)

    def test_placeholders_match(self):
        for es, en in EN.items():
            self.assertEqual(_fields(es), _fields(en), es)

    def test_no_stale_entries(self):
        used = _source_keys() | {s.label for s in STRATEGIES.values()} | set(STYLES.values())
        used |= {"petición del usuario", "Fundido"}
        self.assertEqual(sorted(k for k in EN if k not in used), [])

    def test_translation_and_fallback(self):
        self.assertEqual(i18n.tr("Cola reordenada"), "Cola reordenada")
        i18n.set_lang("en")
        self.assertEqual(i18n.tr("Cola reordenada"), "Queue reordered")
        self.assertEqual(i18n.tr("Siguiente: {title}", title="X"), "Next: X")
        self.assertEqual(i18n.tr("texto sin traducir"), "texto sin traducir")

    def test_unknown_language_is_ignored(self):
        i18n.set_lang("en")
        i18n.set_lang("fr")
        i18n.set_lang(None)
        self.assertEqual(i18n.get_lang(), "en")

    def test_llm_directive_only_in_english(self):
        self.assertEqual(i18n.llm_directive(), "")
        i18n.set_lang("en")
        self.assertIn("English", i18n.llm_directive())


if __name__ == "__main__":
    unittest.main()
