# Markéta Trefilová → Přehraj.to

Samostatný účet `marketa-trefilova@seznam.cz` (uživatelské jméno
`markéta.trefilová.cz`). Originální soubory ze Sdílej.cz se průběžně přenášejí;
celý film se neukládá na disk runneru. Hesla jsou pouze v GitHub Secrets.
Přístup k e-mailové schránce není potřeba.

## Pravidla

Na jeden film nejvýše dvě varianty, každá s vlastním stavem a cílovým ID:

| Zdroj | Název na Přehraj.to |
| --- | --- |
| Ověřený český zvuk | The Shawshank Redemption CZ Dabing |
| Ověřený anglický nebo původní zvuk a doložené české titulky | The Shawshank Redemption CZ Titulky |
| Anglický nebo původní zvuk, české titulky nenalezeny | The Shawshank Redemption |

Pokud se anglický a původní název liší, uvedou se oba, v pořadí
`Anglický název (Původní název)`. Jazykový dodatek je až na konci, například
`The Good, the Bad and the Ugly (Il buono, il brutto, il cattivo) CZ Dabing`.
Shodné názvy, včetně rozdílů pouze ve velikosti písmen, se neopakují.
Každý název se při hledání zdrojů ověřuje samostatně; sloučený název slouží
pro pojmenování cílového videa.

Varianta v angličtině nebo původním znění s českými titulky má přednost i před
vyšším rozlišením bez titulků. Bez titulků se vybírá pouze po dokončení kontroly
nalezených kandidátů.
Uvnitř této preference se vybírá nejvyšší ověřené rozlišení a následně nejmenší
soubor splňující původní minimální datový tok pro daný kodek. Chyba při kontrole
kandidáta není důkaz, že titulkový zdroj neexistuje; výběr bez titulků se odloží
a výsledky dokončených kontrol se uchovají pro další běh. Pokud už byl ověřen
titulkový zdroj, lze použít nejlepší z ověřených titulkových zdrojů i při chybě
jiného kandidáta.
Původní jazyk se přebírá z ověřených metadat filmu, nikdy se neodhaduje podle
názvu souboru. Pokud údaj chybí, druhá varianta zatím přijímá pouze angličtinu.

České zdroje se znovu nevyhledávají. Import používá
`Olbrasoft/sdilej-to-prehrajto`: doložený zdroj dokončeného uploadu má přednost
před nově vybraným zdrojem v manifestu. Pokud původní účet už nemá podrobný
uploadový záznam, použije se jeho ověřený manifest. Importují se výhradně
české zvukové stopy a stabilní detailové odkazy. Staré cílové identifikátory,
příznaky nahrání, chyby a cookies se nepřenášejí. Při dalších bězích se doplňují
nové české zdroje; již převzaté zdroje zůstávají zachované.

Pořadí 28 775 filmů pochází z původního `backlog/films.jsonl.gz`: jde o vážené
hodnocení, primárně IMDb, s počtem hlasů a náhradními hodnoceními ČSFD/TMDB.
Není to žebříček počtu přehrání. Pořadí se nemění.

Anglické názvy se ověřují podle IMDb ID (případně TMDB ID) proti anglickým
štítkům Wikidat. Původní název se automaticky nepovažuje za anglický.
Původní název se přebírá z `original_title` v exportu původního repozitáře
(databázový sloupec `films.original_title`). Pokud chybí, doplní se z pole
`original_title` v [detailu filmu TMDB](https://developer.themoviedb.org/reference/movie-details)
podle uloženého TMDB ID, s kontrolou IMDb ID, pokud je dostupné. Všechny názvy
jsou převzaté databázové údaje; žádné názvy se nepřekládají ani negenerují.
Nedohledaný původní název se do závorky nedoplňuje odhadem.
Chybějící nebo nejednoznačné názvy čekají na doplnění v
`config/english-titles.json`, například `{"5": {"title": "The Shawshank Redemption"}}`.
U známých filmů se stejným anglickým názvem se přidává rok pro rozlišení remaků.
Zdroj metadata API: [Wikibase API](https://www.mediawiki.org/wiki/Wikibase/API).

## GitHub Actions

Automatika má dva nezávislé procesy, zapnuté proměnnou `AUTOMATION_ENABLED=true`:

- `continuous-upload`: hodinový start, jeden běh zahajuje nové přenosy po dobu
  240 minut. Rozběhnutému přenosu ponechává rezervu před limitem runneru.
  Po každé dílčí dávce ihned vybírá další filmy a načítá nové zdroje z Gitu.
  Prázdnou frontu znovu kontroluje po 30 sekundách. Hodinové starty zajistí
  navázání dalšího běhu po skončení runneru, nikoli přestávku mezi dávkami.
- `prepare-sources`: samostatný hodinový běh doplňuje názvy, české zdroje
  a vyhledává anglické a původní varianty. S časovým rozpočtem 40 minut připravuje
  až 200 filmů; po čtyřech dosud nezkoušených filmech zařadí nejstarší opakovanou
  kontrolu, aby průběžně dokončoval i dříve přerušená hledání.
  Každý film má dílčí časový limit a dokončené kontroly zdrojů se ukládají.
  Chybný kandidát nezastaví kontrolu dalších kandidátů stejného filmu.
  Jeho kontrola zdrojů nezdržuje uploader.

Workflow `marketa-sync` poskytuje společný runtime a ruční operace:

- `plan`: vypíše příští názvy a zdroje, nic nenahrává.
- `prepare`: vyhledá anglické zdroje, ověří identitu, délku, skutečné rozlišení,
  jazyk originálního zvuku pomocí Whisperu a české titulkové stopy.
- `upload`: postupně nahraje připravené varianty v pořadí backlogu.
- `continuous`: opakuje nahrávání dílčích dávek po zadanou dobu (v automatice
  240 minut), rozběhnutý přenos nechá doběhnout.
- `subtitles`: doplní existující české textové titulky z přesného původního zdroje.

Uploady a titulkové operace mají společnou concurrency skupinu; současně
běží jeden uploadovací proces se dvěma souběžnými přenosy filmů. Po dokončení
jednoho přenosu uvolněný worker ihned převezme další film, i když druhý přenos
ještě běží. Souběh lze nastavit parametrem `--workers` nebo `UPLOAD_WORKERS`
v rozsahu 1 až 6. Každý worker má vlastní přihlášené relace; česká a původní
varianta sdílejí jeden společný limit přenosů. Titulky se doplňují samostatnou
relací na pozadí. Příprava má vlastní skupinu a zapisuje oddělené
stavové soubory. Plánovací kontrola je nezávislá a nic neposílá na cílový účet.
Výchozí dílčí dávka jsou dvě varianty; v průběžném režimu jde o velikost jedné
iterace, nikoli o omezení celého běhu. Uploader mezi dávkami také doplňuje
titulky dokončených anglických videí. Přípravný běh obnoví české zdroje a doplní
až 100 anglických názvů.

GitHub Secrets: `SDILEJ_EMAIL`, `SDILEJ_PASSWORD`, `PREHRAJTO_EMAIL`,
`PREHRAJTO_PASSWORD`; `TMDB_API_KEY` pro doplnění chybějících původních názvů.
Cílový klient odmítne jiné přihlašovací jméno než
`marketa-trefilova@seznam.cz`.

Před odesláním dat se uloží přidělené cílové ID do Gitu. Přerušení ani čekání na
zpracování nesmí vytvořit další upload. Vyhledávání existujících videí porovnává
celý název včetně jazykového dodatku a případného roku. Dokončený upload uchovává
vazbu na zdroj pro pozdější titulky. Stav se odděluje v `variants/cz` a `variants/en`.

## Titulky a odložená práce

- `reports/subtitle-followup.md`: přenesená anglická videa a potřebná akce.
- `reports/subtitle-backfill.md`: skutečný výsledek kontrol a doplnění titulků.
- `variants/en/plans/subtitle-followup.jsonl`: trvalá fronta s cílovým ID a zdrojem.
- `variants/en/state/discovery.json`: nedohledané zdroje a termíny opakování.
- `state/english-titles.json`: ověřené názvy a nedohledané názvy.

Existující české textové stopy se po zpracování videa automaticky extrahují
z jeho přesného zdroje a připojí. Bez titulkové stopy zůstane film v přehledu
k ručnímu doplnění. Titulky se automaticky nepřekládají a nepřebírají z jiné verze
filmu. Nejasný výsledek odeslání titulků se pouze ověřuje, odeslání se neopakuje.
Název bez dodatku se po ručním doplnění titulků zatím automaticky nepřejmenovává.

Obrazové české stopy mají při výběru také přednost, jejich převod a doplnění
však vyžadují ruční práci a zůstávají v titulkovém přehledu. Natvrdo vložené
titulky nemají automatické ověření. Zdroj označený českými titulky bez
dohledatelné stopy se odloží k ověření.
Pouhá zmínka EN v názvu vícezvukového souboru nestačí: první i výchozí zvuková
stopa nesmějí být označené jiným jazykem a původní první stopu ověřuje Whisper.

## Vývoj

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
.venv/bin/marketa-sync plan --limit 5
```

Pro vyhledávání je potřeba FFmpeg a instalace `.[whisper]`. Přenosový a titulkový
klient vychází z původního repozitáře; výchozí revize je v `upstream/revision.txt`.
Původní pracovní adresář se neupravuje.
