"""Labels, grouping and documented defaults for the settings in enshrouded_server.json.

This file only describes settings; their values always come from the server (see settings.py).
A key the server reports that is missing here still shows up, under a generic label, so new game
settings appear without code changes. Defaults are the documented ones from
docs/enshrouded_server.md (the game's Default preset); keep both in sync.
"""

NO_DEFAULT = object()

GROUPS = [
    ("general", "General"),
    ("gameplay", "Gameplay"),
    ("players", "Players and access"),
    ("networking", "Networking"),
    ("advanced", "Advanced"),
]

# Gameplay sub-sections, in display order
SECTIONS = ["Difficulty", "Combat", "Player", "Survival", "Progress and resources", "World", "Other"]

# How a change takes effect. Every key in enshrouded_server.json is read once at server start:
# the server logs "Config Parsed" and prints the game settings only while starting.
RESTART = "restart"
IN_GAME = "in-game"      # changed by the server itself while running
READ_ONLY = "read-only"  # runtime facts, not settings


def _s(key, label, group, fmt="text", default=NO_DEFAULT, description=None, section=None, change=RESTART):
    return {"key": key, "label": label, "group": group, "fmt": fmt, "default": default,
            "description": description, "section": section, "change": change}


# Top-level keys of enshrouded_server.json
TOP_LEVEL = {s["key"]: s for s in [
    _s("name", "Server name", "general", description="Shown in the in-game server list."),
    _s("gameSettingsPreset", "Difficulty preset", "gameplay", "enum", "Default", section="Difficulty",
       description="Default, Relaxed, Hard or Survival use the game's fixed values; only Custom uses the individual settings below."),
    _s("tags", "Server list tags", "general", "list", [], description="Optional tags shown in the server browser."),
    _s("slotCount", "Maximum players", "players", "int", 16),
    _s("enableTextChat", "Text chat", "players", "onoff", False),
    _s("enableVoiceChat", "Voice chat", "players", "onoff", False),
    _s("voiceChatMode", "Voice chat mode", "players", "enum", "Proximity",
       description="Proximity: only nearby players hear you. Global: everyone does."),
    _s("bannedAccountsCount", "Banned accounts", "players", "int", 0, change=IN_GAME,
       description="Number of banned players. Admins ban in game; the IDs are not shown here."),
    _s("bansCount", "Banned accounts", "players", "int", 0, change=IN_GAME,
       description="Number of banned players (pre-1.0 key)."),
    _s("queryPort", "Query port (UDP)", "networking", "int", 15637,
       description="The UDP port players' games connect to."),
    _s("ip", "Bind address", "networking", "text", "0.0.0.0",
       description="Network interface the server listens on inside the container. 0.0.0.0 means all."),
    _s("saveDirectory", "Save directory", "advanced", "text", "./savegame",
       description="Relative to the server install directory."),
    _s("logDirectory", "Log directory", "advanced", "text", "./logs",
       description="Relative to the server install directory."),
]}

_G = "gameplay"
GAME_SETTINGS = {s["key"]: s for s in [
    # Combat
    _s("enemyDamageFactor", "Enemy damage", _G, "factor", 1, section="Combat", description="Damage dealt by regular enemies."),
    _s("enemyHealthFactor", "Enemy health", _G, "factor", 1, section="Combat"),
    _s("enemyStaminaFactor", "Enemy stamina", _G, "factor", 1, section="Combat",
       description="Higher makes enemies harder to stagger."),
    _s("enemyPerceptionRangeFactor", "Enemy perception range", _G, "factor", 1, section="Combat"),
    _s("bossDamageFactor", "Boss damage", _G, "factor", 1, section="Combat"),
    _s("bossHealthFactor", "Boss health", _G, "factor", 1, section="Combat"),
    _s("threatBonus", "Enemy attack frequency", _G, "factor", 1, section="Combat"),
    _s("randomSpawnerAmount", "Enemy amount", _G, "enum", "Normal", section="Combat",
       description="How many enemies roam the world (Few, Normal, Many, Extreme)."),
    _s("aggroPoolAmount", "Simultaneous attackers", _G, "enum", "Normal", section="Combat",
       description="How many enemies may attack you at once (Few, Normal, Many, Extreme)."),
    _s("pacifyAllEnemies", "Passive enemies", _G, "onoff", False, section="Combat",
       description="When on, enemies other than bosses only attack when provoked."),
    # Player
    _s("playerHealthFactor", "Player health", _G, "factor", 1, section="Player"),
    _s("playerManaFactor", "Player mana", _G, "factor", 1, section="Player"),
    _s("playerStaminaFactor", "Player stamina", _G, "factor", 1, section="Player"),
    _s("playerBodyHeatFactor", "Body heat", _G, "factor", 1, section="Player", description="Resistance to cold."),
    _s("playerDivingTimeFactor", "Diving time", _G, "factor", 1, section="Player", description="Oxygen when swimming underwater."),
    # Survival
    _s("enableStarvingDebuff", "Starvation", _G, "onoff", False, section="Survival",
       description="When on, staying hungry too long causes damage."),
    _s("fromHungerToStarving", "Hunger before starving", _G, "duration", 600000000000, section="Survival",
       description="Only matters when starvation is on."),
    _s("foodBuffDurationFactor", "Food buff duration", _G, "factor", 1, section="Survival"),
    _s("shroudTimeFactor", "Time in the Shroud", _G, "factor", 1, section="Survival"),
    _s("enableDurability", "Weapon durability", _G, "onoff", True, section="Survival"),
    _s("tombstoneMode", "Loot loss on death", _G, "enum", "AddBackpackMaterials", section="Survival",
       description="What ends up in your tombstone when you die (AddBackpackMaterials, Everything or NoTombstone)."),
    _s("curseModifier", "Shroud curse chance", _G, "enum", "Normal", section="Survival",
       description="Easy turns curses off, Hard doubles the chance."),
    # Progress and resources
    _s("experienceCombatFactor", "Combat XP", _G, "factor", 1, section="Progress and resources"),
    _s("experienceExplorationQuestsFactor", "Exploration and quest XP", _G, "factor", 1, section="Progress and resources"),
    _s("experienceMiningFactor", "Mining XP", _G, "factor", 1, section="Progress and resources"),
    _s("resourceDropStackAmountFactor", "Materials per loot stack", _G, "factor", 1, section="Progress and resources"),
    _s("miningDamageFactor", "Mining and terraforming speed", _G, "factor", 1, section="Progress and resources"),
    _s("plantGrowthSpeedFactor", "Crop growth speed", _G, "factor", 1, section="Progress and resources"),
    _s("factoryProductionSpeedFactor", "Workshop production speed", _G, "factor", 1, section="Progress and resources",
       description="Higher means crafting stations finish sooner."),
    _s("perkCostFactor", "Weapon upgrade rune cost", _G, "factor", 1, section="Progress and resources"),
    _s("perkUpgradeRecyclingFactor", "Rune refund when salvaging", _G, "factor", 0.5, section="Progress and resources",
       description="Share of upgrade runes returned when salvaging an upgraded weapon."),
    # World
    _s("dayTimeDuration", "Day length", _G, "duration", 1800000000000, section="World"),
    _s("nightTimeDuration", "Night length", _G, "duration", 720000000000, section="World"),
    _s("weatherFrequency", "Weather events", _G, "enum", "Normal", section="World",
       description="Disabled, Rare, Normal or Often."),
    _s("enableGliderTurbulences", "Glider turbulence", _G, "onoff", True, section="World"),
    _s("fishingDifficulty", "Fishing difficulty", _G, "enum", "Normal", section="World"),
    _s("tamingStartleRepercussion", "Startling animals while taming", _G, "enum", "LoseSomeProgress", section="World",
       description="Taming progress lost when the animal gets startled."),
]}

# User groups as documented (docs/enshrouded_server.md); passwords are never part of this
GROUP_PERMISSIONS = [
    ("canKickBan", "Kick and ban"),
    ("canAccessInventories", "Open others' chests"),
    ("canEditWorld", "Terraform outside bases"),
    ("canEditBase", "Edit any base"),
    ("canExtendBase", "Extend base claims"),
]
GROUP_DEFAULTS = {
    "Admin": {"canKickBan": True, "canAccessInventories": True, "canEditWorld": True, "canEditBase": True, "canExtendBase": True, "reservedSlots": 0},
    "Friend": {"canKickBan": False, "canAccessInventories": True, "canEditWorld": True, "canEditBase": True, "canExtendBase": False, "reservedSlots": 0},
    "Guest": {"canKickBan": False, "canAccessInventories": False, "canEditWorld": True, "canEditBase": False, "canExtendBase": False, "reservedSlots": 0},
    "Visitor": {"canKickBan": False, "canAccessInventories": False, "canEditWorld": False, "canEditBase": False, "canExtendBase": False, "reservedSlots": 0},
}


def humanize(key):
    """flameLevelFogRemoval -> Flame level fog removal"""
    words, word = [], ""
    for ch in key:
        if ch.isupper() and word:
            words.append(word)
            word = ch.lower()
        else:
            word += ch
    words.append(word)
    return " ".join(words).capitalize()


def unknown_setting(key, group, section=None):
    return _s(key, humanize(key), group, "auto", section=section,
              description="Not described in this dashboard yet; shown exactly as the server reports it.")
