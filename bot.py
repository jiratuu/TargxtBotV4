import discord
from discord.ext import commands
from discord.ui import View, Select, ChannelSelect, RoleSelect, TextInput
import json
import os
import asyncio
import random
from datetime import datetime, timedelta
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# SERVEUR HTTP POUR L'HEBERGEMENT
# ============================================================

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), BaseHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# ============================================================
# CONFIG
# ============================================================

PREFIX = "+"
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None
)

COLORS = {
    "warn": discord.Color.orange(),
    "mute": discord.Color.dark_grey(),
    "unmute": discord.Color.green(),
    "ban": discord.Color.red(),
    "help": discord.Color.blue(),
    "sanctions": discord.Color.purple(),
    "lock": discord.Color.dark_red(),
    "unlock": discord.Color.green(),
    "disconnect": discord.Color.gold(),
    "ticket": discord.Color.teal(),
    "giveaway": discord.Color.from_str("#555555"),
    "giveaway_end": discord.Color.from_str("#777777"),
}

TIME_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

# Empêche l'événement on_member_ban de doubler les sanctions
pending_bans = set()


# ============================================================
# OUTILS
# ============================================================

def parse_duration(value: str):
    value = value.strip().lower()

    if not value or value[-1] not in TIME_UNITS:
        return None

    try:
        amount = int(value[:-1])
    except ValueError:
        return None

    if amount < 1:
        return None

    return amount * TIME_UNITS[value[-1]]


def format_duration(seconds: int):
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def send_dm(user: discord.abc.User, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except (discord.HTTPException, discord.Forbidden):
        pass


# ============================================================
# STOCKAGE JSON
# ============================================================

class Store:
    def __init__(self, path: str):
        self.path = path

    def read(self) -> dict:
        if not os.path.exists(self.path):
            return {}

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def write(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=4
            )

    def update(self, mutator):
        data = self.read()
        result = mutator(data)
        self.write(data)
        return result


sanctions_store = Store("sanctions.json")
ticket_store = Store("ticket_config.json")
giveaway_store = Store("giveaways.json")


# ============================================================
# SANCTIONS
# ============================================================

class SanctionManager:

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}_{user_id}"

    @classmethod
    def add(
        cls,
        guild_id: int,
        user_id: int,
        action: str,
        reason: str,
        moderator: str
    ):
        key = cls._key(guild_id, user_id)

        def mutate(data):
            data.setdefault(key, []).append({
                "action": action,
                "reason": reason,
                "moderator": moderator,
                "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
            })

        sanctions_store.update(mutate)

    @classmethod
    def get_all(cls, guild_id: int, user_id: int) -> list:
        return sanctions_store.read().get(
            cls._key(guild_id, user_id),
            []
        )

    @classmethod
    def reset(cls, guild_id: int, user_id: int) -> bool:
        key = cls._key(guild_id, user_id)
        found = {"value": False}

        def mutate(data):
            if key in data:
                del data[key]
                found["value"] = True

        sanctions_store.update(mutate)
        return found["value"]

    @classmethod
    def remove_one(
        cls,
        guild_id: int,
        user_id: int,
        index: int
    ):
        key = cls._key(guild_id, user_id)
        removed = {"value": None}

        def mutate(data):
            entries = data.get(key)

            if entries and 0 < index <= len(entries):
                removed["value"] = entries.pop(index - 1)

                if not entries:
                    del data[key]

        sanctions_store.update(mutate)
        return removed["value"]


# ============================================================
# TICKETS
# ============================================================

class TicketConfigManager:

    @staticmethod
    def get(guild_id: int) -> dict:
        return ticket_store.read().get(
            str(guild_id),
            {}
        )

    @staticmethod
    def set(guild_id: int, key: str, value):
        gid = str(guild_id)

        def mutate(data):
            data.setdefault(gid, {})[key] = value

        ticket_store.update(mutate)

    @staticmethod
    def add_staff_role(guild_id: int, role_id: int):
        gid = str(guild_id)

        def mutate(data):
            data.setdefault(gid, {})

            roles = data[gid].get(
                "staff_role_ids",
                []
            )

            if role_id not in roles:
                roles.append(role_id)

            data[gid]["staff_role_ids"] = roles

        ticket_store.update(mutate)

    @staticmethod
    def remove_staff_role(guild_id: int, role_id: int):
        gid = str(guild_id)

        def mutate(data):
            data.setdefault(gid, {})

            roles = data[gid].get(
                "staff_role_ids",
                []
            )

            if role_id in roles:
                roles.remove(role_id)

            data[gid]["staff_role_ids"] = roles

        ticket_store.update(mutate)

    @staticmethod
    def get_staff_roles(guild_id: int) -> list:
        return TicketConfigManager.get(
            guild_id
        ).get("staff_role_ids", [])


# ============================================================
# GIVEAWAYS
# ============================================================

class GiveawayManager:

    @staticmethod
    def all_running():
        return {
            gid: g
            for gid, g in giveaway_store.read().items()
            if g.get("status") == "running"
        }

    @staticmethod
    def save(message_id: int, data: dict):
        def mutate(store):
            store[str(message_id)] = data

        giveaway_store.update(mutate)

    @staticmethod
    def mark_ended(message_id: int):
        key = str(message_id)

        def mutate(store):
            if key in store:
                store[key]["status"] = "ended"

        giveaway_store.update(mutate)

    @staticmethod
    def delete(message_id: int):
        key = str(message_id)

        def mutate(store):
            store.pop(key, None)

        giveaway_store.update(mutate)

    @staticmethod
    def get(message_id: int):
        return giveaway_store.read().get(
            str(message_id)
        )

    @classmethod
    async def loop(cls):
        await bot.wait_until_ready()

        while not bot.is_closed():
            now = datetime.now().timestamp()

            for gid, giveaway in cls.all_running().items():
                if now >= giveaway["end_timestamp"]:
                    try:
                        await cls.finish(
                            int(gid),
                            giveaway
                        )
                    except Exception as error:
                        print(
                            f"[GIVEAWAY] Erreur: {error}"
                        )

            await asyncio.sleep(5)

    @classmethod
    async def draw_winners(
        cls,
        message: discord.Message,
        count: int
    ):
        reaction = discord.utils.get(
            message.reactions,
            emoji="🎉"
        )

        if not reaction:
            return []

        participants = [
            user
            async for user in reaction.users()
            if user != bot.user
        ]

        if not participants:
            return []

        if len(participants) <= count:
            return participants

        return random.sample(
            participants,
            count
        )

    @classmethod
    async def finish(
        cls,
        message_id: int,
        giveaway: dict
    ):
        guild = bot.get_guild(
            giveaway["guild_id"]
        )

        if not guild:
            return

        channel = guild.get_channel(
            giveaway["channel_id"]
        )

        if not channel:
            return

        try:
            message = await channel.fetch_message(
                message_id
            )
        except discord.NotFound:
            cls.delete(message_id)
            return

        winners = await cls.draw_winners(
            message,
            giveaway["winners"]
        )

        embed = discord.Embed(
            title="GIVEAWAY TERMINE",
            description=(
                f"Prix : **{giveaway['prize']}**\n\n"
                f"Hébergé par : {giveaway['host']}"
            ),
            color=COLORS["giveaway_end"]
        )

        if winners:
            mentions = ", ".join(
                user.mention
                for user in winners
            )

            embed.add_field(
                name="Gagnant(s)",
                value=mentions,
                inline=False
            )

            embed.set_footer(
                text="Félicitations aux gagnants."
            )
        else:
            embed.add_field(
                name="Gagnant(s)",
                value="Aucun participant.",
                inline=False
            )

        embed.timestamp = datetime.fromtimestamp(
            giveaway["end_timestamp"]
        )

        await message.edit(
            embed=embed,
            view=None
        )

        if winners:
            mentions = ", ".join(
                user.mention
                for user in winners
            )

            await channel.send(
                f"Félicitations {mentions}. "
                f"Vous avez gagné **{giveaway['prize']}**."
            )

        cls.mark_ended(message_id)


# ============================================================
# GIVEAWAY MODALS
# ============================================================

class GiveawayDureeModal(
    discord.ui.Modal,
    title="Durée du giveaway"
):

    duree = TextInput(
        label="Durée",
        placeholder="30s, 5m, 2h, 1d",
        max_length=10
    )

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        seconds = parse_duration(
            self.duree.value
        )

        if seconds is None:
            await interaction.response.send_message(
                "Durée invalide. Utilise s, m, h ou d.",
                ephemeral=True
            )
            return

        self.parent.duree = (
            self.duree.value
            .strip()
            .lower()
        )

        self.parent.duree_seconds = seconds

        await interaction.response.edit_message(
            embed=self.parent.build_embed(),
            view=self.parent
        )


class GiveawayGagnantsModal(
    discord.ui.Modal,
    title="Nombre de gagnants"
):

    gagnants = TextInput(
        label="Nombre de gagnants",
        placeholder="1, 2, 3, 5",
        max_length=3
    )

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        try:
            number = int(
                self.gagnants.value.strip()
            )
        except ValueError:
            await interaction.response.send_message(
                "Nombre invalide.",
                ephemeral=True
            )
            return

        if not 1 <= number <= 100:
            await interaction.response.send_message(
                "Entre 1 et 100 gagnants.",
                ephemeral=True
            )
            return

        self.parent.gagnants = number

        await interaction.response.edit_message(
            embed=self.parent.build_embed(),
            view=self.parent
        )


class GiveawayPrixModal(
    discord.ui.Modal,
    title="Prix du giveaway"
):

    prix = TextInput(
        label="Prix à gagner",
        placeholder="Nitro, 50€, rôle exclusif",
        max_length=100
    )

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        self.parent.prix = (
            self.prix.value.strip()
        )

        await interaction.response.edit_message(
            embed=self.parent.build_embed(),
            view=self.parent
        )


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawaySetupView(discord.ui.View):

    def __init__(self, ctx):
        super().__init__(timeout=300)

        self.ctx = ctx
        self.salon = None
        self.duree = None
        self.duree_seconds = 0
        self.gagnants = 1
        self.prix = "Non défini"

    def _guard(
        self,
        interaction: discord.Interaction
    ):
        return interaction.user == self.ctx.author

    def build_embed(self):
        embed = discord.Embed(
            title="Configuration du Giveaway",
            description=(
                "Configure le giveaway avec "
                "les boutons ci-dessous."
            ),
            color=COLORS["giveaway"]
        )

        embed.add_field(
            name="Salon",
            value=(
                self.salon.mention
                if self.salon
                else "Non défini"
            ),
            inline=False
        )

        embed.add_field(
            name="Durée",
            value=self.duree or "Non définie",
            inline=False
        )

        embed.add_field(
            name="Gagnant(s)",
            value=str(self.gagnants),
            inline=False
        )

        embed.add_field(
            name="Prix",
            value=self.prix,
            inline=False
        )

        embed.set_footer(
            text="Configure tous les champs puis lance le giveaway."
        )

        return embed

    @discord.ui.button(
        label="Salon",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def set_salon(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Mentionne le salon où envoyer le giveaway.",
            ephemeral=True
        )

        def check(message):
            return (
                message.author == self.ctx.author
                and message.channel == self.ctx.channel
            )

        try:
            message = await self.ctx.bot.wait_for(
                "message",
                timeout=30,
                check=check
            )
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content="Temps écoulé.",
                embed=self.build_embed(),
                view=self
            )
            return

        if message.channel_mentions:
            self.salon = message.channel_mentions[0]

            try:
                await message.delete()
            except discord.HTTPException:
                pass

            await interaction.edit_original_response(
                content="Salon défini.",
                embed=self.build_embed(),
                view=self
            )
        else:
            await interaction.edit_original_response(
                content="Salon invalide.",
                embed=self.build_embed(),
                view=self
            )

    @discord.ui.button(
        label="Durée",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def set_duree(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            GiveawayDureeModal(self)
        )

    @discord.ui.button(
        label="Gagnants",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def set_gagnants(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            GiveawayGagnantsModal(self)
        )

    @discord.ui.button(
        label="Prix",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def set_prix(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            GiveawayPrixModal(self)
        )

    @discord.ui.button(
        label="Lancer",
        style=discord.ButtonStyle.success,
        row=2
    )
    async def lancer(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        if not self.salon:
            await interaction.response.send_message(
                "Configure d'abord le salon.",
                ephemeral=True
            )
            return

        if not self.duree:
            await interaction.response.send_message(
                "Configure d'abord la durée.",
                ephemeral=True
            )
            return

        if self.prix == "Non défini":
            await interaction.response.send_message(
                "Configure d'abord le prix.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        end_timestamp = (
            datetime.now()
            + timedelta(seconds=self.duree_seconds)
        ).timestamp()

        embed = discord.Embed(
            title="GIVEAWAY",
            description=(
                f"**{self.prix}**\n\n"
                f"Gagnant(s) : **{self.gagnants}**\n"
                f"Se termine : "
                f"<t:{int(end_timestamp)}:R>\n"
                f"Hébergé par : {self.ctx.author.mention}"
            ),
            color=COLORS["giveaway"]
        )

        embed.set_footer(
            text="Réagis avec 🎉 pour participer."
        )

        embed.timestamp = datetime.fromtimestamp(
            end_timestamp
        )

        message = await self.salon.send(
            embed=embed
        )

        await message.add_reaction("🎉")

        GiveawayManager.save(
            message.id,
            {
                "guild_id": self.ctx.guild.id,
                "channel_id": self.salon.id,
                "host": self.ctx.author.mention,
                "prize": self.prix,
                "winners": self.gagnants,
                "end_timestamp": end_timestamp,
                "status": "running",
            }
        )

        confirm = discord.Embed(
            title="Giveaway lancé",
            description=(
                f"Giveaway envoyé dans "
                f"{self.salon.mention}."
            ),
            color=discord.Color.green()
        )

        confirm.add_field(
            name="Prix",
            value=self.prix,
            inline=True
        )

        confirm.add_field(
            name="Durée",
            value=self.duree,
            inline=True
        )

        confirm.add_field(
            name="Gagnants",
            value=str(self.gagnants),
            inline=True
        )

        await self.ctx.send(
            embed=confirm
        )

        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Annuler",
        style=discord.ButtonStyle.danger,
        row=2
    )
    async def annuler(
        self,
        interaction: discord.Interaction,
        _button
    ):
        if not self._guard(interaction):
            await interaction.response.send_message(
                "Ce n'est pas ton panneau.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Giveaway annulé.",
            ephemeral=True
        )

        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# ============================================================
# TICKETS
# ============================================================

TICKET_TOPICS = [
    discord.SelectOption(
        label="Contactez le staff",
        description="Poser une question au staff ou autre."
    ),
    discord.SelectOption(
        label="Partenariat",
        description="Demande un partenariat."
    ),
    discord.SelectOption(
        label="Achat",
        description="Question ou demande concernant un achat."
    ),
    discord.SelectOption(
        label="Osint",
        description="Demande ou question concernant l'OSINT."
    ),
    discord.SelectOption(
        label="Autre...",
        description="Autre demande non incluse."
    ),
]


class TicketDescriptionModal(
    discord.ui.Modal,
    title="Ouvrir un ticket"
):

    description = TextInput(
        label="Description",
        placeholder="Explique brièvement ta demande.",
        style=discord.TextStyle.long,
        max_length=2000
    )

    def __init__(self, sujet: str):
        super().__init__()
        self.sujet = sujet

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        guild = interaction.guild
        user = interaction.user

        if guild is None:
            return

        cfg = TicketConfigManager.get(
            guild.id
        )

        category = discord.utils.get(
            guild.categories,
            id=cfg.get("category_id")
        )

        if not category:
            await interaction.response.send_message(
                "Tickets non configurés. Demande à un admin.",
                ephemeral=True
            )
            return

        safe_name = (
            user.name
            .lower()
            .replace(" ", "-")
            .replace("_", "-")
        )

        chan_name = (
            f"ticket-{safe_name}-{user.id}"
        )

        if discord.utils.get(
            guild.text_channels,
            name=chan_name
        ):
            await interaction.response.send_message(
                "Tu as déjà un ticket ouvert.",
                ephemeral=True
            )
            return

        staff_role_ids = (
            TicketConfigManager.get_staff_roles(
                guild.id
            )
        )

        # Réponse immédiate pour éviter les timeouts
        await interaction.response.send_message(
            "Création de ton ticket...",
            ephemeral=True
        )

        overwrites = {
            guild.default_role:
                discord.PermissionOverwrite(
                    view_channel=False
                ),
            user:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                ),
        }

        if guild.me:
            overwrites[guild.me] = (
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )
            )

        for role_id in staff_role_ids:
            role = guild.get_role(role_id)

            if role:
                overwrites[role] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
                )

        try:
            channel = await guild.create_text_channel(
                name=chan_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket de {user}"
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=(
                    "Je n'ai pas la permission "
                    "de créer des salons."
                )
            )
            return
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Impossible de créer le ticket."
            )
            return

        ticket_message = cfg.get(
            "ticket_message",
            "Bienvenue. Un membre du staff va vous répondre."
        )

        staff_mentions = []

        for role_id in staff_role_ids:
            role = guild.get_role(role_id)

            if role:
                staff_mentions.append(
                    role.mention
                )

        mention_staff = (
            " ".join(staff_mentions)
            if staff_mentions
            else ""
        )

        embed = discord.Embed(
            title="Nouveau ticket",
            description=ticket_message,
            color=COLORS["ticket"]
        )

        embed.add_field(
            name="Utilisateur",
            value=user.mention,
            inline=True
        )

        embed.add_field(
            name="Sujet",
            value=self.sujet,
            inline=True
        )

        if staff_mentions:
            embed.add_field(
                name="Staff",
                value=", ".join(staff_mentions),
                inline=True
            )

        embed.set_footer(
            text="Utilise le bouton ci-dessous pour fermer le ticket."
        )

        embed2 = discord.Embed(
            title="Description du ticket",
            description=(
                self.description.value
                if self.description.value
                else "Aucune description fournie."
            ),
            color=discord.Color.light_grey()
        )

        content = (
            f"{user.mention} {mention_staff}".strip()
        )

        await channel.send(
            content=content,
            embed=embed,
            view=CloseTicketView()
        )

        await channel.send(
            embed=embed2
        )

        await interaction.edit_original_response(
            content=(
                f"Ton ticket a été créé : "
                f"{channel.mention}"
            )
        )


class TicketSubjectSelect(
    discord.ui.View
):

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Choisis le sujet de ton ticket.",
        options=TICKET_TOPICS,
        custom_id="ticket_subject_select"
    )
    async def select_subject(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select
    ):
        await interaction.response.send_modal(
            TicketDescriptionModal(
                select.values[0]
            )
        )


class TicketView(
    discord.ui.View
):

    def __init__(self):
        # Persistant
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ouvrir un ticket",
        style=discord.ButtonStyle.primary,
        custom_id="open_ticket"
    )
    async def open_ticket(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button
    ):
        cfg = TicketConfigManager.get(
            interaction.guild_id
        )

        if not cfg.get("category_id"):
            await interaction.response.send_message(
                "Tickets non configurés. Demande à un admin.",
                ephemeral=True
            )
            return

        # Réponse immédiate à Discord
        await interaction.response.defer(
            ephemeral=True
        )

        embed = discord.Embed(
            title="Choisis un sujet",
            description=(
                "Sélectionne le sujet de ton ticket "
                "dans le menu ci-dessous."
            ),
            color=COLORS["ticket"]
        )

        await interaction.followup.send(
            embed=embed,
            view=TicketSubjectSelect(),
            ephemeral=True
        )


class CloseTicketView(
    discord.ui.View
):

    def __init__(self):
        # Persistant
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        custom_id="close_ticket"
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Confirmer la fermeture ?",
            view=ConfirmCloseView(
                interaction.channel
            ),
            ephemeral=True
        )


class ConfirmCloseView(
    discord.ui.View
):

    def __init__(self, channel):
        super().__init__(timeout=30)
        self.channel = channel

    @discord.ui.button(
        label="Oui, fermer",
        style=discord.ButtonStyle.danger
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button
    ):
        await interaction.response.send_message(
            "Fermeture du ticket dans 5 secondes."
        )

        await asyncio.sleep(5)

        try:
            await self.channel.delete(
                reason=f"Fermé par {interaction.user}"
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Annuler",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button
    ):
        await interaction.response.edit_message(
            content="Fermeture annulée.",
            view=None
        )


class TicketMessageModal(
    discord.ui.Modal,
    title="Modifier le message du ticket"
):

    message = TextInput(
        label="Message de bienvenue",
        style=discord.TextStyle.long,
        placeholder=(
            "Bienvenue dans votre ticket. "
            "Un membre du staff va vous répondre."
        ),
        max_length=4000
    )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        TicketConfigManager.set(
            interaction.guild_id,
            "ticket_message",
            self.message.value
        )

        embed = discord.Embed(
            title="Message mis à jour",
            description=(
                f"Nouveau message :\n"
                f"{self.message.value}"
            ),
            color=discord.Color.green()
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


class AddStaffRoleSelect(
    discord.ui.View
):

    def __init__(self, guild):
        super().__init__(timeout=120)
        self.guild = guild

    @discord.ui.select(
        cls=RoleSelect,
        placeholder="Ajouter un rôle staff.",
        custom_id="add_staff_role_select"
    )
    async def select_role(
        self,
        interaction: discord.Interaction,
        select: RoleSelect
    ):
        role = select.values[0]

        TicketConfigManager.add_staff_role(
            interaction.guild_id,
            role.id
        )

        await interaction.response.send_message(
            f"Rôle {role.mention} ajouté.",
            ephemeral=True
        )


class RemoveStaffRoleSelect(
    discord.ui.View
):

    def __init__(self, guild):
        super().__init__(timeout=120)

        self.guild = guild
        self.options = []

        if guild:
            staff_role_ids = (
                TicketConfigManager.get_staff_roles(
                    guild.id
                )
            )

            for role_id in staff_role_ids:
                role = guild.get_role(role_id)

                if role:
                    self.options.append(
                        discord.SelectOption(
                            label=role.name[:100],
                            value=str(role.id)
                        )
                    )

        if not self.options:
            self.options = [
                discord.SelectOption(
                    label="Aucun rôle staff",
                    value="none"
                )
            ]

        self.remove_role_select.options = (
            self.options
        )

    @discord.ui.select(
        placeholder="Retirer un rôle staff.",
        custom_id="remove_staff_role_select"
    )
    async def remove_role_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select
    ):
        if select.values[0] == "none":
            await interaction.response.send_message(
                "Aucun rôle staff configuré.",
                ephemeral=True
            )
            return

        role_id = int(
            select.values[0]
        )

        TicketConfigManager.remove_staff_role(
            interaction.guild_id,
            role_id
        )

        role = (
            self.guild.get_role(role_id)
            if self.guild
            else None
        )

        await interaction.response.send_message(
            f"Rôle {role.mention if role else role_id} retiré.",
            ephemeral=True
        )


class ConfigPanelView(
    discord.ui.View
):

    def __init__(self, guild):
        super().__init__(timeout=300)
        self.guild = guild

    @discord.ui.select(
        cls=ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Choisir la catégorie des tickets."
    )
    async def select_category(
        self,
        interaction: discord.Interaction,
        select: ChannelSelect
    ):
        category = select.values[0]

        TicketConfigManager.set(
            interaction.guild_id,
            "category_id",
            category.id
        )

        embed = discord.Embed(
            title="Catégorie définie",
            description=(
                f"Catégorie : {category.mention}"
            ),
            color=discord.Color.green()
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

        await interaction.followup.send(
            "Catégorie sauvegardée.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Ajouter un rôle staff",
        style=discord.ButtonStyle.success,
        row=1
    )
    async def add_staff_role(
        self,
        interaction: discord.Interaction,
        _button
    ):
        await interaction.response.send_message(
            "Sélectionne un rôle à ajouter comme staff.",
            view=AddStaffRoleSelect(
                self.guild
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="Retirer un rôle staff",
        style=discord.ButtonStyle.danger,
        row=1
    )
    async def remove_staff_role(
        self,
        interaction: discord.Interaction,
        _button
    ):
        staff_role_ids = (
            TicketConfigManager.get_staff_roles(
                interaction.guild_id
            )
        )

        if not staff_role_ids:
            await interaction.response.send_message(
                "Aucun rôle staff configuré.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Sélectionne un rôle à retirer.",
            view=RemoveStaffRoleSelect(
                self.guild
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="Modifier le message du ticket",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def edit_message(
        self,
        interaction: discord.Interaction,
        _button
    ):
        await interaction.response.send_modal(
            TicketMessageModal()
        )

    @discord.ui.button(
        label="Envoyer le bouton ticket",
        style=discord.ButtonStyle.success,
        row=2
    )
    async def send_ticket_btn(
        self,
        interaction: discord.Interaction,
        _button
    ):
        cfg = TicketConfigManager.get(
            interaction.guild_id
        )

        if not cfg.get("category_id"):
            await interaction.response.send_message(
                "Configure d'abord la catégorie.",
                ephemeral=True
            )
            return

        await interaction.channel.send(
            embed=build_ticket_intro_embed(),
            view=TicketView()
        )

        await interaction.response.send_message(
            "Bouton ticket envoyé.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Voir la config actuelle",
        style=discord.ButtonStyle.secondary,
        row=2
    )
    async def show_config(
        self,
        interaction: discord.Interaction,
        _button
    ):
        cfg = TicketConfigManager.get(
            interaction.guild_id
        )

        category = discord.utils.get(
            self.guild.categories,
            id=cfg.get("category_id")
        )

        staff_role_ids = (
            TicketConfigManager.get_staff_roles(
                interaction.guild_id
            )
        )

        staff_roles = [
            self.guild.get_role(role_id)
            for role_id in staff_role_ids
            if self.guild.get_role(role_id)
        ]

        message = cfg.get(
            "ticket_message",
            "Message par défaut"
        )

        embed = discord.Embed(
            title="Configuration actuelle",
            color=COLORS["ticket"]
        )

        embed.add_field(
            name="Catégorie",
            value=(
                category.mention
                if category
                else "Non définie"
            ),
            inline=False
        )

        roles_str = (
            "\n".join(
                role.mention
                for role in staff_roles
            )
            if staff_roles
            else "Aucun rôle staff"
        )

        embed.add_field(
            name="Rôles staff",
            value=roles_str,
            inline=False
        )

        embed.add_field(
            name="Message",
            value=(
                message[:100] + "..."
                if len(message) > 100
                else message
            ),
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @discord.ui.button(
        label="Fermer le panneau",
        style=discord.ButtonStyle.danger,
        row=2
    )
    async def close_panel(
        self,
        interaction: discord.Interaction,
        _button
    ):
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            "Panneau fermé.",
            ephemeral=True
        )


def build_ticket_intro_embed():
    embed = discord.Embed(
        title="TARGXT - Support",
        description=(
            "Clique sur le bouton ci-dessous "
            "pour contacter le staff.\n\n"
            "N'abuse pas du système de tickets."
        ),
        color=COLORS["ticket"]
    )

    embed.set_footer(
        text="TARGXT - Support"
    )

    return embed


# ============================================================
# HELP
# ============================================================

@bot.command(name="help")
async def help_command(
    ctx: commands.Context
):
    embed = discord.Embed(
        title="Bot Modération - Aide",
        description=(
            f"Préfixe : `{PREFIX}`\n"
            "Commandes disponibles :"
        ),
        color=COLORS["help"]
    )

    embed.set_thumbnail(
        url=ctx.bot.user.display_avatar.url
    )

    sections = {
        "Sanctions": (
            f"`{PREFIX}warn <membre> [raison]` - Avertir\n"
            f"`{PREFIX}mute <membre> <durée> [raison]` - Mute temporaire\n"
            f"`{PREFIX}unmute <membre>` - Retirer le mute\n"
            f"`{PREFIX}ban <membre> [raison]` - Bannir\n"
            f"`{PREFIX}softban <membre> [raison]` - Softban\n"
            f"`{PREFIX}unban <utilisateur>` - Débannir\n"
            f"`{PREFIX}unbanall` - Débannir tous les bannis"
        ),
        "Gestion des sanctions": (
            f"`{PREFIX}sanctions <membre>` - Voir les sanctions\n"
            f"`{PREFIX}resetsanctions <membre>` - Supprimer les sanctions\n"
            f"`{PREFIX}removesanction <membre> <numéro>` - Retirer une sanction"
        ),
        "Giveaways": (
            f"`{PREFIX}giveaway` - Panneau giveaway\n"
            f"`{PREFIX}gend <id>` - Terminer un giveaway\n"
            f"`{PREFIX}reroll <id>` - Nouveau tirage"
        ),
        "Salons": (
            f"`{PREFIX}lock` - Verrouiller\n"
            f"`{PREFIX}unlock` - Déverrouiller"
        ),
        "Vocal": (
            f"`{PREFIX}disconnectall` - Déconnecter les membres"
        ),
        "Tickets": (
            f"`{PREFIX}sendticket [#salon]` - Envoyer le panneau ticket\n"
            f"`{PREFIX}close` - Fermer le ticket\n"
            f"`{PREFIX}panel` - Configuration des tickets"
        ),
        "Divers": (
            f"`{PREFIX}clear <nombre>` - Supprimer des messages"
        ),
    }

    for name, value in sections.items():
        embed.add_field(
            name=name,
            value=value,
            inline=False
        )

    embed.set_footer(
        text="Taper +help pour afficher cette aide."
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# SANCTIONS - WARN
# ============================================================

@bot.command()
@commands.has_permissions(kick_members=True)
async def warn(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason="Aucune raison fournie"
):
    SanctionManager.add(
        ctx.guild.id,
        member.id,
        "Warn",
        reason,
        str(ctx.author)
    )

    embed = discord.Embed(
        title="Avertissement",
        description=(
            f"{member.mention} a été averti."
        ),
        color=COLORS["warn"]
    )

    embed.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    embed.add_field(
        name="Modérateur",
        value=ctx.author.mention,
        inline=False
    )

    await ctx.send(
        embed=embed
    )

    dm = discord.Embed(
        title=f"Avertissement - {ctx.guild.name}",
        description="Tu as reçu un avertissement.",
        color=COLORS["warn"]
    )

    dm.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    dm.add_field(
        name="Modérateur",
        value=str(ctx.author),
        inline=False
    )

    await send_dm(
        member,
        dm
    )


# ============================================================
# MUTE
# ============================================================

@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(
    ctx: commands.Context,
    member: discord.Member,
    duration: str,
    *,
    reason="Aucune raison fournie"
):
    seconds = parse_duration(
        duration
    )

    if seconds is None:
        await ctx.send(
            "Durée invalide. Utilise s, m, h ou d. "
            "Exemple : +mute @user 10m"
        )
        return

    until = (
        discord.utils.utcnow()
        + timedelta(seconds=seconds)
    )

    try:
        await member.timeout(
            until,
            reason=reason
        )
    except discord.Forbidden:
        await ctx.send(
            "Je n'ai pas la permission de mute ce membre."
        )
        return

    SanctionManager.add(
        ctx.guild.id,
        member.id,
        "Mute",
        f"{duration} - {reason}",
        str(ctx.author)
    )

    embed = discord.Embed(
        title="Mute temporaire",
        description=(
            f"{member.mention} est maintenant muet."
        ),
        color=COLORS["mute"]
    )

    embed.add_field(
        name="Durée",
        value=duration,
        inline=True
    )

    embed.add_field(
        name="Raison",
        value=reason,
        inline=True
    )

    embed.add_field(
        name="Modérateur",
        value=ctx.author.mention,
        inline=False
    )

    await ctx.send(
        embed=embed
    )

    dm = discord.Embed(
        title=f"Mute - {ctx.guild.name}",
        description="Tu as été mute.",
        color=COLORS["mute"]
    )

    dm.add_field(
        name="Durée",
        value=duration,
        inline=True
    )

    dm.add_field(
        name="Raison",
        value=reason,
        inline=True
    )

    dm.add_field(
        name="Expire",
        value=(
            f"<t:{int(until.timestamp())}:R>"
        ),
        inline=False
    )

    await send_dm(
        member,
        dm
    )


# ============================================================
# UNMUTE
# ============================================================

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(
    ctx: commands.Context,
    member: discord.Member
):
    if not member.is_timed_out():
        await ctx.send(
            f"{member.mention} n'est pas muet."
        )
        return

    try:
        await member.timeout(None)
    except discord.Forbidden:
        await ctx.send(
            "Je n'ai pas la permission de retirer le mute."
        )
        return

    embed = discord.Embed(
        title="Unmute",
        description=(
            f"{member.mention} n'est plus muet."
        ),
        color=COLORS["unmute"]
    )

    embed.add_field(
        name="Modérateur",
        value=ctx.author.mention,
        inline=False
    )

    await ctx.send(
        embed=embed
    )

    await send_dm(
        member,
        discord.Embed(
            title=f"Unmute - {ctx.guild.name}",
            description="Tu n'es plus muet.",
            color=COLORS["unmute"]
        )
    )


# ============================================================
# BAN NORMAL
# ============================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason="Aucune raison fournie"
):
    pending_bans.add(
        (ctx.guild.id, member.id)
    )

    try:
        await member.ban(
            reason=reason
        )
    except discord.Forbidden:
        pending_bans.discard(
            (ctx.guild.id, member.id)
        )

        await ctx.send(
            "Je n'ai pas la permission de bannir ce membre."
        )
        return
    except discord.HTTPException:
        pending_bans.discard(
            (ctx.guild.id, member.id)
        )

        await ctx.send(
            "Le bannissement a échoué."
        )
        return

    SanctionManager.add(
        ctx.guild.id,
        member.id,
        "Ban",
        reason,
        str(ctx.author)
    )

    pending_bans.discard(
        (ctx.guild.id, member.id)
    )

    embed = discord.Embed(
        title="Bannissement",
        description=(
            f"{member.mention} a été banni."
        ),
        color=COLORS["ban"]
    )

    embed.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    embed.add_field(
        name="Modérateur",
        value=ctx.author.mention,
        inline=False
    )

    await ctx.send(
        embed=embed
    )

    dm = discord.Embed(
        title=f"Bannissement - {ctx.guild.name}",
        description=(
            "Tu as été banni du serveur."
        ),
        color=COLORS["ban"]
    )

    dm.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    dm.add_field(
        name="Modérateur",
        value=str(ctx.author),
        inline=False
    )

    await send_dm(
        member,
        dm
    )


# ============================================================
# SOFTBAN
# ============================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def softban(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason="Aucune raison fournie"
):
    pending_bans.add(
        (ctx.guild.id, member.id)
    )

    try:
        await member.ban(
            reason=reason
        )
    except discord.Forbidden:
        pending_bans.discard(
            (ctx.guild.id, member.id)
        )

        await ctx.send(
            "Je n'ai pas la permission de bannir ce membre."
        )
        return
    except discord.HTTPException:
        pending_bans.discard(
            (ctx.guild.id, member.id)
        )

        await ctx.send(
            "Le softban a échoué."
        )
        return

    try:
        await ctx.guild.unban(
            discord.Object(id=member.id),
            reason=f"Softban - {reason}"
        )
    except discord.HTTPException:
        pending_bans.discard(
            (ctx.guild.id, member.id)
        )

        await ctx.send(
            "Le membre a été banni mais le débannissement automatique a échoué."
        )
        return

    SanctionManager.add(
        ctx.guild.id,
        member.id,
        "Softban",
        reason,
        str(ctx.author)
    )

    pending_bans.discard(
        (ctx.guild.id, member.id)
    )

    embed = discord.Embed(
        title="Softban",
        description=(
            f"{member.mention} a été softban."
        ),
        color=COLORS["ban"]
    )

    embed.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    embed.add_field(
        name="Modérateur",
        value=ctx.author.mention,
        inline=False
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# BAN EXTERNE
# ============================================================

@bot.event
async def on_member_ban(
    guild: discord.Guild,
    user: discord.abc.User
):
    key = (
        guild.id,
        user.id
    )

    # Les commandes +ban/+softban gèrent déjà leur sanction
    if key in pending_bans:
        pending_bans.discard(key)
        return

    reason = "Aucune raison fournie"
    moderator = "Inconnu"

    try:
        async for entry in guild.audit_logs(
            action=discord.AuditLogAction.ban,
            limit=5
        ):
            if entry.target.id == user.id:
                reason = (
                    entry.reason
                    or reason
                )
                moderator = str(
                    entry.user
                )
                break
    except discord.Forbidden:
        pass

    SanctionManager.add(
        guild.id,
        user.id,
        "Ban externe",
        reason,
        moderator
    )

    dm = discord.Embed(
        title=f"Bannissement - {guild.name}",
        description="Tu as été banni du serveur.",
        color=COLORS["ban"]
    )

    dm.add_field(
        name="Raison",
        value=reason,
        inline=False
    )

    dm.add_field(
        name="Modérateur",
        value=moderator,
        inline=False
    )

    await send_dm(
        user,
        dm
    )


# ============================================================
# UNBAN
# ============================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(
    ctx: commands.Context,
    *,
    user_input: str
):
    async for entry in ctx.guild.bans():
        user = entry.user

        if user_input in (
            str(user),
            user.name,
            str(user.id)
        ):
            try:
                await ctx.guild.unban(
                    user
                )
            except discord.HTTPException:
                await ctx.send(
                    "Impossible de débannir cet utilisateur."
                )
                return

            await ctx.send(
                f"{user} a été débanni."
            )
            return

    await ctx.send(
        "Utilisateur non trouvé dans la liste des bannis."
    )


# ============================================================
# UNBAN ALL
# ============================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def unbanall(
    ctx: commands.Context
):
    banned = [
        entry
        async for entry in ctx.guild.bans()
    ]

    if not banned:
        await ctx.send(
            "Aucun banni."
        )
        return

    count = 0

    for entry in banned:
        try:
            await ctx.guild.unban(
                entry.user
            )
            count += 1
        except discord.HTTPException:
            pass

    await ctx.send(
        f"{count} utilisateur(s) débanni(s)."
    )


# ============================================================
# DISCONNECT ALL
# ============================================================

@bot.command()
@commands.has_permissions(move_members=True)
async def disconnectall(
    ctx: commands.Context
):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send(
            "Tu dois être dans un salon vocal."
        )
        return

    channel = ctx.author.voice.channel
    count = 0

    for member in channel.members:
        if member == ctx.bot.user:
            continue

        try:
            await member.move_to(None)
            count += 1
        except discord.HTTPException:
            pass

    await ctx.send(
        embed=discord.Embed(
            title="Déconnexion",
            description=(
                f"{count} membre(s) déconnecté(s)."
            ),
            color=COLORS["disconnect"]
        )
    )


# ============================================================
# LOCK / UNLOCK
# ============================================================

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(
    ctx: commands.Context
):
    overwrite = (
        ctx.channel.overwrites_for(
            ctx.guild.default_role
        )
    )

    overwrite.send_messages = False

    await ctx.channel.set_permissions(
        ctx.guild.default_role,
        overwrite=overwrite
    )

    await ctx.send(
        embed=discord.Embed(
            title="Salon verrouillé",
            description=(
                f"{ctx.channel.mention} est verrouillé."
            ),
            color=COLORS["lock"]
        )
    )


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(
    ctx: commands.Context
):
    overwrite = (
        ctx.channel.overwrites_for(
            ctx.guild.default_role
        )
    )

    overwrite.send_messages = None

    await ctx.channel.set_permissions(
        ctx.guild.default_role,
        overwrite=overwrite
    )

    await ctx.send(
        embed=discord.Embed(
            title="Salon déverrouillé",
            description=(
                f"{ctx.channel.mention} est déverrouillé."
            ),
            color=COLORS["unlock"]
        )
    )


# ============================================================
# SANCTIONS
# ============================================================

@bot.command()
async def sanctions(
    ctx: commands.Context,
    member: discord.Member
):
    entries = SanctionManager.get_all(
        ctx.guild.id,
        member.id
    )

    if not entries:
        await ctx.send(
            f"{member.mention} : aucune sanction."
        )
        return

    embed = discord.Embed(
        title=f"Sanctions de {member.display_name}",
        description=f"Total : {len(entries)}",
        color=COLORS["sanctions"]
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    for index, sanction in enumerate(
        entries,
        1
    ):
        embed.add_field(
            name=(
                f"{index}. "
                f"{sanction['action']} - "
                f"{sanction['date']}"
            ),
            value=(
                f"Raison : {sanction['reason']}\n"
                f"Modérateur : {sanction['moderator']}"
            ),
            inline=False
        )

    await ctx.send(
        embed=embed
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def resetsanctions(
    ctx: commands.Context,
    member: discord.Member
):
    if SanctionManager.reset(
        ctx.guild.id,
        member.id
    ):
        await ctx.send(
            embed=discord.Embed(
                title="Sanctions réinitialisées",
                description=(
                    f"Toutes les sanctions de "
                    f"{member.mention} ont été supprimées."
                ),
                color=COLORS["sanctions"]
            )
        )
    else:
        await ctx.send(
            f"{member.mention} n'a aucune sanction."
        )


@bot.command()
@commands.has_permissions(administrator=True)
async def removesanction(
    ctx: commands.Context,
    member: discord.Member,
    numero: int
):
    removed = SanctionManager.remove_one(
        ctx.guild.id,
        member.id,
        numero
    )

    if not removed:
        await ctx.send(
            f"Sanction #{numero} introuvable."
        )
        return

    embed = discord.Embed(
        title="Sanction retirée",
        description=(
            f"Sanction #{numero} retirée "
            f"pour {member.mention}."
        ),
        color=COLORS["sanctions"]
    )

    embed.add_field(
        name="Action",
        value=removed["action"],
        inline=True
    )

    embed.add_field(
        name="Raison",
        value=removed["reason"],
        inline=True
    )

    embed.add_field(
        name="Date",
        value=removed["date"],
        inline=True
    )

    await ctx.send(
        embed=embed
    )


# ============================================================
# CLEAR
# ============================================================

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(
    ctx: commands.Context,
    amount: int
):
    if not 1 <= amount <= 100:
        await ctx.send(
            "Entre un nombre entre 1 et 100."
        )
        return

    deleted = await ctx.channel.purge(
        limit=amount + 1
    )

    await ctx.send(
        f"{max(0, len(deleted) - 1)} message(s) supprimé(s).",
        delete_after=3
    )


# ============================================================
# GIVEAWAYS COMMANDS
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def giveaway(
    ctx: commands.Context
):
    view = GiveawaySetupView(ctx)

    await ctx.send(
        embed=view.build_embed(),
        view=view
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def gend(
    ctx: commands.Context,
    message_id: int = None
):
    if message_id is None:
        await ctx.send(
            f"Utilisation : {PREFIX}gend <id_du_message>"
        )
        return

    giveaway_data = GiveawayManager.get(
        message_id
    )

    if not giveaway_data:
        await ctx.send(
            "Giveaway introuvable ou déjà supprimé."
        )
        return

    if giveaway_data["status"] != "running":
        await ctx.send(
            "Ce giveaway est déjà terminé."
        )
        return

    await GiveawayManager.finish(
        message_id,
        giveaway_data
    )

    await ctx.send(
        f"Giveaway terminé. Résultats dans "
        f"<#{giveaway_data['channel_id']}>."
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def reroll(
    ctx: commands.Context,
    message_id: int = None
):
    if message_id is None:
        await ctx.send(
            f"Utilisation : {PREFIX}reroll <id_du_message>"
        )
        return

    giveaway_data = GiveawayManager.get(
        message_id
    )

    if not giveaway_data:
        await ctx.send(
            "Giveaway introuvable."
        )
        return

    channel = ctx.guild.get_channel(
        giveaway_data["channel_id"]
    )

    if not channel:
        await ctx.send(
            "Salon du giveaway introuvable."
        )
        return

    try:
        message = await channel.fetch_message(
            message_id
        )
    except discord.NotFound:
        await ctx.send(
            "Message introuvable."
        )
        return

    winners = await GiveawayManager.draw_winners(
        message,
        giveaway_data["winners"]
    )

    if not winners:
        await ctx.send(
            "Aucun participant."
        )
        return

    mentions = ", ".join(
        user.mention
        for user in winners
    )

    await ctx.send(
        f"Nouveau tirage. Félicitations {mentions}. "
        f"Vous avez gagné **{giveaway_data['prize']}**."
    )


# ============================================================
# TICKETS COMMANDS
# ============================================================

@bot.command()
@commands.has_permissions(manage_channels=True)
async def close(
    ctx: commands.Context
):
    if not ctx.channel.name.startswith(
        "ticket-"
    ):
        await ctx.send(
            "Cette commande doit être utilisée "
            "dans un salon de ticket."
        )
        return

    embed = discord.Embed(
        title="Confirmer la fermeture",
        description=(
            "Ce ticket va être fermé dans 5 secondes."
        ),
        color=discord.Color.red()
    )

    await ctx.send(
        embed=embed
    )

    await asyncio.sleep(5)

    try:
        await ctx.channel.delete(
            reason=f"Fermé par {ctx.author}"
        )
    except discord.HTTPException:
        pass


@bot.command()
@commands.has_permissions(administrator=True)
async def panel(
    ctx: commands.Context
):
    embed = discord.Embed(
        title="Panneau de configuration - Tickets",
        description=(
            "Configure le système de tickets "
            "avec les menus ci-dessous."
        ),
        color=COLORS["ticket"]
    )

    embed.add_field(
        name="Catégorie",
        value=(
            "Choisis la catégorie où les tickets seront créés."
        ),
        inline=False
    )

    embed.add_field(
        name="Rôles staff",
        value=(
            "Ajoute ou retire les rôles qui gèrent les tickets."
        ),
        inline=False
    )

    embed.add_field(
        name="Message",
        value=(
            "Personnalise le message de bienvenue."
        ),
        inline=False
    )

    embed.set_footer(
        text="Panneau de configuration"
    )

    await ctx.send(
        embed=embed,
        view=ConfigPanelView(ctx.guild)
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def sendticket(
    ctx: commands.Context,
    channel: discord.TextChannel = None
):
    channel = channel or ctx.channel

    cfg = TicketConfigManager.get(
        ctx.guild.id
    )

    if not cfg.get("category_id"):
        await ctx.send(
            "Configure d'abord les tickets avec +panel."
        )
        return

    await channel.send(
        embed=build_ticket_intro_embed(),
        view=TicketView()
    )

    if channel != ctx.channel:
        await ctx.send(
            f"Panneau envoyé dans {channel.mention}."
        )


# ============================================================
# ERREURS
# ============================================================

@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError
):
    # Une commande inconnue ne fait absolument rien
    if isinstance(
        error,
        commands.CommandNotFound
    ):
        return

    if isinstance(
        error,
        commands.MissingPermissions
    ):
        await ctx.send(
            "Tu n'as pas les permissions nécessaires."
        )
        return

    if isinstance(
        error,
        commands.MemberNotFound
    ):
        await ctx.send(
            "Membre introuvable."
        )
        return

    if isinstance(
        error,
        commands.BadArgument
    ):
        await ctx.send(
            "Argument invalide."
        )
        return

    if isinstance(
        error,
        commands.MissingRequiredArgument
    ):
        await ctx.send(
            "Argument manquant."
        )
        return

    if isinstance(
        error,
        commands.CommandOnCooldown
    ):
        await ctx.send(
            "Cette commande est temporairement indisponible."
        )
        return

    print(
        f"[ERREUR COMMANDE] "
        f"{type(error).__name__}: {error}"
    )


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():
    print(
        f"Bot connecté : "
        f"{bot.user} ({bot.user.id})"
    )

    print(
        f"Serveurs : {len(bot.guilds)}"
    )

    print(
        f"Préfixe : {PREFIX}"
    )

    # Les seules vues ajoutées ici sont persistantes.
    # TicketSubjectSelect et les vues de configuration
    # sont temporaires et ne doivent pas être ajoutées ici.

    if not getattr(
        bot,
        "_persistent_views_added",
        False
    ):
        bot.add_view(
            TicketView()
        )

        bot.add_view(
            CloseTicketView()
        )

        bot._persistent_views_added = True

    if not getattr(
        bot,
        "_giveaway_loop_started",
        False
    ):
        bot._giveaway_loop_started = True

        bot.loop.create_task(
            GiveawayManager.loop()
        )


# ============================================================
# LANCEMENT
# ============================================================

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Aucun token trouvé. "
            "Définis la variable d'environnement "
            "DISCORD_TOKEN."
        )

    bot.run(TOKEN)
