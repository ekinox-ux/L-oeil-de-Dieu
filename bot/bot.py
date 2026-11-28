"""
L'Oeil de Dieu — Bot Discord
Commandes slash: /search, /stats
Acces restreint par role.
"""

import asyncio
import logging
import os
import re
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp

from pathlib import Path

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

load_dotenv(Path(__file__).resolve().parent / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8500")
ROLE_NAME = os.getenv("ROLE_NAME", "OSINT")
API_KEY = os.getenv("API_KEY", "")

# ---- Logging ----
logger = logging.getLogger("oeil-bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Bot setup — intents par defaut, sans intent privilegie: les slash commands
# n'ont besoin ni de message_content ni de members (le Member est fourni par
# l'interaction).
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---- Verification du role ----

def has_required_role():
    """Decorator: verifie que l'utilisateur a le role requis."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Cette commande ne fonctionne que sur un serveur.", ephemeral=True)
            return False
        role_name_lower = ROLE_NAME.lower()
        user = interaction.user

        # Dans un serveur, Discord fournit deja un Member avec ses roles: aucun
        # intent privilegie n'est necessaire. fetch_member reste un repli rare.
        member = user if isinstance(user, discord.Member) else None
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user.id)
            except Exception:
                logger.warning("Impossible de recuperer le membre %s", user.id)
                member = user

        has_role = any(r.name.lower() == role_name_lower for r in member.roles) if hasattr(member, 'roles') else False
        if not has_role:
            await interaction.response.send_message(
                f"Acces refuse — role **{ROLE_NAME}** requis.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# ---- Session HTTP partagee ----

_http_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    """Retourne la session HTTP partagee, ou en cree une nouvelle si necessaire."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


# ---- Appels API ----

def _make_error(error_type: str, detail: str, status: int = 0) -> dict:
    """Construit un dict d'erreur typee pour les handlers."""
    return {"_error": True, "_error_type": error_type, "detail": detail, "_status": status}


async def _api_get_once(endpoint: str, params: dict | None, timeout_seconds: float,
                        log_connection_error: bool) -> dict | None:
    """Tentative unique d'appel GET. Retourne dict (succes ou erreur typee) ou None (connexion impossible)."""
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        session = _get_session()
        async with session.get(
            f"{API_BASE}{endpoint}",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 409:
                data = await resp.json()
                data["_error"] = True
                data["_error_type"] = "index_unavailable"
                data["_status"] = 409
                return data
            if resp.status == 429:
                return _make_error("rate_limit", "Trop de requetes. Reessayez dans quelques instants.", 429)
            if resp.status >= 500:
                return _make_error("server_error", "Le backend a rencontre une erreur interne.", resp.status)
            # Autres 4xx
            detail = ""
            try:
                body = await resp.json()
                detail = body.get("detail", "")
            except Exception:
                pass
            return _make_error("client_error", detail or f"Erreur HTTP {resp.status}.", resp.status)
    except asyncio.TimeoutError:
        logger.warning("Timeout appel API %s (%.0fs)", endpoint, timeout_seconds)
        return _make_error("timeout", "Le backend met trop de temps a repondre.", 0)
    except aiohttp.ClientConnectorError as exc:
        if log_connection_error:
            logger.warning("Backend injoignable pour %s: %s", endpoint, exc)
        return None
    except Exception:
        logger.exception("Erreur appel API %s", endpoint)
        return None


async def api_get(endpoint: str, params: dict = None, *,
                  log_connection_error: bool = True,
                  timeout: float = 30.0,
                  retries: int = 1,
                  retry_on_timeout: bool = True) -> dict | None:
    """Appel GET vers le backend avec retry sur timeout/connexion.

    retry_on_timeout=False sert aux recherches : apres un timeout, la requete
    tourne encore cote backend, la relancer doublerait la charge et l'attente.
    Une connexion impossible reste retentee.

    Retourne:
      - dict sans _error : succes
      - dict avec _error=True, _error_type, detail : erreur typee
      - None : backend totalement injoignable apres retries
    """
    last_result = None
    for attempt in range(1 + retries):
        result = await _api_get_once(endpoint, params, timeout, log_connection_error=(attempt == 0 and log_connection_error))
        # Succes ou erreur applicative (pas transitoire) : retourner immediatement
        if result is not None:
            error_type = result.get("_error_type", "")
            if error_type != "timeout" or not retry_on_timeout:
                return result
            # Timeout : retenter si retries restants
            last_result = result
        else:
            # None = connexion impossible : retenter aussi
            last_result = None
        if attempt < retries:
            logger.info("Retry %s (tentative %d/%d)", endpoint, attempt + 2, 1 + retries)
            await asyncio.sleep(2)
    return last_result


async def wait_for_backend(attempts: int = 20, delay_seconds: float = 2.0) -> dict | None:
    """Attend quelques instants que le backend reponde au health check."""
    for attempt in range(1, attempts + 1):
        health = await api_get("/api/health", log_connection_error=False)
        if health and health.get("status") == "ok":
            return health
        if attempt < attempts:
            logger.info("Backend indisponible (tentative %d/%d), nouvelle verification dans %.1fs", attempt, attempts, delay_seconds)
            await asyncio.sleep(delay_seconds)
    return None


# ---- Formatage des resultats ----

# Limite Discord de la description d'un embed.
EMBED_DESCRIPTION_LIMIT = 4096
# Contexte demande a l'API autour de chaque correspondance : 5 resultats
# doivent tenir dans un embed, inutile de rapatrier 2 x 2500 caracteres.
SEARCH_CONTEXT_CHARS = 300
# Extrait par resultat quand la page n'impose pas de budget plus petit.
MAX_SNIPPET_CHARS = 1500
_CODE_FENCE_OVERHEAD = len("\n```\n\n```")
# Rien ne contenait tous les mots : l'API a elargi a "au moins un mot".
RELAXED_NOTICE = "*Aucun resultat ne contient tous les mots : voici ceux qui en contiennent au moins un.*"


def _excerpt(text: str, highlights: list | None, max_chars: int) -> str:
    """Coupe un extrait a max_chars en gardant la premiere correspondance visible."""
    if len(text) <= max_chars:
        return text
    width = max_chars - 2  # place des deux marqueurs de coupure
    if width <= 0:
        return text[:max_chars]
    start = 0
    if highlights:
        hl_start, hl_end = highlights[0][0], highlights[0][1]
        start = max(0, hl_start - max(0, width - (hl_end - hl_start)) // 2)
    start = min(start, len(text) - width)
    end = start + width
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def format_hit(hit: dict, index: int, max_snippet_chars: int = MAX_SNIPPET_CHARS) -> str:
    """Formate un resultat de recherche en texte.

    L'extrait est borne a max_snippet_chars et centre sur la premiere
    correspondance. Les ``` du contenu sont neutralises pour ne pas fermer le
    bloc de code.
    """
    source = hit.get("source", "Inconnu")
    parts = source.replace("\\", "/").split("/")
    name = "Inconnu"
    for p in reversed(parts[:-1]):
        if p and p not in ("FRANCE", "FRANCE-CSV") and ":" not in p:
            name = p
            break

    score = hit.get("score", 0)
    hit_type = hit.get("type", "?")
    # Recherche sur tous les index : de quelle fuite vient le resultat.
    origin = f" [{hit['index_name']}]" if hit.get("index_name") else ""

    header = f"**{index}. {name}**{origin} ({hit_type}) — score: {score:.2f}"
    snippets = hit.get("snippets", [])
    if not snippets:
        return header
    text = _excerpt(snippets[0].get("text", ""), snippets[0].get("highlights"), max_snippet_chars)
    text = text.replace("```", "`\u200b``")
    if not text.strip():
        return header
    return f"{header}\n```\n{text}\n```"


def format_hits_page(hits: list, first_number: int, max_chars: int = EMBED_DESCRIPTION_LIMIT) -> str:
    """Formate une page de resultats sans depasser max_chars.

    La place est partagee entre les resultats : un extrait long ne masque plus
    les suivants, et les blocs de code restent fermes.
    """
    if not hits:
        return ""
    headers = [format_hit({**hit, "snippets": []}, first_number + i) for i, hit in enumerate(hits)]
    available = max_chars - 2 * (len(hits) - 1) - sum(len(h) for h in headers)
    budget = min(MAX_SNIPPET_CHARS, available // len(hits) - _CODE_FENCE_OVERHEAD)
    if budget < 20:
        blocks = headers
    else:
        blocks = [format_hit(hit, first_number + i, budget) for i, hit in enumerate(hits)]
    return truncate("\n\n".join(blocks), max_chars)


def truncate(text: str, max_len: int = 4096) -> str:
    """Tronque le texte pour respecter la limite Discord."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ---- Events ----

async def _setup_hook():
    """Initialise la session HTTP partagee au demarrage du bot."""
    _get_session()

bot.setup_hook = _setup_hook


_known_ready: set[str] = set()


@tasks.loop(seconds=30.0)
async def watch_indexes():
    """Detecte les index qui finissent de charger apres le demarrage du bot."""
    health = await api_get("/api/health", log_connection_error=False)
    if not health or health.get("status") != "ok":
        return
    ready = set(health.get("indexes_ready") or [])
    newly_ready = ready - _known_ready
    if newly_ready:
        logger.info("Index nouvellement charges : %s", ", ".join(sorted(newly_ready)))
    _known_ready.clear()
    _known_ready.update(ready)


@bot.event
async def on_ready():
    logger.info("Connecte en tant que %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logger.info("%d commandes slash synchronisees", len(synced))
    except Exception:
        logger.exception("Erreur sync commandes")

    # Verifier que le backend est disponible avec quelques retries.
    health = await wait_for_backend()
    if health and health.get("status") == "ok":
        ready = health.get("indexes_ready") or []
        loading = health.get("indexes_loading") or []
        suffix = f" : {', '.join(ready)}" if ready else ""
        if loading:
            suffix += f" (en cours : {', '.join(loading)})"
        logger.info(
            "Backend OK — %d index charges%s",
            health.get("indexes_loaded", 0),
            suffix,
        )
        _known_ready.clear()
        _known_ready.update(ready)
    else:
        logger.warning("Backend indisponible — verifiez que le serveur est lance")

    if not watch_indexes.is_running():
        watch_indexes.start()


async def _close_hook():
    """Ferme la session HTTP partagee a l'arret du bot."""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None

# Chainer avec le close existant du bot pour garantir l'appel
_original_close = bot.close


async def _patched_close():
    await _close_hook()
    await _original_close()

bot.close = _patched_close


# ---- Rendu breach par provider ----

def _format_breach_line(b: dict) -> str:
    """Formate une fuite individuelle en texte markdown pour Discord."""
    breach_name = b.get("name", "?")
    breach_date = b.get("breach_date", "")
    data_classes = b.get("data_classes", [])
    pwd_exposed = b.get("password_exposed", False)
    hibp = b.get("hibp_details") or {}
    bd_entries = b.get("bd_entries", [])
    domain = b.get("domain", "")
    description = b.get("description", "")

    line = f"• **{breach_name}**"
    title = hibp.get("title", "")
    if title and title != breach_name:
        line += f" ({title})"
    if breach_date:
        line += f" — {breach_date}"
    if pwd_exposed:
        line += " **[MDP]**"

    flags = []
    if hibp.get("is_verified"):
        flags.append("verifie")
    if hibp.get("is_sensitive"):
        flags.append("sensible")
    if hibp.get("is_fabricated"):
        flags.append("fabrique")
    if hibp.get("is_retired"):
        flags.append("retire")
    if hibp.get("is_spam_list"):
        flags.append("spam")
    if hibp.get("is_malware"):
        flags.append("malware")
    if hibp.get("is_subscription_free"):
        flags.append("abo gratuit")
    if hibp.get("is_stealer_log"):
        flags.append("stealer")
    if flags:
        line += f" [{', '.join(flags)}]"

    pwn = hibp.get("pwn_count", 0)
    if pwn:
        line += f"\n  {pwn:,} comptes affectes".replace(",", " ")

    if domain:
        line += f"\n  Domaine: {domain}"

    added = hibp.get("added_date", "")
    modified = hibp.get("modified_date", "")
    if added or modified:
        date_parts = []
        if added:
            date_parts.append(f"ajout: {added[:10]}")
        if modified:
            date_parts.append(f"maj: {modified[:10]}")
        line += f"\n  {' | '.join(date_parts)}"

    attribution = hibp.get("attribution", "")
    if attribution:
        line += f"\n  Attribution: {attribution}"

    disclosure = hibp.get("disclosure_url", "")
    if disclosure:
        line += f"\n  Disclosure: {disclosure}"

    if data_classes:
        line += f"\n  {', '.join(data_classes)}"

    if description:
        clean_desc = re.sub(r"<[^>]*>", "", description)
        if len(clean_desc) > 200:
            clean_desc = clean_desc[:200] + "..."
        line += f"\n  > {clean_desc}"

    for cred in bd_entries:
        parts = []
        if cred.get("password"):
            parts.append(f"mdp: {cred['password']}")
        if cred.get("hash_password"):
            parts.append("hash_pwd: oui")
        if cred.get("sha1"):
            parts.append(f"sha1: {cred['sha1']}")
        if cred.get("hash"):
            parts.append(f"hash: {cred['hash']}")
        if not parts and not cred.get("has_password"):
            parts.append("pas de mot de passe")
        line += f"\n  `{' | '.join(parts)}`"

    return line


# ---- Pagination ----

RESULTS_PER_PAGE = 5

# Discord embed description limit = 4096 chars.
# On reserve une marge pour les headers provider (**── name ──**) et le
# separateur \n\n entre blocs. 3800 est conservateur.
_BREACH_PAGE_MAX_CHARS = 3800


def _build_breach_pages(entries: list[tuple[str, str]]) -> list[list[int]]:
    """Decoupe les entries en pages respectant la limite de caracteres.

    Chaque page contient les indices des entries. Une page ne depasse pas
    _BREACH_PAGE_MAX_CHARS de texte cumule (entries + headers provider).
    Une entry seule trop longue occupe sa propre page (jamais perdue).
    """
    pages: list[list[int]] = []
    current_page: list[int] = []
    current_size = 0
    current_provider = None

    for i, (provider_name, text) in enumerate(entries):
        # Taille de cette entry = header provider eventuel + separateur + texte
        extra = 0
        if provider_name != current_provider:
            extra = len(f"**── {provider_name} ──**") + 2  # + \n\n
        entry_size = extra + len(text) + 2  # + \n\n separateur

        if current_page and current_size + entry_size > _BREACH_PAGE_MAX_CHARS:
            # Nouvelle page
            pages.append(current_page)
            current_page = []
            current_size = 0
            current_provider = None
            # Recalculer le header pour la nouvelle page
            extra = len(f"**── {provider_name} ──**") + 2
            entry_size = extra + len(text) + 2

        if provider_name != current_provider:
            current_provider = provider_name
        current_page.append(i)
        current_size += entry_size

    if current_page:
        pages.append(current_page)

    return pages if pages else [[]]


class BreachPaginator(discord.ui.View):
    """Pagination des resultats de fuites par provider.

    Les pages sont decoupees par taille cumulative de texte pour ne jamais
    depasser la limite Discord de 4096 chars sur la description d'un embed.
    """

    def __init__(self, email: str, providers: list, summary: dict, user_id: int):
        super().__init__(timeout=600)
        self.email = email
        self.message: discord.Message | None = None
        self.summary = summary
        self.user_id = user_id

        self._entries: list[tuple[str, str]] = []
        for provider in providers:
            name = provider.get("name", "?")
            if not provider.get("ok"):
                self._entries.append((name, f"**Erreur:** {provider.get('error', '?')}"))
                continue
            for b in provider.get("results", []):
                self._entries.append((name, _format_breach_line(b)))

        self._pages = _build_breach_pages(self._entries)
        self.page = 0
        self.max_page = len(self._pages) - 1
        self._update_buttons()

    def _update_buttons(self):
        self.bp_prev.disabled = self.page == 0
        self.bp_next.disabled = self.page >= self.max_page

    def build_embed(self) -> discord.Embed:
        page_indices = self._pages[self.page]
        page_entries = [self._entries[i] for i in page_indices]

        lines = []
        current_provider = None
        for provider_name, text in page_entries:
            if provider_name != current_provider:
                current_provider = provider_name
                lines.append(f"**── {provider_name} ──**")
            lines.append(text)

        total_hits = self.summary.get("total_hits", 0)
        ok_count = self.summary.get("providers_ok", 0)
        fail_count = self.summary.get("providers_failed", 0)

        embed = discord.Embed(
            title=f"Fuites — {self.email}",
            description="\n\n".join(lines),
            color=0xf43f5e if total_hits > 0 else 0x737373,
        )
        footer = f"{total_hits} fuite(s) — {ok_count} provider(s) OK, {fail_count} en erreur"
        footer += f" — page {self.page + 1}/{self.max_page + 1}"
        embed.set_footer(text=footer)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ce n'est pas votre recherche.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Precedent", style=discord.ButtonStyle.secondary, emoji="◀")
    async def bp_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Suivant", style=discord.ButtonStyle.primary, emoji="▶")
    async def bp_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_page, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            embed = self.build_embed()
            embed.set_footer(text=embed.footer.text + " — Interaction expiree, relancez la recherche")
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


class SearchPaginator(discord.ui.View):
    """Boutons Precedent / Suivant, avec pagination API optionnelle."""

    def __init__(
        self,
        query: str,
        hits: list,
        total: int,
        user_id: int,
        *,
        index: str | None = None,
        result_limit: int | None = None,
        match_mode: str | None = None,
        relaxed: bool = False,
        skipped_indexes: list | None = None,
    ):
        super().__init__(timeout=600)
        self.message: discord.Message | None = None
        self.query = query
        self.hits = hits
        self.total = total
        self.user_id = user_id
        self.index = index
        self.result_limit = result_limit
        # Les pages suivantes reprennent le mode de la page 1 et son total.
        self.match_mode = match_mode
        self.relaxed = relaxed
        self.skipped_indexes = skipped_indexes or []
        self.server_pagination = result_limit is not None
        self.display_total = min(total, result_limit) if result_limit is not None else len(hits)
        self.page = 0
        page_basis = self.display_total if self.server_pagination else len(hits)
        self.max_page = max(0, (page_basis - 1) // RESULTS_PER_PAGE)
        self._email = EMAIL_RE.search(query)
        if not self._email:
            self.btn_hibp.disabled = True
            self.btn_hibp.style = discord.ButtonStyle.secondary
        self._update_buttons()

    def _update_buttons(self):
        self.btn_prev.disabled = self.page == 0
        self.btn_next.disabled = self.page >= self.max_page

    def build_embed(self) -> discord.Embed:
        start = self.page * RESULTS_PER_PAGE
        if self.server_pagination:
            page_hits = self.hits
        else:
            end = start + RESULTS_PER_PAGE
            page_hits = self.hits[start:end]

        if self.relaxed:
            budget = EMBED_DESCRIPTION_LIMIT - len(RELAXED_NOTICE) - 2
            description = RELAXED_NOTICE + "\n\n" + format_hits_page(page_hits, start + 1, budget)
        else:
            description = format_hits_page(page_hits, start + 1)
        embed = discord.Embed(
            title=f"Recherche : {self.query}",
            description=description,
            color=0x3b82f6,
        )
        loaded = len(self.hits)
        if self.server_pagination and self.display_total < self.total:
            footer = (
                f"{self.display_total} premiers resultats sur {self.total} — "
                f"page {self.page + 1}/{self.max_page + 1}"
            )
        elif self.server_pagination:
            footer = f"{self.total} resultats — page {self.page + 1}/{self.max_page + 1}"
        elif loaded < self.total:
            footer = f"{loaded} resultats affiches sur {self.total} — page {self.page + 1}/{self.max_page + 1}"
        else:
            footer = f"{self.total} resultats — page {self.page + 1}/{self.max_page + 1}"
        if self.skipped_indexes:
            names = ", ".join(item.get("name") or item.get("id", "?") for item in self.skipped_indexes)
            footer += f" — index ignores : {names} (total minimum)"
        embed.set_footer(text=footer)
        return embed

    async def _load_server_page(self, target_page: int, interaction: discord.Interaction) -> bool:
        offset = target_page * RESULTS_PER_PAGE
        page_limit = min(RESULTS_PER_PAGE, max(0, self.display_total - offset))
        params = {
            "q": self.query,
            "limit": page_limit,
            "offset": offset,
            "context": SEARCH_CONTEXT_CHARS,
        }
        if self.index:
            params["index"] = self.index
        if self.match_mode in ("all", "any"):
            # Total deja connu : l'API ne recompte pas, mode de la page 1 garde.
            params["match"] = self.match_mode
            params["count"] = "false"

        data = await api_get("/api/search", params, timeout=180.0, retry_on_timeout=False)
        if data is None or data.get("_error"):
            await interaction.followup.send(embed=_error_embed(data), ephemeral=True)
            return False

        if data.get("total") is not None:
            self.total = data["total"]
        self.display_total = min(self.total, self.result_limit or self.total)
        self.max_page = max(0, (self.display_total - 1) // RESULTS_PER_PAGE)
        self.hits = data.get("hits", [])
        self.page = min(target_page, self.max_page)
        self._update_buttons()
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Ce n'est pas votre recherche.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Precedent", style=discord.ButtonStyle.secondary, emoji="◀")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_page = max(0, self.page - 1)
        if self.server_pagination:
            await interaction.response.defer()
            if await self._load_server_page(target_page, interaction) and self.message:
                await self.message.edit(embed=self.build_embed(), view=self)
            return
        self.page = target_page
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Suivant", style=discord.ButtonStyle.primary, emoji="▶")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_page = min(self.max_page, self.page + 1)
        if self.server_pagination:
            await interaction.response.defer()
            if await self._load_server_page(target_page, interaction) and self.message:
                await self.message.edit(embed=self.build_embed(), view=self)
            return
        self.page = target_page
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Fuites", style=discord.ButtonStyle.danger)
    async def btn_hibp(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._email:
            await interaction.response.send_message("Aucun email detecte dans la recherche.", ephemeral=True)
            return

        email = self._email.group(0)
        await interaction.response.defer(thinking=True, ephemeral=True)

        data = await api_get("/api/breaches", {"email": email}, timeout=45.0)
        if data is None or data.get("_error"):
            embed = _error_embed(data, "Erreur lors de la verification des fuites.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        summary = data.get("summary", {})
        providers = data.get("providers", [])
        total_hits = summary.get("total_hits", 0)

        if total_hits == 0 and summary.get("providers_failed", 0) == 0:
            embed = discord.Embed(
                title=f"Fuites — {email}",
                description="Aucune fuite connue.",
                color=0x22c55e,
            )
            status_parts = [f"{p['name']}: OK" for p in providers if p.get("ok")]
            if status_parts:
                embed.set_footer(text=" | ".join(status_parts))
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        paginator = BreachPaginator(email, providers, summary, interaction.user.id)
        msg = await interaction.followup.send(embed=paginator.build_embed(), view=paginator, ephemeral=True, wait=True)
        paginator.message = msg

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            embed = self.build_embed()
            embed.set_footer(text=embed.footer.text + " — Interaction expiree, relancez la recherche")
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass


# ---- Messages d'erreur Discord ----

_ERROR_EMBEDS = {
    "index_unavailable": ("Index indisponible", 0xf59e0b),
    "rate_limit": ("Limite atteinte", 0xf59e0b),
    "timeout": ("Timeout backend", 0xf59e0b),
    "server_error": ("Erreur backend", 0xf43f5e),
    "client_error": ("Erreur requete", 0xf43f5e),
}


def _error_embed(data: dict | None, fallback_desc: str = "Impossible de contacter le serveur local.") -> discord.Embed:
    """Construit un embed Discord adapte au type d'erreur."""
    if data is None:
        return discord.Embed(title="Backend injoignable", description=fallback_desc, color=0xf43f5e)
    error_type = data.get("_error_type", "")
    title, color = _ERROR_EMBEDS.get(error_type, ("Erreur", 0xf43f5e))
    detail = data.get("detail", fallback_desc)
    return discord.Embed(title=title, description=detail, color=color)


# ---- /search ----

@bot.tree.command(name="search", description="Rechercher dans l'index OSINT")
@app_commands.describe(
    query="Terme de recherche (email, nom, telephone...)",
    limit="Nombre de resultats (max 50)",
    index="Index a interroger (ex: france, index-foreign ; all = tous les index)",
)
@app_commands.checks.cooldown(5, 60.0)
@has_required_role()
async def cmd_search(interaction: discord.Interaction, query: str, limit: int = 20, index: str = None):
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    # Limiter la taille de la query
    if len(query) > 500:
        await interaction.response.send_message("Requete trop longue (max 500 caracteres).", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    params = {
        "q": query,
        "limit": min(limit, RESULTS_PER_PAGE),
        "offset": 0,
        "context": SEARCH_CONTEXT_CHARS,
    }
    if index:
        params["index"] = index
    data = await api_get("/api/search", params, timeout=180.0, retry_on_timeout=False)

    if data is None or data.get("_error"):
        await interaction.followup.send(embed=_error_embed(data))
        return

    total = data.get("total") or 0
    hits = data.get("hits", [])

    if total == 0:
        embed = discord.Embed(
            title=f"Recherche : {query}",
            description="Aucun resultat trouve.",
            color=0x737373,
        )
        await interaction.followup.send(embed=embed)
        return

    view = SearchPaginator(
        query,
        hits,
        total,
        interaction.user.id,
        index=index,
        result_limit=limit,
        match_mode=data.get("match_mode"),
        relaxed=bool(data.get("relaxed")),
        skipped_indexes=data.get("skipped_indexes"),
    )
    msg = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = msg


# ---- /stats ----

@bot.tree.command(name="stats", description="Statistiques de l'index")
@app_commands.describe(index="Index a interroger (ex: france, index-foreign)")
@app_commands.checks.cooldown(5, 60.0)
@has_required_role()
async def cmd_stats(interaction: discord.Interaction, index: str = None):
    await interaction.response.defer(thinking=True)

    params = {"index": index} if index else {}
    data = await api_get("/api/stats", params)

    if data is None or data.get("_error"):
        await interaction.followup.send(embed=_error_embed(data))
        return

    total_docs = data.get("total_docs", 0)
    index_size = data.get("index_size", "?")
    num_segments = data.get("num_segments", 0)

    index_name = data.get("index_name") or (index or "default")
    embed = discord.Embed(
        title=f"Statistiques — {index_name}",
        color=0x3b82f6,
    )
    embed.add_field(name="Documents indexes", value=f"{total_docs:,}".replace(",", " "), inline=True)
    embed.add_field(name="Taille de l'index", value=index_size, inline=True)
    embed.add_field(name="Segments", value=str(num_segments), inline=True)

    analysis = data.get("analysis")
    if analysis:
        emails = analysis.get("emails", {})
        phones = analysis.get("phones", {})

        embed.add_field(name="Emails uniques", value=f"{emails.get('unique_count', 0):,}".replace(",", " "), inline=True)
        embed.add_field(name="Telephones uniques", value=f"{phones.get('unique_count', 0):,}".replace(",", " "), inline=True)
        embed.add_field(name="Fichiers scannes", value=str(analysis.get("total_files", 0)), inline=True)

        top_domains = emails.get("top_domains", [])[:5]
        if top_domains:
            domains_text = "\n".join(
                f"`{d['domain']}` — {d['count']:,}".replace(",", " ")
                for d in top_domains
            )
            embed.add_field(name="Top domaines email", value=domains_text, inline=False)

    embed.set_footer(text="L'Oeil de Dieu — Index operationnel")

    await interaction.followup.send(embed=embed)


# ---- Gestion d'erreurs ----

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # CommandOnCooldown herite de CheckFailure : il doit etre teste en premier,
    # sinon l'utilisateur ne recoit aucune reponse.
    if isinstance(error, app_commands.CommandOnCooldown):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Commande en cooldown. Reessayez dans {error.retry_after:.0f}s.", ephemeral=True
            )
        return
    if isinstance(error, app_commands.CheckFailure):
        # has_required_role() a deja repondu a l'utilisateur.
        return
    logger.error("Erreur commande: %s", error)
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "Une erreur interne est survenue.", ephemeral=True
        )


# ---- Lancement ----

if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN manquant dans .env")
        exit(1)
    bot.run(TOKEN)
