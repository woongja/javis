import re
from html.parser import HTMLParser
from importlib.resources import files


class HUDMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        assert not any(key.startswith("on") or key in {"style", "srcset"} for key in fields)
        for key in ("src", "href", "action"):
            if key in fields:
                value = fields[key]
                assert value and value.startswith("/") and not value.startswith("//")
                if key != "action":
                    self.assets.add(value)
        if tag == "script":
            assert fields.get("src") and set(fields) <= {"src", "defer"}
            self.in_script = True

    def handle_data(self, data):
        if self.in_script:
            assert not data.strip()

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False


def test_assets_are_offline_and_use_external_scripts():
    package = files("hud")
    html = package.joinpath("index.html").read_text(encoding="utf-8")
    parser = HUDMarkup()
    parser.feed(html)
    parser.close()
    assert parser.assets == {"/static/hud.css", "/static/hud.js", "/static/bg.js"}
    assert "//" not in html
    for path in parser.assets:
        source = package.joinpath(*path[1:].split("/")).read_text(encoding="utf-8")
        assert "//" not in source


def test_hud_scripts_use_text_insertion_and_lock_decisions():
    static = files("hud").joinpath("static")
    script = static.joinpath("hud.js").read_text(encoding="utf-8")
    scripts = script + static.joinpath("bg.js").read_text(encoding="utf-8")
    assert not re.search(
        r"\b(?:innerHTML|insertAdjacentHTML|eval|localStorage)\b|document\s*\.\s*write|new\s+Function",
        scripts,
    )
    assert "document.createElement" in script and ".textContent" in script
    assert "deciding.has(id)" in script and "deciding.add(id)" in script and "deciding.delete(id)" in script
    assert "button.disabled" in script and "deciding.has(actionsByButton.get(button).id)" in script
