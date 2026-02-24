import os
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ======================
# CONFIG
# ======================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN (add it in Railway Variables).")

INTENTS = discord.Intents.default()
INTENTS.members = True  # IMPORTANT: enable "Server Members Intent" in Dev Portal
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ======================
# ROLES
# ======================
ROLE_LABELS_FR = {
    "loup_garou": "🐺 Loup-Garou",
    "loup_garou_noir": "🖤🐺 Loup-Garou Noir",
    "sorciere": "🧪 Sorcière",
    "voyante": "🔮 Voyante",
    "petite_fille": "👧 Petite Fille",
    "garde": "🛡️ Garde",
    "chasseur": "🔫 Chasseur",
    "cupidon": "💘 Cupidon",
    "villageois": "🌾 Villageois",
}

WOLF_ROLES = {"loup_garou", "loup_garou_noir"}

# ======================
# GAME STATE
# ======================
@dataclass
class PlayerState:
    user_id: int
    role: Optional[str] = None
    alive: bool = True
    lover_id: Optional[int] = None  # Cupidon
    infected: bool = False          # became wolf by infection

@dataclass
class VoteState:
    active: bool = False
    kind: str = ""  # "mayor" or "daykill"
    votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id

@dataclass
class GameState:
    guild_id: int
    channel_id: int
    created_by: int
    started: bool = False
    phase: str = "lobby"  # lobby/night/day/ended
    day: int = 0
    players: Dict[int, PlayerState] = field(default_factory=dict)

    # Mayor
    mayor_id: Optional[int] = None

    # Night actions storage
    protected_id: Optional[int] = None
    seer_target_id: Optional[int] = None
    wolf_target_id: Optional[int] = None

    # Witch
    witch_heal_used: bool = False
    witch_kill_used: bool = False
    witch_heal_target_id: Optional[int] = None
    witch_kill_target_id: Optional[int] = None

    # Cupidon
    cupid_done: bool = False
    cupid_targets: List[int] = field(default_factory=list)

    # Black wolf infection
    black_infect_used: bool = False
    black_infect_target_id: Optional[int] = None

    # Voting
    vote: VoteState = field(default_factory=VoteState)

GAMES: Dict[int, GameState] = {}  # guild_id -> game


# ======================
# HELPERS
# ======================
def get_game(guild_id: int) -> Optional[GameState]:
    return GAMES.get(guild_id)

def alive_ids(game: GameState) -> List[int]:
    return [pid for pid, p in game.players.items() if p.alive]

def get_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    return guild.get_member(user_id)

async def dm(member: discord.Member, content: str):
    try:
        await member.send(content)
    except discord.Forbidden:
        # user has DMs closed
        pass

def role_distribution(n: int) -> List[str]:
    """
    Simple distribution for 8–15 players.
    You can tweak it anytime.
    """
    if n < 8 or n > 15:
        raise ValueError("Partie prévue pour 8 à 15 joueurs.")

    roles: List[str] = []
    # Core roles
    roles += ["voyante", "sorciere", "garde", "chasseur", "cupidon"]

    # Wolves depending on size
    if n <= 9:
        roles += ["loup_garou", "loup_garou"]
    elif n <= 12:
        roles += ["loup_garou", "loup_garou_noir", "loup_garou"]
    else:
        roles += ["loup_garou", "loup_garou_noir", "loup_garou", "loup_garou"]

    # Petite fille at 10+
    if n >= 10:
        roles += ["petite_fille"]

    while len(roles) < n:
        roles.append("villageois")

    random.shuffle(roles)
    return roles

def is_wolf(role: Optional[str]) -> bool:
    return role in WOLF_ROLES

def channel(game: GameState, guild: discord.Guild) -> discord.TextChannel:
    ch = guild.get_channel(game.channel_id)
    assert isinstance(ch, discord.TextChannel)
    return ch

def endgame_roles_text(game: GameState, guild: discord.Guild) -> str:
    lines = ["📜 **Rôles de fin de partie :**"]
    for pid, p in game.players.items():
        m = get_member(guild, pid)
        name = m.display_name if m else str(pid)
        role = ROLE_LABELS_FR.get(p.role or "?", p.role or "?")
        status = "✅ vivant" if p.alive else "💀 mort"
        if p.infected:
            status += " (infecté)"
        lines.append(f"- **{name}** : {role} — {status}")
    return "\n".join(lines)

def check_victory(game: GameState) -> Optional[str]:
    """
    Return winner text if game ended, else None.
    Wolves win when wolves >= non-wolves alive.
    Village wins when no wolves alive.
    """
    alive = [p for p in game.players.values() if p.alive]
    wolves = sum(1 for p in alive if is_wolf(p.role))
    others = sum(1 for p in alive if not is_wolf(p.role))
    if wolves == 0 and game.started:
        return "🏆 **Victoire du Village !** (il n’y a plus de loups)"
    if wolves >= others and game.started and (wolves > 0):
        return "🏆 **Victoire des Loups !** (les loups sont majoritaires)"
    return None


# ======================
# DISCORD UI: DM SELECTS
# ======================
class TargetSelect(discord.ui.Select):
    def __init__(
        self,
        game: GameState,
        guild: discord.Guild,
        actor_id: int,
        action_key: str,
        prompt: str,
        allow_self: bool = False,
        only_alive: bool = True,
        max_values: int = 1,
    ):
        self.game = game
        self.guild = guild
        self.actor_id = actor_id
        self.action_key = action_key

        opts: List[discord.SelectOption] = []
        for pid, p in game.players.items():
            if only_alive and not p.alive:
                continue
            if (not allow_self) and pid == actor_id:
                continue
            m = get_member(guild, pid)
            label = m.display_name if m else f"Joueur {pid}"
            opts.append(discord.SelectOption(label=label, value=str(pid)))

        super().__init__(
            placeholder=prompt,
            min_values=1,
            max_values=max_values,
            options=opts[:25],  # Discord limit
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Ce menu n’est pas pour toi.", ephemeral=True)
            return

        selected = [int(v) for v in self.values]

        if self.action_key == "seer":
            self.game.seer_target_id = selected[0]
            await interaction.response.send_message("🔮 Cible enregistrée.", ephemeral=True)

        elif self.action_key == "guard":
            self.game.protected_id = selected[0]
            await interaction.response.send_message("🛡️ Protection enregistrée.", ephemeral=True)

        elif self.action_key == "wolves":
            self.game.wolf_target_id = selected[0]
            await interaction.response.send_message("🐺 Vote des loups enregistré.", ephemeral=True)

        elif self.action_key == "witch_heal":
            self.game.witch_heal_target_id = selected[0]
            await interaction.response.send_message("🧪 Soin choisi.", ephemeral=True)

        elif self.action_key == "witch_kill":
            self.game.witch_kill_target_id = selected[0]
            await interaction.response.send_message("🧪 Poison choisi.", ephemeral=True)

        elif self.action_key == "cupidon":
            self.game.cupid_targets = selected
            self.game.cupid_done = True
            await interaction.response.send_message("💘 Amoureux choisis.", ephemeral=True)

        elif self.action_key == "black_infect":
            self.game.black_infect_target_id = selected[0]
            await interaction.response.send_message("🖤 Infection choisie.", ephemeral=True)

        else:
            await interaction.response.send_message("Action inconnue.", ephemeral=True)


class ActionView(discord.ui.View):
    def __init__(
        self,
        game: GameState,
        guild: discord.Guild,
        actor_id: int,
        action_key: str,
        prompt: str,
        allow_self: bool = False,
        only_alive: bool = True,
        max_values: int = 1,
        timeout: int = 120,
    ):
        super().__init__(timeout=timeout)
        self.add_item(TargetSelect(game, guild, actor_id, action_key, prompt, allow_self, only_alive, max_values))


# ======================
# SLASH COMMANDS GROUP
# ======================
class LG(app_commands.Group):
    def __init__(self):
        super().__init__(name="lg", description="Bot Loup-Garou (IRL vocal)")

lg = LG()
bot.tree.add_command(lg)


@lg.command(name="create", description="Créer une partie dans ce salon")
async def lg_create(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Utilise ça dans un serveur, dans un salon texte.", ephemeral=True)
        return

    gid = interaction.guild.id
    if gid in GAMES and GAMES[gid].phase != "ended":
        await interaction.response.send_message("Il y a déjà une partie active sur ce serveur.", ephemeral=True)
        return

    game = GameState(guild_id=gid, channel_id=interaction.channel.id, created_by=interaction.user.id)
    GAMES[gid] = game

    await interaction.response.send_message(
        "✅ Partie créée.\n"
        "➡️ Les joueurs font **/lg join**\n"
        "➡️ Quand vous êtes prêts : **/lg start**",
        ephemeral=False
    )


@lg.command(name="join", description="Rejoindre la partie")
async def lg_join(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return

    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie. Fais **/lg create**.", ephemeral=True)
        return

    if game.started:
        await interaction.response.send_message("La partie a déjà commencé.", ephemeral=True)
        return

    uid = interaction.user.id
    if uid in game.players:
        await interaction.response.send_message("Tu es déjà dans la partie.", ephemeral=True)
        return

    game.players[uid] = PlayerState(user_id=uid)
    await interaction.response.send_message(f"✅ {interaction.user.mention} a rejoint. ({len(game.players)} joueurs)", ephemeral=False)


@lg.command(name="leave", description="Quitter la partie (avant le start)")
async def lg_leave(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if game.started:
        await interaction.response.send_message("Impossible de quitter après le start.", ephemeral=True)
        return
    uid = interaction.user.id
    if uid in game.players:
        del game.players[uid]
        await interaction.response.send_message(f"✅ {interaction.user.mention} a quitté. ({len(game.players)} joueurs)", ephemeral=False)
    else:
        await interaction.response.send_message("Tu n’es pas dans la partie.", ephemeral=True)


@lg.command(name="start", description="Démarrer la partie (rôles en DM + nuit)")
async def lg_start(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie. Fais **/lg create**.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut lancer.", ephemeral=True)
        return
    if game.started:
        await interaction.response.send_message("Déjà lancé.", ephemeral=True)
        return
    n = len(game.players)
    if n < 8 or n > 15:
        await interaction.response.send_message("Il faut entre **8 et 15** joueurs.", ephemeral=True)
        return

    roles = role_distribution(n)
    items = list(game.players.items())
    random.shuffle(items)

    for (pid, pstate), role in zip(items, roles):
        pstate.role = role

    game.started = True
    game.phase = "night"
    game.day = 0

    guild = interaction.guild
    ch = channel(game, guild)

    # DM roles
    for pid, p in game.players.items():
        m = get_member(guild, pid)
        if not m:
            continue
        await dm(m, f"🎭 Ton rôle: **{ROLE_LABELS_FR[p.role]}**\nNe le dis à personne.\n")

    await interaction.response.send_message("✅ Partie lancée ! Les rôles ont été envoyés en DM.", ephemeral=False)

    # Narration + start first night
    await ch.send("🌙 **La nuit tombe…** (vous jouez en vocal, actions en DM)")
    await send_night_actions(game, guild, first_night=True)
    await ch.send("⏳ **Nuit**: Voyante / Garde / Loups / Sorcière (si besoin). Quand tout le monde a choisi: **/lg resolve_night**")


async def send_night_actions(game: GameState, guild: discord.Guild, first_night: bool):
    # reset night storage
    game.protected_id = None
    game.seer_target_id = None
    game.wolf_target_id = None
    game.witch_heal_target_id = None
    game.witch_kill_target_id = None
    game.black_infect_target_id = None

    # Cupidon (first night only)
    if first_night and not game.cupid_done:
        cupid_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "cupidon"), None)
        if cupid_id:
            cupid = get_member(guild, cupid_id)
            if cupid:
                view = ActionView(game, guild, cupid_id, "cupidon", "Choisis 2 amoureux 💘", max_values=2, timeout=180)
                await dm(cupid, "💘 **Cupidon**: choisis **2 joueurs** amoureux.")
                try:
                    await cupid.send(view=view)
                except discord.Forbidden:
                    pass

    # Garde
    guard_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "garde"), None)
    if guard_id:
        guard = get_member(guild, guard_id)
        if guard:
            view = ActionView(game, guild, guard_id, "guard", "Qui protèges-tu ? 🛡️", allow_self=True)
            await dm(guard, "🛡️ **Garde**: choisis qui tu protèges cette nuit.")
            try:
                await guard.send(view=view)
            except discord.Forbidden:
                pass

    # Voyante
    seer_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "voyante"), None)
    if seer_id:
        seer = get_member(guild, seer_id)
        if seer:
            view = ActionView(game, guild, seer_id, "seer", "Qui veux-tu voir ? 🔮")
            await dm(seer, "🔮 **Voyante**: choisis un joueur.")
            try:
                await seer.send(view=view)
            except discord.Forbidden:
                pass

    # Loups (chacun choisit, on prend la dernière cible comme choix global)
    wolves = [pid for pid, p in game.players.items() if p.alive and p.role in WOLF_ROLES]
    for wid in wolves:
        wm = get_member(guild, wid)
        if wm:
            view = ActionView(game, guild, wid, "wolves", "Qui tuez-vous ? 🐺")
            await dm(wm, "🐺 **Loups**: choisis une cible. (Le bot prend la dernière cible choisie comme décision des loups.)")
            try:
                await wm.send(view=view)
            except discord.Forbidden:
                pass

    # Loup-garou noir infection (1 fois dans la partie)
    if not game.black_infect_used:
        black_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "loup_garou_noir"), None)
        if black_id:
            bm = get_member(guild, black_id)
            if bm:
                view = ActionView(game, guild, black_id, "black_infect", "Qui veux-tu infecter ? 🖤 (1 fois)", allow_self=False)
                await dm(bm, "🖤🐺 **Loup-Garou Noir**: tu peux **infecter 1 fois** quelqu’un (il devient loup et survit). Si tu ne veux pas, ignore.")
                try:
                    await bm.send(view=view)
                except discord.Forbidden:
                    pass

    # Petite fille (info)
    pf_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "petite_fille"), None)
    if pf_id:
        pf = get_member(guild, pf_id)
        if pf:
            await dm(pf, "👧 **Petite Fille**: tu peux espionner (IRL en vocal) mais attention 😉")


@lg.command(name="resolve_night", description="Résoudre la nuit (après actions)")
async def lg_resolve_night(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut résoudre.", ephemeral=True)
        return
    if game.phase != "night":
        await interaction.response.send_message("Tu n’es pas en phase nuit.", ephemeral=True)
        return

    guild = interaction.guild
    ch = channel(game, guild)

    # Apply Cupidon
    if game.cupid_done and len(game.cupid_targets) == 2:
        a, b = game.cupid_targets
        if a in game.players and b in game.players:
            game.players[a].lover_id = b
            game.players[b].lover_id = a

    # Voyante result
    if game.seer_target_id:
        seer_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "voyante"), None)
        if seer_id:
            seer = get_member(guild, seer_id)
            t = game.players.get(game.seer_target_id)
            tm = get_member(guild, game.seer_target_id)
            if seer and t and tm:
                side = "🐺 **LOUP**" if is_wolf(t.role) else "🌾 **VILLAGE**"
                await dm(seer, f"🔮 Résultat: **{tm.display_name}** est côté {side}.")

    # Determine wolf victim (might be protected)
    victim_id = game.wolf_target_id
    if victim_id and game.protected_id == victim_id:
        victim_id = None

    # Ask Witch now (heal victim / poison someone)
    witch_id = next((pid for pid, p in game.players.items() if p.alive and p.role == "sorciere"), None)
    if witch_id:
        witch = get_member(guild, witch_id)
        if witch:
            if victim_id and not game.witch_heal_used:
                view = ActionView(game, guild, witch_id, "witch_heal", "Soigner qui ? 🧪")
                await dm(witch, "🧪 **Sorcière**: quelqu’un est visé. Si tu veux **soigner**, choisis (souvent la victime). Sinon ignore.")
                try:
                    await witch.send(view=view)
                except discord.Forbidden:
                    pass
            if not game.witch_kill_used:
                view = ActionView(game, guild, witch_id, "witch_kill", "Empoisonner qui ? ☠️")
                await dm(witch, "🧪 **Sorcière**: si tu veux **empoisonner**, choisis une cible. Sinon ignore.")
                try:
                    await witch.send(view=view)
                except discord.Forbidden:
                    pass

    await interaction.response.send_message(
        "✅ Résolution en cours.\n"
        "➡️ Attends 10–30 sec (sorcière + infection si utilisée), puis lance **/lg finish_night**.",
        ephemeral=False
    )
    await ch.send("⏳ **Tour de la Sorcière (≈20s)** puis **/lg finish_night**.")


@lg.command(name="finish_night", description="Appliquer morts/infection + passer au jour")
async def lg_finish_night(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut finir la nuit.", ephemeral=True)
        return
    if game.phase != "night":
        await interaction.response.send_message("Tu n’es pas en phase nuit.", ephemeral=True)
        return

    guild = interaction.guild
    ch = channel(game, guild)

    deaths: Set[int] = set()

    # Base wolves victim (unless protected)
    victim_id = game.wolf_target_id
    if victim_id and game.protected_id == victim_id:
        victim_id = None

    # Witch heal
    if game.witch_heal_target_id and not game.witch_heal_used:
        # If she healed the victim, cancel death if matching
        if victim_id == game.witch_heal_target_id:
            victim_id = None
        game.witch_heal_used = True

    # Black wolf infection (1 time)
    infect_id = game.black_infect_target_id
    if infect_id and not game.black_infect_used:
        # Infection happens only if target alive and not already wolf
        if infect_id in game.players and game.players[infect_id].alive and not is_wolf(game.players[infect_id].role):
            # Infection overrides wolf kill if same target (classic feel)
            game.players[infect_id].role = "loup_garou"
            game.players[infect_id].infected = True
            game.black_infect_used = True
            # If wolves also targeted him, he survives (becomes wolf)
            if victim_id == infect_id:
                victim_id = None

    # Apply wolves victim death
    if victim_id:
        deaths.add(victim_id)

    # Witch poison
    if game.witch_kill_target_id and not game.witch_kill_used:
        deaths.add(game.witch_kill_target_id)
        game.witch_kill_used = True

    # Apply deaths + lovers chain
    final_deaths: Set[int] = set()
    stack = list(deaths)
    while stack:
        d = stack.pop()
        if d in final_deaths:
            continue
        if d not in game.players or not game.players[d].alive:
            continue
        final_deaths.add(d)

        lover = game.players[d].lover_id
        if lover and lover in game.players and game.players[lover].alive:
            stack.append(lover)

    # Mark dead
    for d in final_deaths:
        game.players[d].alive = False

    # Narration
    game.day += 1
    game.phase = "day"

    dead_names = []
    for d in final_deaths:
        m = get_member(guild, d)
        dead_names.append(m.display_name if m else str(d))

    inf_txt = ""
    if game.black_infect_target_id and game.black_infect_used:
        m = get_member(guild, game.black_infect_target_id)
        if m:
            inf_txt = f"\n🖤 Une présence étrange… **{m.display_name}** a été infecté."

    if dead_names:
        await ch.send(f"🌅 **Jour {game.day}** — Cette nuit, sont morts : **{', '.join(dead_names)}**.{inf_txt}")
    else:
        await ch.send(f"🌅 **Jour {game.day}** — Personne n’est mort cette nuit.{inf_txt}")

    # Check victory
    win = check_victory(game)
    if win:
        game.phase = "ended"
        await ch.send(win)
        await ch.send(endgame_roles_text(game, guild))
        await interaction.response.send_message("Partie terminée.", ephemeral=True)
        return

    # If no mayor yet, suggest mayor election
    if game.mayor_id is None:
        await ch.send("👑 **Élection du Maire** : lance **/lg mayor_start** (vote en cours).")
    else:
        mayor_m = get_member(guild, game.mayor_id)
        await ch.send(f"👑 Maire actuel : **{mayor_m.display_name if mayor_m else game.mayor_id}**")

    await ch.send("🗳️ Pour éliminer quelqu’un : lance **/lg dayvote_start** puis les gens font **/lg vote @cible**. Fin : **/lg vote_end**.")
    await interaction.response.send_message("✅ Nuit appliquée, jour lancé.", ephemeral=True)


# ======================
# MAYOR & DAY VOTES
# ======================
@lg.command(name="mayor_start", description="Démarrer le vote du maire")
async def lg_mayor_start(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut lancer.", ephemeral=True)
        return
    if game.phase != "day":
        await interaction.response.send_message("Le vote du maire se fait pendant le jour.", ephemeral=True)
        return

    game.vote = VoteState(active=True, kind="mayor", votes={})
    ch = channel(game, interaction.guild)
    await ch.send("👑 **Vote du Maire ouvert !**\n➡️ Votez avec **/lg vote @pseudo** puis finissez avec **/lg vote_end**.")
    await interaction.response.send_message("Vote du maire ouvert.", ephemeral=True)


@lg.command(name="dayvote_start", description="Démarrer le vote d'élimination du jour")
async def lg_dayvote_start(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut lancer.", ephemeral=True)
        return
    if game.phase != "day":
        await interaction.response.send_message("Le vote d’élimination se fait pendant le jour.", ephemeral=True)
        return

    game.vote = VoteState(active=True, kind="daykill", votes={})
    ch = channel(game, interaction.guild)
    await ch.send("🗳️ **Vote d’élimination ouvert !**\n➡️ Votez avec **/lg vote @pseudo** puis finissez avec **/lg vote_end**.")
    await interaction.response.send_message("Vote d’élimination ouvert.", ephemeral=True)


@lg.command(name="vote", description="Voter (maire ou élimination)")
@app_commands.describe(cible="La personne pour laquelle tu votes")
async def lg_vote(interaction: discord.Interaction, cible: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if not game.vote.active:
        await interaction.response.send_message("Aucun vote en cours.", ephemeral=True)
        return
    if interaction.user.id not in game.players or not game.players[interaction.user.id].alive:
        await interaction.response.send_message("Tu n’es pas (ou plus) dans la partie.", ephemeral=True)
        return
    if cible.id not in game.players or not game.players[cible.id].alive:
        await interaction.response.send_message("Cible invalide (pas dans la partie ou déjà mort).", ephemeral=True)
        return

    game.vote.votes[interaction.user.id] = cible.id
    await interaction.response.send_message(f"✅ Vote enregistré pour **{cible.display_name}**.", ephemeral=True)


def tally_votes(game: GameState) -> Tuple[Optional[int], Dict[int, int]]:
    counts: Dict[int, int] = {}
    for voter, target in game.vote.votes.items():
        weight = 1
        if game.mayor_id and voter == game.mayor_id and game.vote.kind == "daykill":
            weight = 2  # mayor vote double for elimination
        counts[target] = counts.get(target, 0) + weight

    if not counts:
        return None, counts

    # determine top (tie -> None)
    best = max(counts.values())
    top = [t for t, c in counts.items() if c == best]
    if len(top) != 1:
        return None, counts
    return top[0], counts


@lg.command(name="vote_end", description="Fermer le vote en cours et appliquer le résultat")
async def lg_vote_end(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut fermer le vote.", ephemeral=True)
        return
    if not game.vote.active:
        await interaction.response.send_message("Aucun vote en cours.", ephemeral=True)
        return

    guild = interaction.guild
    ch = channel(game, guild)

    winner_id, counts = tally_votes(game)
    game.vote.active = False

    # pretty results
    lines = ["📊 **Résultats du vote :**"]
    for tid, c in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        m = get_member(guild, tid)
        lines.append(f"- **{m.display_name if m else tid}** : {c}")

    await ch.send("\n".join(lines))

    if game.vote.kind == "mayor":
        if winner_id is None:
            await ch.send("⚖️ Égalité. Refaite un vote avec **/lg mayor_start**.")
        else:
            game.mayor_id = winner_id
            m = get_member(guild, winner_id)
            await ch.send(f"👑 **Maire élu : {m.display_name if m else winner_id}**")
        await interaction.response.send_message("Vote du maire terminé.", ephemeral=True)
        return

    # daykill
    if winner_id is None:
        await ch.send("⚖️ Égalité. Personne n’est éliminé. (Relancez **/lg dayvote_start** si vous voulez revoter.)")
    else:
        # kill target + lovers chain
        deaths: Set[int] = {winner_id}
        final: Set[int] = set()
        stack = list(deaths)
        while stack:
            d = stack.pop()
            if d in final:
                continue
            if d not in game.players or not game.players[d].alive:
                continue
            final.add(d)
            lover = game.players[d].lover_id
            if lover and lover in game.players and game.players[lover].alive:
                stack.append(lover)

        for d in final:
            game.players[d].alive = False

        names = []
        for d in final:
            m = get_member(guild, d)
            names.append(m.display_name if m else str(d))
        await ch.send(f"☠️ **Éliminé(s) par le vote : {', '.join(names)}**")

    # victory?
    win = check_victory(game)
    if win:
        game.phase = "ended"
        await ch.send(win)
        await ch.send(endgame_roles_text(game, guild))
        await interaction.response.send_message("Partie terminée.", ephemeral=True)
        return

    # Next night
    game.phase = "night"
    await ch.send("🌙 **La nuit tombe…**")
    await send_night_actions(game, guild, first_night=False)
    await ch.send("⏳ **Nuit**: actions en DM. Quand fini: **/lg resolve_night**")
    await interaction.response.send_message("Vote appliqué, nuit lancée.", ephemeral=True)


# ======================
# ADMIN / STATUS / END
# ======================
@lg.command(name="status", description="Voir l'état de la partie")
async def lg_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game or game.phase == "ended":
        await interaction.response.send_message("Aucune partie active.", ephemeral=True)
        return
    guild = interaction.guild
    alive = alive_ids(game)
    mayor = get_member(guild, game.mayor_id) if game.mayor_id else None
    await interaction.response.send_message(
        f"📌 Phase: **{game.phase}** | Jour: **{game.day}**\n"
        f"👥 Joueurs: **{len(game.players)}** | Vivants: **{len(alive)}**\n"
        f"👑 Maire: **{mayor.display_name if mayor else 'aucun'}**",
        ephemeral=True
    )


@lg.command(name="end", description="Terminer la partie et afficher les rôles")
async def lg_end(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
        return
    game = get_game(interaction.guild.id)
    if not game:
        await interaction.response.send_message("Aucune partie.", ephemeral=True)
        return
    if interaction.user.id != game.created_by:
        await interaction.response.send_message("Seul le créateur de la partie peut terminer.", ephemeral=True)
        return
    game.phase = "ended"
    ch = channel(game, interaction.guild)
    await ch.send("🛑 Partie arrêtée par l’admin.")
    await ch.send(endgame_roles_text(game, interaction.guild))
    await interaction.response.send_message("Partie terminée.", ephemeral=True)


# ======================
# BOT EVENTS
# ======================
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print("Sync failed:", e)
    print(f"Logged in as {bot.user}.")


bot.run(TOKEN)
