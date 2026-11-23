"""Client-UI-Patches: Fehler der finalen UI beheben.

Die UI-Dateien liegen lose unter Data/UI und werden von dort geladen. Alle
Patches einer Datei stehen hier zusammen, weil jede Datei immer vom
Original aus gepatcht wird (<Name>.orig, falls vorhanden: das legt
tools/patch_ui.py beim lokalen Patchen an). Verteilt werden sie vom
Patcher (patcher.py, Paket GameUI).

1. Wettbewerbs-Popup "Castle's level": CastleCompetitionPopUpModule liest
   UbisoftCompetitionInfo.HeroLevel, das der Contract des finalen Clients
   nicht mehr hat (der C++-Leser kennt nur CastleLevel) -- die Zeile blieb
   leer. Der Patch nimmt CastleLevel.
2. Wettbewerbs-Rangliste, Spalte "Reward": das Template prüft
   enumValueCompare(RewardItems[0].Type, "CurrencyAmount"), der Client
   übergibt Type aber als Namen ("CurrencyAmount", 0x526330 -> 0x561a20),
   hyperquest.enums.getName vergleicht mit === gegen Zahlen -- die Spalte
   blieb immer leer. Der Patch prüft auf CurrencyAmount selbst.
3. Schmiede: Hinweis beim Einlegen der angelegten Waffe. Beim Abholen einer
   umgeschmiedeten angelegten Waffe (MainHand) wendet der Client die
   Equip-Meldungen nicht an; der Server erzwingt ein Neuladen des Accounts
   (docs/CHECKLIST.md, Schmiede), das erst beim nächsten Moduswechsel kommt.
   Angelegte Teile kommen als ForgeMysteryBoxItemModel.ItemSlotModel.
   EquippedItemModel (ItemSlot 8 = MainHand). Die MessageBox meldet beim OK
   ihre Id an den Client -> vorhandene reine Info-Id.
4. Freundes-News (HeroLevelUpFriend/Own): der Server schickt HeroName als
   englischen Klassennamen (Knight/Archer/Mage/Runaway), die Vorlagen setzen
   ihn unübersetzt in den Satz. Der Patch übersetzt ihn je Sprache mit den
   Namen aus GameplaySettings/Oasis/oasis_<Sprache>.json (Ids 4, 440, 441,
   12183). Englisch zusätzlich: "Name's{Held}" ohne Leerzeichen und
   "Your Knight's is now stronger".
5. Blings im Hauptmenü: beim Zuwachs zählte eine Animation hoch, startete bei
   parseInt("1.234") = 1 und schrieb jeden Schritt und den Endwert ohne
   Tausendertrenner. Der Patch lässt das Hochzählen (280 ms) weg; den
   formatierten Endwert setzt der Client gleich danach selbst. Die erste
   Fassung (Hochzählen mit formatCurrency je Schritt) legte im Spiel danach
   alle Achievement-Popups lahm (im Browser nicht nachvollziehbar).
6. "Attack Castle" im Kontextmenü eines Spielers: der Link nimmt
   PlayerInteractionModel.AttackType, das der C++-Leser nicht kennt
   (nur AttackSource/CastleType) -> attackType=undefined, Assert "Cannot
   parse enum" (UrlParameterMap::GetEnum) und kein Burg-Popup. Der Patch
   setzt wie der Rache-Knopf im Kampfprotokoll attackType=None.

7. Template-Helfer enumValueCompare: vergleicht getEnumName(Wert) mit dem
   Namen; der Client übergibt Enums aber oft schon als Namen (wie in 2.),
   dann liefert getEnumName "" und der Vergleich ist immer falsch. Z. B.
   zeigte das Burgfenster einer delegierten Validierung (AttackType
   "Validation") Kronen statt der Validierungs-Belohnung. Der Patch nimmt
   den Wert auch direkt als Namen.
8. Kaufabfrage eines Probe-Helden: das Helden-Fenster zeigt den Preis mit
   Trial-Rabatt (CurrencyAmount, z. B. 900), die Kaufabfrage danach den
   Listenpreis der SKU (ShopProduct.Skus[..].ActualPrice, 950); berechnet
   wird der Rabatt (Server). Der Patch merkt sich je SkuCode den Preis aus
   dem Helden-/Themen-Fenster und zeigt ihn in den Kaufabfragen.
9. Achievement-Blings (balance.py, nur bei eingeschalteter Bling-Wirtschaft):
   der Client kennt keine Achievement-Belohnung. Der Helfer
   achievementBlings (Beträge eingebettet) liefert sie je Id; Popup und
   Achievement-Liste zeigen "+N" mit Bling-Symbol.
10. Kauf eines Probe-Helden mit weniger Blings als dem Listenpreis: der Kauf-
   knopf im Helden-Fenster (game://shop/buySku) prüft im Client zuerst mit
   dem SKU-Listenpreis ohne Trial-Rabatt (ShopController 0xe4be20, nur
   Sale-Prozente) und öffnet bei zu wenig Blings den Bling-Shop
   (HandleInsufficientPremiumAmount); erst der Bestätigungsknopf
   (isPremiumCashConfirmationPurchase=1) rechnet mit Rabatt
   (CalculateSkuActualPrice 0xe4bc00 zieht die Ziele aus
   Hero.PriceReductionIds ab). Z. B. Level 16, 500 Blings, Preis 300: Shop
   statt Kauf. Der Patch gibt dem Knopf beim Probe-Helden den Parameter
   gleich mit; das Helden-Fenster mit dem Preis auf dem Knopf ist dann die
   Kaufabfrage, die zweite entfällt.
"""
import functools
import json
import pathlib

import balance

# 4. Klassennamen: Wert, den der Server schickt -> Oasis-Id
HERO_NAME_IDS = {"Knight": "4", "Archer": "440", "Mage": "441", "Runaway": "12183"}
HERO_NAME = "{{:News.Data.HeroName}}"


def hero_name(game_data, lang):
    """jsrender-Ausdruck, der HeroName in die Sprache lang übersetzt
    (unbekannte Werte unverändert). Manche Installationen haben UI-Ordner
    ohne Oasis-Datei (z. B. zh): dann englische Namen."""
    path = game_data / f"Data/GameplaySettings/Oasis/oasis_{lang}.json"
    if not path.exists():
        path = path.with_name("oasis_en.json")
    names = json.loads(path.read_text(encoding="utf-8-sig"))["Translations"]
    out = ""
    for i, (key, oasis_id) in enumerate(HERO_NAME_IDS.items()):
        keyword = "if" if i == 0 else "else"
        out += f"{{{{{keyword} News.Data.HeroName=='{key}'}}}}{names[oasis_id]}"
    return out + "{{else}}" + HERO_NAME + "{{/if}}"


def topview_patches(game_data, lang, path, achievement_blings=True):
    patches: list[tuple[str, ...]] = [  # (alt, neu[, "alle"])
        # 2.
        (('{{if ~enumValueCompare(Reward.RewardItems[0].Type, "CurrencyAmount", '
          '"hyperquest.enums.RewardItemType")}}'),
         "{{if Reward.RewardItems[0].CurrencyAmount}}", "alle"),
    ]
    if lang == "en":
        patches += [
            ("</div>'s" + HERO_NAME + " just reached",
             "</div>'s " + HERO_NAME + " just reached"),
            ("doesn't want it to stay that way\n", "doesn't want it to stay that way!\n"),
            ("Your " + HERO_NAME + "'s is now stronger", "Your " + HERO_NAME + " is now stronger"),
        ]
    # 8.
    patches += [
        (SKU_PRICE, ('<span class="currency-value">{{:~rememberPrice(SkuCode, '
                     'CurrencyAmount.Amount)}}</span>'), "alle"),
        (CONFIRM_PRICE, ('<span class="amount">{{:~rememberedPrice('
                         'ShopProduct.Skus[ShopProduct.Skus.length-1].SkuCode, '
                         'ShopProduct.Skus[ShopProduct.Skus.length-1].ActualPrice.Amount)}}</span>'),
         "alle"),
    ]
    # 10.
    patches.append((HERO_BUY_LINK, HERO_BUY_LINK.replace(
        '{{:SkuCode}}"', '{{:SkuCode}}{{if !HeroModel.IsHeroOwned && HeroModel.IsInFreeTrial}}'
        '&isPremiumCashConfirmationPurchase=1{{/if}}"', 1)))
    # 9.
    if achievement_blings:
        patches.append(achievement_blings_patch(path))
        patches.append((ACHIEVEMENT_LIST_ANCHOR, ACHIEVEMENT_LIST_ANCHOR + ACHIEVEMENT_LIST_LINE, "alle"))
    # 4. nach den Textkorrekturen, die HERO_NAME als Anker brauchen
    patches.append((HERO_NAME, hero_name(game_data, lang), "alle"))
    return patches


# 9. Achievement-Blings (balance.py): eigene Zeile unter den Punkten
ACHIEVEMENT_POPUP = 'id="template-achievement-completed-popup"'
ACHIEVEMENT_BLINGS_LINE = (
    "\n        {{if ~achievementBlings && ~achievementBlings(Achievement.Id)}}<div class=\"score\"><span>"
    "<span class='value'>+{{:~achievementBlings(Achievement.Id)}}</span> "
    "<span class=\"icon currency-small currency-small-1\" "
    "style=\"display:inline-block;vertical-align:middle\"></span></span></div>{{/if}}")


# ... und in der Achievement-Liste unter der Beschreibung (Punktespalte zu schmal)
ACHIEVEMENT_LIST_ANCHOR = ('<div class="achievementTitleDescription-description">'
                           '{{:profile.Achievement.Description}}</div>')
ACHIEVEMENT_LIST_LINE = (
    "\n        {{if ~achievementBlings && ~achievementBlings(profile.Achievement.Id)}}"
    "<div class=\"achievementTitleDescription-description\">"
    "+{{:~achievementBlings(profile.Achievement.Id)}} "
    "<span class=\"icon currency-small currency-small-1\" "
    "style=\"display:inline-block;vertical-align:middle\"></span></div>{{/if}}")


def achievement_blings_patch(path):
    """Anker = Punktezeile des Popups (je Sprache anders: pts, Pkte, ...)."""
    text = original(path).read_text(encoding="utf-8")
    start = text.index(ACHIEVEMENT_POPUP)
    begin = text.index('<div class="score">', start)
    end = text.index("</div>", begin) + len("</div>")
    line = text[begin:end]
    return (line, line + ACHIEVEMENT_BLINGS_LINE, "alle")


FORGE_ANCHOR = ("_updateMysteryBoxInformation:function(h){var a=h.ForgeMysteryBoxModel,"
                "b=a.ForgeMysteryBoxItemModel;")
WALLET_COUNT_UP = ('var j=parseInt(c.text()),f=parseInt(k);$({numericValue:j}).stop(true)'
                   '.animate({numericValue:f},{delay:100,duration:b,easing:"swing",'
                   'step:function(){c.text(parseInt(this.numericValue))},'
                   'complete:function(){c.text(f)}})')

# 8. Preis je SkuCode aus dem Fenster, das ihn mit Rabatt kennt
REMEMBER_PRICE = ('(function(){var p={};'
                  '$.views.helpers("rememberPrice",function(s,a){p[s]=a;return a});'
                  '$.views.helpers("rememberedPrice",function(s,a){return p.hasOwnProperty(s)?p[s]:a})})();')
ACHIEVEMENT_HELPER = "@ACHIEVEMENT_BLINGS@"  # ersetzt files() je Stand
SKU_PRICE = '<span class="currency-value">{{:CurrencyAmount.Amount}}</span>'
CONFIRM_PRICE = ('<span class="amount">{{:ShopProduct.Skus[ShopProduct.Skus.length-1]'
                 '.ActualPrice.Amount}}</span>')

# 10. Kaufknopf im Helden-Fenster
HERO_BUY_LINK = '<a href="game://shop/buySku?skuCode={{:SkuCode}}" class="buy" sku-code="{{:SkuCode}}">'

STATIC_FILES = [
    # 1.
    ("Js/modules/attackSelection/CastleCompetitionPopUpModule.js",
     [("Level:a.HeroLevel", "Level:a.CastleLevel")]),
    # 3.
    ("Js/modules/forge/forgeModule.js", [(FORGE_ANCHOR, FORGE_ANCHOR + (
        "if(exists(b)&&exists(b.ItemSlotModel)&&exists(b.ItemSlotModel"
        ".EquippedItemModel)&&b.ItemSlotModel.EquippedItemModel.ItemSlot==8){try{"
        "hyperquest.ui.messageBox.show({Id:hyperquest.enums.MessageBoxIds."
        "NotEnoughCraftingMaterials,Type:hyperquest.enums.MessageBoxButtonConfig.Ok,"
        "Title:\"Blacksmith\",Text:\"You are reforging your equipped weapon. It stays "
        "equipped until you collect the new one. The new weapon will only be equipped "
        "after your next mode switch; until then, both weapons may be shown. To swap, "
        "drag the new weapon onto the weapon slot.\"})}catch(x){}}"))]),
    # 7.
    ("Js/hyperquest.extensions.js", [
        ('$.views.helpers("enumValueCompare",function(c,a,b){var d=$.views.helpers.getEnumName(c,b);return d===a});',
         '$.views.helpers("enumValueCompare",function(c,a,b){var d=$.views.helpers.getEnumName(c,b);return d===a||c===a});'),
        # 8., 9.
        ('$.views.helpers("enumValueCompare",',
         REMEMBER_PRICE + ACHIEVEMENT_HELPER + '$.views.helpers("enumValueCompare",')]),
    # 6.
    ("Js/modules/profile/playerInteractionModule.js", [
        (('"&attackType="+getEnumName(this._playerInteractionModel.AttackType,'
          '"hyperquest.enums.AttackType")'),
         '"&attackType=None"')]),
    # 5.
    # 5.
    ("Js/modules/lobby/lobbyWalletModule.js", [(WALLET_COUNT_UP, "")]),
]


def files(game_data, blings=None):
    """[(Pfad unter Data/UI, [(alt, neu[, "alle"]), ...]), ...]; blings:
    JSON {Achievement-Id: Blings} für den Helfer achievementBlings (9.),
    None = Bling-Wirtschaft aus (9. entfällt)."""
    ui = game_data / "Data/UI"
    helper = "" if blings is None else (
        f'$.views.helpers("achievementBlings",function(i){{var m={blings};'
        'return m[i]||0});')
    static = [(path, [(old, new.replace(ACHIEVEMENT_HELPER, helper)) + tuple(rest)
                      for old, new, *rest in patches])
              for path, patches in STATIC_FILES]
    return static + [
        (str(p.relative_to(ui)), topview_patches(game_data, p.parent.name, p, blings is not None))
        for p in sorted((ui / "Html").glob("*/TopView*.html"))]


def original(path):
    orig = path.with_name(path.name + ".orig")
    return orig if orig.exists() else path


def patched(game_data):
    """{Pfad unter GameData ("Data/UI/..."): gepatchter Inhalt}; ValueError,
    wenn ein Anker fehlt (andere UI-Version). Hängt von den
    Achievement-Blings ab (balance.py)."""
    blings = balance.ui_achievement_blings() if balance.enabled() else None
    return _patched(str(game_data), None if blings is None else json.dumps(blings, sort_keys=True))


@functools.lru_cache(maxsize=4)
def _patched(game_data, blings):
    game_data = pathlib.Path(game_data)
    out = {}
    for rel, patches in files(game_data, blings):
        path = game_data / "Data/UI" / rel
        text = original(path).read_text(encoding="utf-8")  # Zeilenenden \n
        for old, new, *every in patches:
            count = text.count(old)
            if count == 0 or (count > 1 and not every):
                raise ValueError(f"Anker {count}x in {path}: {old}")
            text = text.replace(old, new)
        out["Data/UI/" + rel.replace("\\", "/")] = text.encode("utf-8")
    return out