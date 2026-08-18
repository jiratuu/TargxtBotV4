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

def run_server():
    server = HTTPServer(("0.0.0.0", 10000), BaseHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_server).start()

# ============================================================
# CONFIG
# ============================================================
PREFIX = "+"
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Couleurs
COLORS = {
    "warn": discord.Color.orange(),
    "tempmute": discord.Color.dark_grey(),
    "unmute": discord.Color.green(),
    "ban": discord.Color.red(),
    "help": discord.Color.blue(),
    "sanctions": discord.Color.purple(),
    "lock": discord.Color.dark_red(),
    "unlock": discord.Color.green(),
    "disconnect": discord.Color.gold(),
    "ticket": discord.Color.teal(),
    "giveaway": discord.Color.from_str("#ff6b6b"),
    "giveaway_end": discord.Color.from_str("#2ecc71"),
}

TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str):
    """Convertit '10m', '2h', etc. en secondes. Renvoie None si invalide."""
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


# ============================================================
# GESTIONNAIRE DE STOCKAGE (classe unique pour tous les JSON)
# ============================================================
class Store:
    """Petit wrapper autour de fichiers JSON pour centraliser la persistance."""

    def __init__(self, path: str):
        self.path = path

    def read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def update(self, mutator):
        """Applique une fonction de mutation sur les données puis sauvegarde."""
        data = self.read()
        result = mutator(data)
        self.write(data)
        return result


sanctions_store = Store("sanctions.json")
ticket_store = Store("ticket_config.json")
giveaway_store = Store("giveaways.json")


# ============================================================
# SANCTIONS — logique métier
# ============================================================
class SanctionManager:
    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}_{user_id}"

    @classmethod
    def add(cls, guild_id: int, user_id: int, action: str, reason: str, moderator: str):
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
        return sanctions_store.read().get(cls._key(guild_id, user_id), [])

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
    def remove_one(cls, guild_id: int, user_id: int, index: int):
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
# TICKETS — configuration par serveur
# ============================================================
class TicketConfigManager:
    @staticmethod
    def get(guild_id: int) -> dict:
        return ticket_store.read().get(str(guild_id), {})

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
            roles = data[gid].get("staff_role_ids", [])
            if role_id not in roles:
                roles.append(role_id)
            data[gid]["staff_role_ids"] = roles

        ticket_store.update(mutate)

    @staticmethod
    def remove_staff_role(guild_id: int, role_id: int):
        gid = str(guild_id)

        def mutate(data):
            data.setdefault(gid, {})
            roles = data[gid].get("staff_role_ids", [])
            if role_id in roles:
                roles.remove(role_id)
            data[gid]["staff_role_ids"] = roles

        ticket_store.update(mutate)

    @staticmethod
    def get_staff_roles(guild_id: int) -> list:
        return TicketConfigManager.get(guild_id).get("staff_role_ids", [])


async def send_dm(user: discord.abc.User, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


# ============================================================
# GIVEAWAYS — logique métier + boucle de fond
# ============================================================
class GiveawayManager:
    @staticmethod
    def all_running():
        return {
            gid: g for gid, g in giveaway_store.read().items()
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
        return giveaway_store.read().get(str(message_id))

    @classmethod
    async def loop(cls):
        await bot.wait_until_ready()
        while not bot.is_closed():
            now = datetime.now().timestamp()
            for gid, g in cls.all_running().items():
                if now >= g["end_timestamp"]:
                    await cls.finish(int(gid), g)
            await asyncio.sleep(5)

    @classmethod
    async def draw_winners(cls, message: discord.Message, count: int):
        reaction = discord.utils.get(message.reactions, emoji="🎉")
        if not reaction:
            return []
        participants = [u async for u in reaction.users() if u != bot.user]
        if not participants:
            return []
        return participants if len(participants) <= count else random.sample(participants, count)

    @classmethod
    async def finish(cls, message_id: int, g: dict):
        guild = bot.get_guild(g["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(g["channel_id"])
        if not channel:
            return

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            cls.delete(message_id)
            return

        winners = await cls.draw_winners(message, g["winners"])

        embed = discord.Embed(
            title="🎉 **GIVEAWAY TERMINÉ** 🎉",
            description=f"**{g['prize']}**\n\nHébergé par : {g['host']}",
            color=COLORS["giveaway_end"],
        )
        if winners:
            mentions = ", ".join(w.mention for w in winners)
            embed.add_field(name="🏆 Gagnant(s)", value=mentions, inline=False)
            embed.set_footer(text="Félicitations aux gagnants !")
        else:
            embed.add_field(name="🏆 Gagnant(s)", value="Aucun participant 😢", inline=False)
        embed.timestamp = datetime.fromtimestamp(g["end_timestamp"])

        await message.edit(embed=embed, view=None)
        if winners:
            mentions = ", ".join(w.mention for w in winners)
            await channel.send(f"🎉 **Félicitations {mentions} !** Vous avez gagné **{g['prize']}** !")

        cls.mark_ended(message_id)


# ============================================================
# UI — PANNEAU DE CRÉATION DE GIVEAWAY
# ============================================================
class GiveawayDureeModal(discord.ui.Modal, title="⏳ Durée du giveaway"):
    duree = TextInput(label="Durée (ex: 30s, 5m, 2h, 1d)", placeholder="30s, 5m, 2h, 1d...", max_length=10)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duree.value)
        if seconds is None:
            await interaction.response.send_message("❌ Durée invalide. Utilise s, m, h, d.", ephemeral=True)
            return
        self.parent.duree = self.duree.value.strip().lower()
        self.parent.duree_seconds = seconds
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawayGagnantsModal(discord.ui.Modal, title="👥 Nombre de gagnants"):
    gagnants = TextInput(label="Nombre de gagnants", placeholder="1, 2, 3, 5...", max_length=3)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        try:
            nb = int(self.gagnants.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Nombre invalide.", ephemeral=True)
            return
        if not (1 <= nb <= 100):
            await interaction.response.send_message("❌ Entre 1 et 100 gagnants.", ephemeral=True)
            return
        self.parent.gagnants = nb
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawayPrixModal(discord.ui.Modal, title="🎁 Prix du giveaway"):
    prix = TextInput(label="Prix à gagner", placeholder="Nitro Classic, 50€, Role exclusif...", max_length=100)

    def __init__(self, parent: "GiveawaySetupView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        self.parent.prix = self.prix.value.strip()
        await interaction.response.edit_message(embed=self.parent.build_embed(), view=self.parent)


class GiveawaySetupView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.salon: discord.TextChannel | None = None
        self.duree: str | None = None
        self.duree_seconds = 0
        self.gagnants = 1
        self.prix = "Non défini"

    def _guard(self, interaction: discord.Interaction) -> bool:
        return interaction.user == self.ctx.author

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎉 Configuration du Giveaway",
            description="Configure ton giveaway avec les boutons ci-dessous.",
            color=COLORS["giveaway"],
        )
        embed.add_field(name="📢 Salon", value=self.salon.mention if self.salon else "❌ Non défini", inline=False)
        embed.add_field(name="⏳ Durée", value=self.duree or "❌ Non définie", inline=False)
        embed.add_field(name="👥 Gagnant(s)", value=str(self.gagnants), inline=False)
        embed.add_field(name="🎁 Prix", value=self.prix, inline=False)
        embed.set_footer(text="Configure tous les champs puis clique sur Lancer")
        return embed

    @discord.ui.button(label="📢 Salon", style=discord.ButtonStyle.secondary, row=0)
    async def set_salon(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)

        await interaction.response.send_message("📢 **Mentionne le salon** où envoyer le giveaway :", ephemeral=True)

        def check(m):
            return m.author == self.ctx.author and m.channel == self.ctx.channel

        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30, check=check)
        except asyncio.TimeoutError:
            await interaction.edit_original_response(content="⏰ Temps écoulé.", embed=self.build_embed(), view=self)
            return

        if msg.channel_mentions:
            self.salon = msg.channel_mentions[0]
            await msg.delete()
            await interaction.edit_original_response(content="✅ Salon défini !", embed=self.build_embed(), view=self)
        else:
            await interaction.edit_original_response(content="❌ Salon invalide.", embed=self.build_embed(), view=self)

    @discord.ui.button(label="⏳ Durée", style=discord.ButtonStyle.secondary, row=0)
    async def set_duree(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayDureeModal(self))

    @discord.ui.button(label="👥 Gagnants", style=discord.ButtonStyle.secondary, row=1)
    async def set_gagnants(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayGagnantsModal(self))

    @discord.ui.button(label="🎁 Prix", style=discord.ButtonStyle.secondary, row=1)
    async def set_prix(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_modal(GiveawayPrixModal(self))

    @discord.ui.button(label="🚀 Lancer le giveaway", style=discord.ButtonStyle.success, row=2)
    async def lancer(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        if not self.salon:
            return await interaction.response.send_message("❌ Configure d'abord le salon.", ephemeral=True)
        if not self.duree:
            return await interaction.response.send_message("❌ Configure d'abord la durée.", ephemeral=True)
        if self.prix == "Non défini":
            return await interaction.response.send_message("❌ Configure d'abord le prix.", ephemeral=True)

        await interaction.response.defer()

        end_timestamp = (datetime.now() + timedelta(seconds=self.duree_seconds)).timestamp()
        embed = discord.Embed(
            title="🎉 **GIVEAWAY** 🎉",
            description=(
                f"**{self.prix}**\n\n"
                f"🎁 **Gagnant(s) :** {self.gagnants}\n"
                f"⏳ **Se termine :** <t:{int(end_timestamp)}:R>\n"
                f"🛡️ **Hébergé par :** {self.ctx.author.mention}"
            ),
            color=COLORS["giveaway"],
        )
        embed.set_footer(text="Clique sur 🎉 pour participer !")
        embed.timestamp = datetime.fromtimestamp(end_timestamp)

        msg = await self.salon.send(embed=embed)
        await msg.add_reaction("🎉")

        GiveawayManager.save(msg.id, {
            "guild_id": self.ctx.guild.id,
            "channel_id": self.salon.id,
            "host": self.ctx.author.mention,
            "prize": self.prix,
            "winners": self.gagnants,
            "end_timestamp": end_timestamp,
            "status": "running",
        })

        confirm = discord.Embed(
            title="✅ Giveaway lancé !",
            description=f"Giveaway envoyé dans {self.salon.mention}",
            color=discord.Color.green(),
        )
        confirm.add_field(name="🎁 Prix", value=self.prix, inline=True)
        confirm.add_field(name="⏳ Durée", value=self.duree, inline=True)
        confirm.add_field(name="👥 Gagnants", value=str(self.gagnants), inline=True)
        await self.ctx.send(embed=confirm)
        await interaction.delete_original_response()

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.danger, row=2)
    async def annuler(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not self._guard(interaction):
            return await interaction.response.send_message("❌ Ce n'est pas ton panneau.", ephemeral=True)
        await interaction.response.send_message("❌ Giveaway annulé.", ephemeral=True)
        await interaction.delete_original_response()


# ============================================================
# UI — TICKETS (style DraftBot)
# ============================================================
TICKET_TOPICS = [
    discord.SelectOption(label="Contactez le staff", description="Poser une question au staff ou autre...", emoji="🔨"),
    discord.SelectOption(label="Partenariat", description="Demande un partenariat...", emoji="🤝"),
    discord.SelectOption(label="Autre...", description="Autre demande non inclus...", emoji="📩"),
]


class TicketDescriptionModal(discord.ui.Modal, title="Ouvrir un ticket"):
    description = TextInput(
        label="Description", placeholder="Explique brièvement ta demande...",
        style=discord.TextStyle.long, max_length=2000,
    )

    def __init__(self, sujet: str):
        super().__init__()
        self.sujet = sujet

    async def on_submit(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        cfg = TicketConfigManager.get(guild.id)

        category = discord.utils.get(guild.categories, id=cfg.get("category_id"))
        if not category:
            await interaction.response.send_message("Tickets non configurés. Demande à un admin.", ephemeral=True)
            return

        chan_name = f"ticket-{user.name.lower().replace(' ', '-')}-{user.discriminator}"
        if discord.utils.get(guild.text_channels, name=chan_name):
            await interaction.response.send_message("Tu as déjà un ticket ouvert.", ephemeral=True)
            return

        staff_role_ids = TicketConfigManager.get_staff_roles(guild.id)
        
        # Répondre immédiatement pour éviter le timeout
        await interaction.response.send_message("Création de ton ticket en cours... ⏳", ephemeral=True)

        # Préparer les overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        
        # Ajouter tous les rôles staff
        for role_id in staff_role_ids:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        # Création du salon (peut prendre du temps)
        try:
            chan = await guild.create_text_channel(
                name=chan_name, category=category, overwrites=overwrites, reason=f"Ticket de {user}",
            )
        except discord.Forbidden:
            await interaction.edit_original_response(content="❌ Je n'ai pas la permission de créer des salons.")
            return
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Erreur : {e}")
            return

        ticket_msg = cfg.get("ticket_message", "🎫 **Bienvenue !** Un membre du staff va vous répondre.")
        
        # Construire le message du salon
        staff_mentions = []
        for role_id in staff_role_ids:
            role = guild.get_role(role_id)
            if role:
                staff_mentions.append(role.mention)
        
        mention_staff = " ".join(staff_mentions) if staff_mentions else "@staff"

        embed = discord.Embed(title="Nouveau ticket 🎫", description=ticket_msg, color=COLORS["ticket"])
        embed.add_field(name="Utilisateur", value=user.mention, inline=True)
        embed.add_field(name="Sujet", value=self.sujet, inline=True)
        if staff_mentions:
            embed.add_field(name="👥 Staff", value=", ".join(staff_mentions), inline=True)
        embed.set_footer(text="Utilise le bouton ci-dessous pour fermer le ticket")

        embed2 = discord.Embed(
            title="📝 Description du ticket",
            description=self.description.value if self.description.value else "Aucune description fournie",
            color=discord.Color.light_grey()
        )

        # Envoyer les messages dans le nouveau salon
        await chan.send(content=f"{user.mention} — {mention_staff}", embed=embed, view=CloseTicketView())
        await chan.send(embed=embed2)

        # Modifier la réponse éphémère
        await interaction.edit_original_response(content=f"Ton ticket a été créé : {chan.mention}")


class TicketSubjectSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Choisis le sujet de ton ticket...",
        options=TICKET_TOPICS,
        custom_id="ticket_subject_select"
    )
    async def select_subject(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(TicketDescriptionModal(select.values[0]))


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Ouvrir un ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, _button: discord.ui.Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        if not cfg.get("category_id"):
            await interaction.response.send_message("❌ Tickets non configurés. Demande à un admin.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🎯 Choisis un sujet",
            description="Sélectionne le sujet de ton ticket dans le menu ci-dessous.",
            color=COLORS["ticket"],
        )
        await interaction.response.send_message(embed=embed, view=TicketSubjectSelect(), ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Fermer le ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Demander confirmation avec un bouton
        await interaction.response.send_message(
            "⚠️ **Confirmer la fermeture ?**",
            view=ConfirmCloseView(interaction.channel),
            ephemeral=True
        )


class ConfirmCloseView(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=30)
        self.channel = channel

    @discord.ui.button(label="Oui, fermer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_message("Fermeture du ticket dans 5 secondes...")
        await asyncio.sleep(5)
        await self.channel.delete(reason=f"Fermé par {interaction.user}")

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content="Fermeture annulée.", view=None)


class TicketMessageModal(discord.ui.Modal, title="Modifier le message du ticket"):
    message = TextInput(
        label="Message de bienvenue", style=discord.TextStyle.long,
        placeholder="🎫 Bienvenue dans votre ticket ! Un membre du staff va vous répondre.", max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        TicketConfigManager.set(interaction.guild_id, "ticket_message", self.message.value)
        embed = discord.Embed(
            title="Message mis à jour",
            description=f"Nouveau message :\n{self.message.value}",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddStaffRoleSelect(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        self.guild = guild

    @discord.ui.select(cls=RoleSelect, placeholder="Ajouter un rôle staff...", custom_id="add_staff_role_select")
    async def select_role(self, interaction: discord.Interaction, select: RoleSelect):
        role = select.values[0]
        TicketConfigManager.add_staff_role(interaction.guild_id, role.id)
        await interaction.response.send_message(f"Rôle {role.mention} ajouté aux rôles staff !", ephemeral=True)


class RemoveStaffRoleSelect(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=120)
        self.guild = guild
        self.options = []
        staff_role_ids = TicketConfigManager.get_staff_roles(guild.id)
        for rid in staff_role_ids:
            role = guild.get_role(rid)
            if role:
                self.options.append(discord.SelectOption(label=role.name, value=str(role.id)))
        
        if self.options:
            self.remove_role_select.options = self.options
        else:
            self.remove_role_select.options = [discord.SelectOption(label="Aucun rôle staff", value="none")]

    @discord.ui.select(placeholder="Retirer un rôle staff...", custom_id="remove_staff_role_select")
    async def remove_role_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        role_id = int(select.values[0])
        TicketConfigManager.remove_staff_role(interaction.guild_id, role_id)
        role = self.guild.get_role(role_id)
        await interaction.response.send_message(f"Rôle {role.mention if role else role_id} retiré des rôles staff !", ephemeral=True)


class ConfigPanelView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild

    @discord.ui.select(cls=ChannelSelect, channel_types=[discord.ChannelType.category],
                        placeholder="Choisir la catégorie des tickets...")
    async def select_category(self, interaction: discord.Interaction, select: ChannelSelect):
        category = select.values[0]
        TicketConfigManager.set(interaction.guild_id, "category_id", category.id)
        embed = discord.Embed(title="Catégorie définie", description=f"Catégorie : {category.mention}", color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("Catégorie sauvegardée !", ephemeral=True)

    @discord.ui.button(label="Ajouter un rôle staff", style=discord.ButtonStyle.success, row=1)
    async def add_staff_role(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_message(
            "Sélectionne un rôle à ajouter comme staff :",
            view=AddStaffRoleSelect(self.guild),
            ephemeral=True
        )

    @discord.ui.button(label="➖ Retirer un rôle staff", style=discord.ButtonStyle.danger, row=1)
    async def remove_staff_role(self, interaction: discord.Interaction, _button: discord.ui.Button):
        staff_role_ids = TicketConfigManager.get_staff_roles(interaction.guild_id)
        if not staff_role_ids:
            await interaction.response.send_message("❌ Aucun rôle staff configuré.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Sélectionne un rôle à retirer :",
            view=RemoveStaffRoleSelect(self.guild),
            ephemeral=True
        )

    @discord.ui.button(label="Modifier le message du ticket", style=discord.ButtonStyle.secondary, row=1)
    async def edit_message(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(TicketMessageModal())

    @discord.ui.button(label="Envoyer le bouton ticket", style=discord.ButtonStyle.success, row=2)
    async def send_ticket_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        if not cfg.get("category_id"):
            await interaction.response.send_message("❌ Configure d'abord la catégorie !", ephemeral=True)
            return
        await interaction.channel.send(embed=build_ticket_intro_embed(), view=TicketView())
        await interaction.response.send_message("Bouton ticket envoyé !", ephemeral=True)

    @discord.ui.button(label="Voir la config actuelle", style=discord.ButtonStyle.secondary, row=2)
    async def show_config(self, interaction: discord.Interaction, _button: discord.ui.Button):
        cfg = TicketConfigManager.get(interaction.guild_id)
        category = discord.utils.get(self.guild.categories, id=cfg.get("category_id"))
        staff_role_ids = TicketConfigManager.get_staff_roles(interaction.guild_id)
        staff_roles = [self.guild.get_role(rid) for rid in staff_role_ids if self.guild.get_role(rid)]
        msg = cfg.get("ticket_message", "Message par défaut")

        embed = discord.Embed(title="Configuration actuelle", color=COLORS["ticket"])
        embed.add_field(name="Catégorie", value=category.mention if category else "❌ Non définie", inline=False)
        
        roles_str = "\n".join([r.mention for r in staff_roles]) if staff_roles else "❌ Aucun rôle staff"
        embed.add_field(name="👥 Rôles staff", value=roles_str, inline=False)
        embed.add_field(name="✏️ Message", value=(msg[:100] + "...") if len(msg) > 100 else msg, inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer le panneau", style=discord.ButtonStyle.danger, row=2)
    async def close_panel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.message.delete()
        await interaction.response.send_message("🔧 Panneau fermé.", ephemeral=True)


def build_ticket_intro_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫 TARGXT • Support",
        description=(
            "Clique sur le bouton juste en dessous si tu veux contacter un membre du staff\n\n"
            "> Ne pas abuser du système de tickets. :warning:\n-# Besoin d'aide ?"
        ),
        color=COLORS["ticket"],
    )
    embed.set_footer(text="TARGXT • Support")
    return embed


# ============================================================
# HELP
# ============================================================
@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="🛡️ Bot Modération — Aide",
        description=f"Préfixe : {PREFIX}\nToutes les commandes utilisent le préfixe +.",
        color=COLORS["help"],
    )
    embed.set_thumbnail(url=ctx.bot.user.display_avatar.url)

    sections = {
        "⬇️ Sanctions": (
            f"{PREFIX}warn <membre> [raison] — Avertir un membre\n"
            f"{PREFIX}tempmute <membre> <durée> [raison] — Mute temporaire\n"
            f"{PREFIX}unmute <membre> — Enlever le mute\n"
            f"{PREFIX}ban <membre> [raison] — Bannir un membre\n"
            f"{PREFIX}unban <utilisateur#1234> — Débannir\n"
            f"{PREFIX}unbanall — Débannir tous les bannis"
        ),
        "📋 Gestion des sanctions": (
            f"{PREFIX}sanctions <membre> — Voir les sanctions d'un membre\n"
            f"{PREFIX}resetsanctions <membre> — Supprimer TOUTES les sanctions\n"
            f"{PREFIX}removesanction <membre> <numéro> — Retirer une sanction spécifique"
        ),
        "🎉 Giveaways": (
            f"{PREFIX}giveaway — Ouvrir le panneau interactif de giveaway\n"
            f"{PREFIX}gend <id_message> — Terminer un giveaway prématurément\n"
            f"{PREFIX}reroll <id_message> — Re-tirer un giveaway terminé"
        ),
        "🔒 Salon": f"{PREFIX}lock — Verrouiller le salon\n{PREFIX}unlock — Déverrouiller le salon",
        "🔊 Vocal": f"{PREFIX}disconnectall — Déconnecter tout le monde du vocal",
        "🎫 Tickets": (
            f"{PREFIX}sendticket [#salon] — Envoyer le bouton de ticket\n"
            f"{PREFIX}close — Fermer le ticket (dans le salon ticket)\n"
            f"{PREFIX}panel — Ouvrir le panneau de configuration interactif"
        ),
        "📋 Divers": f"{PREFIX}clear <nb> — Supprimer des messages",
    }
    for name, value in sections.items():
        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text="Bot Modération • Taper +help pour ce message")
    await ctx.send(embed=embed)


# ============================================================
# SANCTIONS — commandes
# ============================================================
@bot.command()
@commands.has_permissions(kick_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason="Aucune raison fournie"):
    SanctionManager.add(ctx.guild.id, member.id, "⚠️ Warn", reason, str(ctx.author))

    embed = discord.Embed(title="⚠️ Avertissement", description=f"{member.mention} a été averti.", color=COLORS["warn"])
    embed.add_field(name="Raison", value=reason, inline=False)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    embed.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"⚠️ Avertissement — {ctx.guild.name}", description="Tu as reçu un avertissement.", color=COLORS["warn"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=str(ctx.author), inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(member, dm)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def tempmute(ctx: commands.Context, member: discord.Member, duration: str, *, reason="Aucune raison fournie"):
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("❌ Durée invalide. Utilise s, m, h, d. Ex: +tempmute @user 10m")
        return

    until = discord.utils.utcnow() + timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    SanctionManager.add(ctx.guild.id, member.id, "🔇 Tempmute", f"{duration} — {reason}", str(ctx.author))

    embed = discord.Embed(title="🔇 Mute temporaire", description=f"{member.mention} est réduit au silence.", color=COLORS["tempmute"])
    embed.add_field(name="Durée", value=duration, inline=True)
    embed.add_field(name="Raison", value=reason, inline=True)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"🔇 Mute — {ctx.guild.name}", description="Tu es réduit au silence.", color=COLORS["tempmute"])
    dm.add_field(name="Durée", value=duration, inline=True)
    dm.add_field(name="Raison", value=reason, inline=True)
    dm.add_field(name="Expire", value=f"<t:{int((datetime.now() + timedelta(seconds=seconds)).timestamp())}>", inline=False)
    await send_dm(member, dm)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    if not member.is_timed_out():
        await ctx.send(f"ℹ️ {member.mention} n'est pas muet.")
        return

    await member.timeout(None)
    embed = discord.Embed(title="✅ Unmute", description=f"{member.mention} n'est plus muet.", color=COLORS["unmute"])
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    await ctx.send(embed=embed)
    await send_dm(member, discord.Embed(
        title=f"✅ Unmute — {ctx.guild.name}", description="Tu n'es plus réduit au silence.", color=COLORS["unmute"],
    ))


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason="Aucune raison fournie"):
    await member.ban(reason=reason)
    SanctionManager.add(ctx.guild.id, member.id, "🔨 Ban", reason, str(ctx.author))

    embed = discord.Embed(title="🔨 Bannissement", description=f"{member.mention} a été banni.", color=COLORS["ban"])
    embed.add_field(name="Raison", value=reason, inline=False)
    embed.add_field(name="Modérateur", value=ctx.author.mention, inline=False)
    embed.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await ctx.send(embed=embed)

    dm = discord.Embed(title=f"🔨 Bannissement — {ctx.guild.name}", description="Tu as été banni de [TARGXT] #LEAK. Pour contester rejoins ce serveur : https://discord.gg/7YnttqCX", color=COLORS["ban"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=str(ctx.author), inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(member, dm)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.abc.User):
    reason, moderator = "Aucune raison fournie", "Inconnu"
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=5):
            if entry.target.id == user.id:
                reason = entry.reason or reason
                moderator = str(entry.user)
                break
    except discord.Forbidden:
        pass

    SanctionManager.add(guild.id, user.id, "🔨 Ban (externe)", reason, moderator)

    dm = discord.Embed(title=f"🔨 Bannissement — {guild.name}", description="Tu as été banni de [TARGXT] #LEAK. Pour contester rejoins ce serveur : https://discord.gg/7YnttqCX", color=COLORS["ban"])
    dm.add_field(name="Raison", value=reason, inline=False)
    dm.add_field(name="Modérateur", value=moderator, inline=False)
    dm.set_footer(text=datetime.now().strftime("%d/%m/%Y %H:%M"))
    await send_dm(user, dm)


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, *, user_input: str):
    async for entry in ctx.guild.bans():
        u = entry.user
        if user_input in (str(u), u.name, str(u.id)):
            await ctx.guild.unban(u)
            await ctx.send(f"✅ {u} débanni.")
            return
    await ctx.send("❌ Utilisateur non trouvé.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unbanall(ctx: commands.Context):
    banned = [entry async for entry in ctx.guild.bans()]
    if not banned:
        await ctx.send("ℹ️ Aucun banni.")
        return

    count = 0
    for entry in banned:
        try:
            await ctx.guild.unban(entry.user)
            count += 1
        except discord.HTTPException:
            pass
    await ctx.send(f"✅ {count} débanni(s).")


@bot.command()
@commands.has_permissions(move_members=True)
async def disconnectall(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Tu dois être dans un salon vocal.")
        return

    channel = ctx.author.voice.channel
    count = 0
    for member in channel.members:
        if member != ctx.bot.user:
            try:
                await member.move_to(None)
                count += 1
            except discord.HTTPException:
                pass

    await ctx.send(embed=discord.Embed(
        title="🔊 Déconnexion", description=f"{count} membre(s) déconnecté(s).", color=COLORS["disconnect"],
    ))


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=discord.Embed(
        title="🔒 Verrouillé", description=f"{ctx.channel.mention} est verrouillé.", color=COLORS["lock"],
    ))


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=discord.Embed(
        title="🔓 Déverrouillé", description=f"{ctx.channel.mention} est déverrouillé.", color=COLORS["unlock"],
    ))


@bot.command()
async def sanctions(ctx: commands.Context, member: discord.Member):
    entries = SanctionManager.get_all(ctx.guild.id, member.id)
    if not entries:
        await ctx.send(f"✅ {member.mention} : aucune sanction.")
        return

    embed = discord.Embed(title=f"📋 Sanctions de {member.display_name}", description=f"Total : {len(entries)}", color=COLORS["sanctions"])
    embed.set_thumbnail(url=member.display_avatar.url)
    for i, s in enumerate(entries, 1):
        embed.add_field(
            name=f"{i}. {s['action']} — {s['date']}",
            value=f"Raison : {s['reason']}\nModérateur : {s['moderator']}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def resetsanctions(ctx: commands.Context, member: discord.Member):
    if SanctionManager.reset(ctx.guild.id, member.id):
        await ctx.send(embed=discord.Embed(
            title="🗑️ Sanctions réinitialisées",
            description=f"Toutes les sanctions de {member.mention} ont été supprimées.",
            color=COLORS["sanctions"],
        ))
    else:
        await ctx.send(f"ℹ️ {member.mention} n'a aucune sanction.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removesanction(ctx: commands.Context, member: discord.Member, numero: int):
    removed = SanctionManager.remove_one(ctx.guild.id, member.id, numero)
    if not removed:
        await ctx.send(f"❌ Sanction #{numero} introuvable pour {member.mention}. Utilise `{PREFIX}sanctions {member.display_name}` pour voir les numéros.")
        return

    embed = discord.Embed(title="✅ Sanction retirée", description=f"Sanction #{numero} retirée pour {member.mention}", color=COLORS["sanctions"])
    embed.add_field(name="Action", value=removed["action"], inline=True)
    embed.add_field(name="Raison", value=removed["reason"], inline=True)
    embed.add_field(name="Date", value=removed["date"], inline=True)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int):
    if not (1 <= amount <= 100):
        await ctx.send("❌ Entre 1 et 100.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ {len(deleted) - 1} supprimé(s).", delete_after=3)


# ============================================================
# GIVEAWAYS — commandes
# ============================================================
@bot.command()
@commands.has_permissions(administrator=True)
async def giveaway(ctx: commands.Context):
    view = GiveawaySetupView(ctx)
    await ctx.send(embed=view.build_embed(), view=view)


@bot.command()
@commands.has_permissions(administrator=True)
async def gend(ctx: commands.Context, message_id: int = None):
    if message_id is None:
        await ctx.send(f"❌ Utilisation : `{PREFIX}gend <id_du_message_giveaway>`")
        return

    g = GiveawayManager.get(message_id)
    if not g:
        await ctx.send("❌ Giveaway introuvable ou déjà terminé.")
        return
    if g["status"] != "running":
        await ctx.send("❌ Ce giveaway est déjà terminé.")
        return

    await GiveawayManager.finish(message_id, g)
    await ctx.send(f"✅ Giveaway terminé ! Résultats dans <#{g['channel_id']}>.")


@bot.command()
@commands.has_permissions(administrator=True)
async def reroll(ctx: commands.Context, message_id: int = None):
    if message_id is None:
        await ctx.send(f"❌ Utilisation : `{PREFIX}reroll <id_du_message_giveaway>`")
        return

    g = GiveawayManager.get(message_id)
    if not g:
        await ctx.send("❌ Giveaway introuvable.")
        return

    channel = ctx.guild.get_channel(g["channel_id"])
    if not channel:
        await ctx.send("❌ Salon du giveaway introuvable.")
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await ctx.send("❌ Message introuvable.")
        return

    winners = await GiveawayManager.draw_winners(message, g["winners"])
    if not winners:
        await ctx.send("❌ Aucun participant à ce giveaway.")
        return

    mentions = ", ".join(w.mention for w in winners)
    await ctx.send(f"🎉 **Nouveau tirage !** Félicitations {mentions} ! Vous avez gagné **{g['prize']}** !")


# ============================================================
# TICKETS — commandes
# ============================================================
@bot.command()
@commands.has_permissions(manage_channels=True)
async def close(ctx: commands.Context):
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.send("❌ Cette commande doit être utilisée dans un salon de ticket.")
        return
    
    # Demander confirmation
    embed = discord.Embed(
        title="🔒 Confirmer la fermeture ?",
        description="⚠️ Ce ticket va être fermé dans 5 secondes.",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)
    await asyncio.sleep(5)
    await ctx.channel.delete(reason=f"Fermé par {ctx.author}")


@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="🛠️ Panneau de configuration — Tickets",
        description="Configure le système de tickets en utilisant les menus ci-dessous.",
        color=COLORS["ticket"],
    )
    embed.add_field(name="📂 Catégorie", value="Choisis la catégorie où les tickets seront créés", inline=False)
    embed.add_field(name="👥 Rôles staff", value="Ajoute ou retire les rôles qui gérent les tickets (jusqu'à 5 rôles)", inline=False)
    embed.add_field(name="✏️ Message", value="Personnalise le message de bienvenue dans les tickets", inline=False)
    embed.set_footer(text="Panneau de configuration • +help pour l'aide")
    await ctx.send(embed=embed, view=ConfigPanelView(ctx.guild))


@bot.command()
@commands.has_permissions(administrator=True)
async def sendticket(ctx: commands.Context, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    cfg = TicketConfigManager.get(ctx.guild.id)
    if not cfg.get("category_id"):
        await ctx.send("❌ Configure d'abord avec +panel.")
        return

    await channel.send(embed=build_ticket_intro_embed(), view=TicketView())
    if channel != ctx.channel:
        await ctx.send(f"✅ Envoyé dans {channel.mention}.")


# ============================================================
# ERREURS
# ============================================================
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Tu n'as pas les permissions nécessaires.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Membre introuvable.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argument invalide.")
    else:
        await ctx.send(f"❌ Erreur : {error}")


# ============================================================
# READY / LANCEMENT
# ============================================================
@bot.event
async def on_ready():
    print(f"✅ Bot connecté : {bot.user} ({bot.user.id})")
    print(f"📡 Serveurs : {len(bot.guilds)}")
    print(f"⚡ Préfixe : {PREFIX}")

    bot.add_view(TicketView())
    bot.add_view(CloseTicketView())
    bot.add_view(TicketSubjectSelect())
    bot.add_view(AddStaffRoleSelect(None))
    bot.add_view(RemoveStaffRoleSelect(None))
    bot.loop.create_task(GiveawayManager.loop())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "❌ Aucun token trouvé. Définis la variable d'environnement DISCORD_TOKEN "
            "avant de lancer le bot (et régénère ton token s'il a déjà été exposé)."
        )
    bot.run(TOKEN)