# Pluginy OpenTAKServer

Ten dokument opisuje, jak dziala system pluginow serwerowych w OpenTAKServer i jak stworzyc wlasny plugin.

## Jak to dziala

OpenTAKServer laduje pluginy przez Python entry points z grupy:
- `opentakserver.plugin`

Mechanizm:
1. `PluginManager` odczytuje entry points.
2. Kazdy plugin jest instancjonowany i weryfikowany jako subclass klasy `Plugin`.
3. Podczas aktywacji plugin moze zostac wlaczony/wylaczony na podstawie wpisu w tabeli `plugins`.
4. Jesli plugin udostepnia Flask `Blueprint`, jest automatycznie rejestrowany w aplikacji.

## Wymagania dla pluginu

Plugin musi:
1. Dziedziczyc po klasie `Plugin`.
2. Implementowac metody:
- `activate(self, app, enabled)`
- `stop(self)`
- `get_info(self)`
- `load_metadata(self)`
3. Ustawic poprawnie pola identyfikacyjne:
- `name`
- `distro`
- `metadata`

## Minimalny przyklad

```python
from flask import Blueprint
from opentakserver.plugins.Plugin import Plugin


class ExamplePlugin(Plugin):
	def __init__(self):
		super().__init__()

		self.name = "Example Plugin"
		self.distro = "ots-example-plugin"
		self.metadata = {
			"name": self.name,
			"distro": self.distro,
			"author": "Your Name",
			"version": "0.1.0",
			"description": "Minimal OTS server plugin",
		}

		blueprint = Blueprint("example_plugin", __name__, url_prefix="/api/example")

		@blueprint.get("/health")
		def health():
			return {"status": "ok", "plugin": self.distro}

		self.blueprint = blueprint

	def activate(self, app, enabled: bool):
		self._app = app
		if not enabled:
			return
		# tutaj inicjalizacja pluginu

	def stop(self):
		# tutaj zwalnianie zasobow/watkow
		pass

	def get_info(self):
		return self.load_metadata()

	def load_metadata(self):
		return self.metadata
```

## Rejestracja entry point (Poetry)

W pakiecie pluginu (`pyproject.toml`):

```toml
[tool.poetry.plugins."opentakserver.plugin"]
example_plugin = "ots_example_plugin.plugin:ExamplePlugin"
```

## Oficjalny punkt startowy

Zgodnie z oficjalna dokumentacja OTS, plugin najlepiej zaczac od template:
1. OTS-Plugin-Template
2. (Opcjonalnie) OTS-UI-Plugin-Template dla frontendu

Template zawiera oznaczenia TODO, ktore prowadza przez podstawowa konfiguracje pluginu.

## Workflow development (Poetry)

Oficjalnie zalecany jest nastepujacy stack:
1. Poetry do zaleznosci i buildow
2. Poetry Dynamic Versioning do wersjonowania po tagach git
3. Semantic Versioning dla wydan (np. `1.0.0`, `1.1.0`, `2.0.0`)

W praktyce:
1. Tworzysz tag wersji w git.
2. Uruchamiasz `poetry build`.
3. Artefakty wheel/sdist dostaja wersje z tagu.

## Checklista startowa pluginu

Najwazniejsze kroki, ktore dokumentacja oficjalna wskazuje na poczatku:
1. `pyproject.toml`:
- ustaw nazwe, opis, autora i URL-e
- nazwa pluginu powinna zaczynac sie od `OTS-`
- popraw wpisy `include` i dynamic-versioning pod wlasny pakiet
2. `app.py` pluginu:
- zmien nazwe klasy
- zmien nazwe blueprintu
- dodaj endpointy API i zabezpiecz je dekoratorami autoryzacji
- rozbuduj `activate()` jesli plugin uruchamia procesy w tle
3. `default_config.py` pluginu:
- zmien nazwy opcji konfiguracyjnych na pluginowe
- dodaj i waliduj nowe opcje

## Web UI pluginu (opcjonalnie)

Dokumentacja OTS opisuje osobny frontend pluginu:
1. UI jest osadzany w OpenTAKServer jako iframe.
2. UI pluginu jest budowane w React + Mantine.
3. Repo UI powinno byc osobne od repo backendowego pluginu.
4. Build UI powinien finalnie trafic do katalogu `ui` pluginu backendowego.

## Publiczne repo pluginow OTS

Dostepne jest publiczne repo pluginow:
- https://repo.opentakserver.io/

Pluginy dodawane do publicznego repo przechodza code review (wymagania, jakosc i bezpieczenstwo).

## Licencjonowanie

Dla publicznie dystrybuowanych pluginow oficjalna dokumentacja wymaga:
1. GPLv3 lub
2. innej licencji kompatybilnej z GPLv3

## Instalacja pluginu

Dostepne opcje:
1. Zainstalowanie pakietu Python (np. wheel) w srodowisku serwera.
2. Instalacja przez mechanizm plugin managera (z repozytorium pluginow lub lokalnego pliku).

Po instalacji plugin powinien pojawic sie w API pluginow.

## Endpointy API pluginow

Administracyjne endpointy API:
- `GET /api/plugins`
- `POST /api/plugins` (upload pliku pluginu)
- `GET /api/plugins/<plugin_name>`
- `POST /api/plugins/<plugin_name>/enable`
- `POST /api/plugins/<plugin_name>/disable`

## Konfiguracja

Istotne opcje:
- `OTS_ENABLE_PLUGINS` - wlacza/wylacza ladowanie pluginow
- `OTS_PLUGIN_REPO` - URL repozytorium pluginow
- `OTS_PLUGIN_PREFIXES` - dozwolone prefiksy nazw pluginow przy instalacji z repo

## Debugowanie

Jesli plugin sie nie laduje:
1. Sprawdz, czy entry point ma grupe `opentakserver.plugin`.
2. Upewnij sie, ze klasa pluginu dziedziczy po `Plugin`.
3. Sprawdz logi OTS pod katem `Failed to load plugin`.
4. Zweryfikuj, czy `distro` i `name` sa stabilne i unikalne.
5. Potwierdz, ze plugin jest `enabled` w tabeli `plugins`.

## Dobre praktyki

1. Trzymaj `activate()` szybkie i odporne na bledy.
2. Unikaj dlugich operacji blokujacych bez osobnych workerow.
3. W `stop()` domykaj watki/polaczenia.
4. Zwracaj kompletne metadane z `load_metadata()`.
5. Trzymaj kompatybilnosc API pluginu z biezaca wersja OTS.
6. Traktuj backend pluginu i UI pluginu jako dwa niezalezne artefakty wydawnicze.

