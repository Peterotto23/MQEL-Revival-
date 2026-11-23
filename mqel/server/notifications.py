"""Server-Notifications: Zustandsänderungen an den Client melden.

Der Client übernimmt Ergebnisse serverseitiger Aktionen nicht selbst; der
Server meldet sie als Notifications im Antwort-Wrapper jeder HTTP-Antwort
(`{"Result": ..., "Notifications": [...]}`, Feldnamen laut
HyperquestWrappedResponseSerializer im Client). Jede Notification trägt einen
fortlaufenden `Index`; der Client schickt den höchsten verarbeiteten Index
als Request-Header `n` und ruft bei einer Lücke
NotificationService.CheckNotifications (s. docs/30-protokoll.md).

NotificationType: wo aus Original-Mitschnitten bekannt als Zahl (so sendete
der Originalserver, s. docs/analysis/mqeloffline-captures), sonst als
Enum-Name (der Client-Enumleser nimmt beides).
"""

import threading
from collections.abc import Callable

import castles

T = castles.T

# Belegte Enum-Werte (Mitschnitte des Originalservers, Okt. 2016)
NOTIFICATION_TYPE = {
    "FriendInvite": 1,        # FriendshipInviteNotification, Klasse FriendInviteNotification
    "FriendshipAdded": 2,     # laut NotificationType-Enum des Clients
    "FriendshipInvitationCancelled": 3,  # laut NotificationType-Enum des Clients
    "FriendshipInvitationDeclined": 4,   # laut NotificationType-Enum des Clients
    "FriendshipRemoved": 5,   # laut NotificationType-Enum des Clients
    "FriendshipInvitationUpdated": 60,  # laut NotificationType-Enum des Clients
    "CastleAttacked": 9,      # laut NotificationType-Enum des Clients
    "ObjectiveCompleted": 14,
    "AchievementCompleted": 16,  # laut NotificationType-Enum des Clients
    "ObjectiveUnlocked": 17,
    # Enum-Name ist AttackRegionCompletedNotification, nicht der Klassenname
    # ohne Suffix: als Name kam die Meldung nicht an (Region erst nach Relog)
    "AttackRegionCompleted": 18,
    "WalletUpdated": 24,
    "ExpirableAdded": 34,
    "MineShieldExpired": 66,  # laut NotificationType-Enum des Original-Servers
    "MinePillaged": 67,
    "HeroInventoryUpdated": 37,
    "HeroInventoryRemoved": 38,
    "HeroDyeApplied": 58,     # NotificationType HeroEquipmentDyeApplied
    "ShowMoreCastle": 54,     # laut NotificationType-Enum des Clients
    "ThemeUnlocked": 55,      # Enumleser des Clients
    "ThemeSelected": 56,      # Enumleser des Clients
    "ExpirableUpdated": 36,
    "TargetedAttackReloaded": 81,
    "HeroUnlocked": 27,       # laut NotificationType-Enum des Clients
    "HeroSelected": 83,       # Enumleser des Clients (0x93c577)
    "SpellUnlocked": 28,      # laut NotificationType-Enum des Clients
    "CastleLevelUp": 30,      # Enumleser des Clients (0x93bf4d)
    "HarvestingCompleted": 32,  # Enumleser des Clients (0x93bf8d)
    "HeroXpChanged": 43,
    "HeroConsumableEquip": 26,    # NotificationType-Enum des Clients
    "HeroConsumableUnequip": 42,
    "HeroEquipmentUnequip": 40,  # Enumleser des Clients (0x93c077)
    "HeroEquipmentEquip": 41,    # Enumleser des Clients (0x93c097)
    "ForgeItemReady": 64,      # Enumleser des Clients (0x93c34d)
    "ForgeStarted": 77,        # Enumleser des Clients (0x93c4cd)
    "ForgeItemCollected": 78,  # Enumleser des Clients (0x93c4ed)
    "BuildingUpgradeStarted": 45,    # Enumleser des Clients
    "BuildingUpgradeCompleted": 46,  # Enumleser des Clients (0x93c14d)
    "WalletCurrencyCapacityUpdated": 47,
    "ServerAssignmentActionCompleted": 74,
    "TrophyScoreChanged": 75,
    "FriendTrophyScoreChanged": 15,  # laut NotificationType-Enum des Original-Servers
    "FriendLeagueUpdated": 69,
    "LeagueUpdated": 65,      # Enumleser des Clients (0x93c357)
    "NewsAdded": 22,
    "CastleInventoryChanged": 25,  # Enumleser des Clients (0x93beb7)          # Enumleser des Clients (0x93be77)
    "CraftingMaterialsPackPurchased": 85,  # Enumleser des Clients (0x93c5cd)
    "CastleBought": 86,
    "DraftValidated": 87,     # Enumleser des Clients (0x93c60d)
    "GuildMemberJoined": 92,  # Enumleser des Clients (0x93c6ad)
    "GuildMemberLeft": 93,
    "GuildLeft": 94,          # Enumleser des Clients (0x93c6ed)
    "GuildMemberTitleUpdated": 103,  # Enumleser des Clients (0x93c80d)
    "InboxItemsAdded": 111,
    # Helden-Trial: Zahlen im finalen Client unbekannt, als Enum-Name; die
    # Klasse FreeTrialConditionCompletedNotification hat den Typ
    # FreeTrialPriceConditionCompleted
    "FreeTrialConditionCompleted": "FreeTrialPriceConditionCompleted",
}


class NotificationQueue:
    """Ausstehende Notifications eines (des einzigen) Spielers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = []   # noch nicht bestätigt (Index > Client-n)
        self.last_index = 0
        self.on_push: Callable[[str, dict], None] | None = None  # je neuer Meldung (Tracking)

    def ack(self, client_index):
        """Client hat alles bis `client_index` verarbeitet (Header `n`).

        Nach einem Server-Neustart setzt das auch den eigenen Zähler hinter
        den Client-Stand, damit die Indizes lückenlos weiterlaufen; nach
        einem Client-Neustart (n wieder 0) gibt es nichts mehr nachzuholen,
        der Account kommt dann frisch per GetAccountInformation.
        """
        with self.lock:
            self.last_index = max(self.last_index, client_index)
            self.pending = [x for x in self.pending if x["Index"] > client_index]

    def reset(self, client_index):
        """Client lädt den vollständigen Account (GetAccountInformation):
        Offenes ist darin enthalten und darf nicht doppelt ankommen; die
        Nummerierung setzt am Client-Stand fort."""
        with self.lock:
            self.pending = []
            self.last_index = client_index

    def push(self, type_name, fields):
        """Neue Notification `<type_name>Notification` mit Feldern."""
        with self.lock:
            self.last_index += 1
            n = {"$type": T(type_name + "Notification"),
                 "Index": self.last_index,
                 "NotificationType": NOTIFICATION_TYPE.get(type_name, type_name)}
            n.update(fields)
            self.pending.append(n)
        if self.on_push is not None:
            self.on_push(type_name, fields)
        return n

    def outgoing(self):
        with self.lock:
            return list(self.pending)


# -- Bausteine ----------------------------------------------------------------

CURRENCY_TYPE = {"PremiumCash": 1, "InGameCoin": 2, "LifeForce": 4}


def wallet_updated(queue, deltas, update_tag=0):
    """Wallet-Änderung als DIFFERENZEN je Währung (nur geänderte).

    Belegt durch eine mitgeschnittene Original-Antwort auf EndAttack
    (Tutorial, Okt. 2016, im Projekt MQELOffline_cpp): InitialGold 1000,
    TotalGold 33 -> Amounts [{CurrencyType 2, Amount 33}].
    """
    amounts = [{"$type": T("CurrencyAmount"), "Amount": d,
                "CurrencyType": CURRENCY_TYPE[field]}
               for field, d in deltas.items() if d]
    if amounts:
        queue.push("WalletUpdated", {"Amounts": amounts, "UpdateTag": update_tag})


def hero_xp_changed(queue, hero, xp_added, level_changed):
    queue.push("HeroXpChanged", {
        "HeroSpecContainerId": hero["HeroSpecContainerId"],
        "XpAdded": xp_added,
        "TotalXp": hero["XP"],
        "Level": hero["Level"],
        "LevelChanged": level_changed,
    })


def objective_completed(queue, objective_id):
    queue.push("ObjectiveCompleted", {"ObjectiveId": objective_id})


def objective_unlocked(queue, account_objective):
    queue.push("ObjectiveUnlocked", {"AccountObjective": account_objective})


def spell_unlocked(queue, spell):
    queue.push("SpellUnlocked", {"Spell": spell})


def draft_validated(queue, account_id, seconds):
    """Burg validiert; Felder aus den Strings bei
    DraftValidatedNotification.cpp. Ohne die Meldung bleibt die Burg im
    Client "nicht validiert"."""
    queue.push("DraftValidated", {"CastleValidationDuration": seconds,
                                  "DefenderAccountId": account_id,
                                  "IsDraftPublished": True,
                                  "IsRollbackAvailable": True})


def attack_region_completed(queue, completed, unlocked):
    """Felder laut AttackRegionCompletedNotification des Originalservers
    (UnlockedAttackRegionId fehlt nach der letzten Region)."""
    fields = {"CompletedAttackRegionId": completed}
    if unlocked is not None:
        fields["UnlockedAttackRegionId"] = unlocked
    queue.push("AttackRegionCompleted", fields)


def trophy_score_changed(queue, score):
    queue.push("TrophyScoreChanged", {"TrophyScore": score})


def league_updated(queue, league):
    queue.push("LeagueUpdated", {"League": league[0], "SubLeague": league[1]})