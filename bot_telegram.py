# -*- coding: utf-8 -*-
"""Plataforma Cultural & Dantas Wats — Bot Telegram + Groq para Render."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from openai import OpenAI

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
log = logging.getLogger("bot")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Configuração: segredos exclusivamente por variáveis de ambiente ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "5672036643").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
PORT = int(os.getenv("PORT", "10000"))
MAX_WORKERS = max(2, min(16, int(os.getenv("BOT_WORKERS", "4"))))

BASE_DIR = Path(os.getenv("PLATAFORMA_DIR", "/data/plataforma_cultural" if os.getenv("RENDER") else "./plataforma_cultural"))
BASE_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = BASE_DIR / "data.json"
AUDIT_FILE = BASE_DIR / "audit.jsonl"

APP_CONFIG = {
    "brand_name": "Plataforma Cultural & Dantas Wats",
    "tagline": "Inteligência operacional para cultura e automação elétrica em Salvador.",
    "max_message_chars": 5000,
    "min_seconds_between_ai_calls": 3,
    "privacy_notice": "Protótipo. Não envie dados bancários ou sigilosos.",
    "plans": {
        "gratuito": {"label": "Explorador", "limits": {"projects": 1, "landingpages_month": 0}},
        "essencial": {"label": "Essencial", "limits": {"projects": 3, "landingpages_month": 2}},
        "profissional": {"label": "Profissional", "limits": {"projects": 10, "landingpages_month": 10}},
        "institucional": {"label": "Coletivos e Instituições", "limits": {"projects": 30, "landingpages_month": 30}},
    },
}
SYSTEM_PROMPT = (
    "Você é o assistente técnico, comercial e estratégico da Plataforma Cultural & Dantas Wats, "
    "em Salvador/BA. Atua em duas frentes: (1) captação e assessoria cultural (capoeira, shows, "
    "rider técnico e projetos para editais); (2) serviços elétricos e automação (painéis de comando, "
    "soft starters e NBR 5410). Responda em português, de forma clara, objetiva e prática. "
    "Não invente normas, valores ou prazos; quando faltar informação, diga o que precisa ser confirmado."
)
QUESTIONS = [
    ("nome", "Como se chama o projeto?"),
    ("area", "Qual é a linguagem/área? Ex.: capoeira, música, dança, elétrica, automação."),
    ("territorio", "Em qual município e estado acontecerá?"),
    ("resumo", "Descreva a ideia principal em poucas frases."),
    ("publico", "Quem será beneficiado e qual o público estimado?"),
    ("objetivos", "Quais resultados deseja alcançar?"),
    ("atividades", "Quais atividades serão realizadas?"),
    ("duracao", "Qual será a duração ou o período previsto?"),
    ("orcamento", "Qual o orçamento estimado e principais despesas?"),
    ("acessibilidade", "Que medidas de inclusão ou conformidade técnica serão adotadas?"),
]

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "PlataformaCulturalBot/1.0"})
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
client_groq = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY or "missing-api-key",
    timeout=45.0,
    max_retries=2,
)

DB_LOCK = threading.RLock()
USER_LOCKS_LOCK = threading.Lock()
USER_LOCKS: dict[str, threading.Lock] = {}
AI_CALLS_LOCK = threading.Lock()
LAST_AI_CALL: dict[str, float] = {}
STOP = threading.Event()


def load_json(path: Path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.exception("Não foi possível ler %s: %s", path, exc)
    return deepcopy(default)


DB = load_json(DATA_FILE, {"users": {}, "referrals": {}})
if not isinstance(DB, dict):
    DB = {"users": {}, "referrals": {}}
DB.setdefault("users", {})
DB.setdefault("referrals", {})


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_db() -> None:
    with DB_LOCK:
        save_json(DATA_FILE, DB)


def audit(event: str, actor_id=None, details=None) -> None:
    item = {"at": utc_now(), "event": event,
            "actor_id": str(actor_id) if actor_id is not None else None,
            "details": details or {}}
    try:
        with DB_LOCK:
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Falha ao gravar auditoria")


def get_user(uid) -> dict:
    key = str(uid)
    with DB_LOCK:
        if key not in DB["users"]:
            ref = secrets.token_urlsafe(6)
            DB["users"][key] = {
                "telegram_user_id": key,
                "plan": "institucional" if key == ADMIN_TELEGRAM_ID else "gratuito",
                "created_at": utc_now(), "state": None, "project": {},
                "projects": [], "referral_code": ref, "consent_notice_seen": False,
            }
            DB["referrals"][ref] = {"owner_id": key, "signups": 0}
            save_json(DATA_FILE, DB)
            audit("user_created", key)
        user = DB["users"][key]
        # Compatibilidade com registros antigos.
        user.setdefault("plan", "gratuito")
        user.setdefault("state", None)
        user.setdefault("project", {})
        user.setdefault("projects", [])
        user.setdefault("consent_notice_seen", False)
        return user


def user_lock(uid) -> threading.Lock:
    key = str(uid)
    with USER_LOCKS_LOCK:
        if key not in USER_LOCKS:
            USER_LOCKS[key] = threading.Lock()
        return USER_LOCKS[key]


# ---- HTTP health check para Render ----
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_error(404)
            return
        body = b"Plataforma Cultural & Dantas Wats: online"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def run_http_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.daemon_threads = True
    log.info("Servidor HTTP de saúde ativo na porta %s", PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


# ---- Telegram helpers ----
def tg(method: str, timeout=30, **payload) -> dict:
    if not TELEGRAM_API:
        return {"ok": False, "error_code": 0, "description": "TELEGRAM_BOT_TOKEN não configurado"}
    try:
        response = HTTP.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=timeout)
        try:
            result = response.json()
        except ValueError:
            return {"ok": False, "error_code": response.status_code, "description": response.text[:200]}
        if response.status_code >= 400 and result.get("ok"):
            return {"ok": False, "error_code": response.status_code, "description": "Erro HTTP do Telegram"}
        return result
    except requests.RequestException as exc:
        return {"ok": False, "error_code": 0, "description": str(exc)[:200]}


def split_text(text: str, size=4000) -> list[str]:
    remaining = str(text).strip() or "(sem conteúdo)"
    chunks = []
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    return chunks


def send_message(chat_id, text) -> None:
    for chunk in split_text(text):
        result = tg("sendMessage", timeout=20, chat_id=chat_id, text=chunk)
        if not result.get("ok"):
            log.warning("sendMessage falhou: %s", result.get("description"))
            # Limite de taxa/erro temporário: tentativa única curta.
            if result.get("error_code") == 429:
                retry = result.get("parameters", {}).get("retry_after", 2)
                time.sleep(min(max(int(retry), 1), 10))
                result = tg("sendMessage", timeout=20, chat_id=chat_id, text=chunk)
                if not result.get("ok"):
                    log.warning("Repetição sendMessage falhou: %s", result.get("description"))


def ask_ai(user_prompt: str) -> str:
    if not GROQ_API_KEY:
        return "A IA ainda não está configurada. O administrador deve definir GROQ_API_KEY nas variáveis de ambiente."
    try:
        completion = client_groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_completion_tokens=1600,
        )
        return (completion.choices[0].message.content or "").strip() or "A IA não retornou conteúdo. Tente novamente."
    except Exception:
        log.exception("Erro ao consultar a Groq")
        return "Não consegui consultar a IA agora. Tente novamente em instantes."


def too_fast(uid) -> bool:
    now = time.monotonic()
    key = str(uid)
    with AI_CALLS_LOCK:
        last = LAST_AI_CALL.get(key, 0.0)
        if now - last < APP_CONFIG["min_seconds_between_ai_calls"]:
            return True
        LAST_AI_CALL[key] = now
        return False


def start_project(user) -> str:
    plan = user.get("plan", "gratuito")
    if plan not in APP_CONFIG["plans"]:
        plan = "gratuito"
        user["plan"] = plan
    limit = APP_CONFIG["plans"][plan]["limits"]["projects"]
    if len(user["projects"]) >= limit:
        return f"Seu plano permite {limit} projeto(s), e o limite foi atingido. Veja /planos."
    user["project"] = {}
    user["state"] = {"flow": "project", "index": 0}
    save_db()
    return (f"Vamos estruturar seu projeto por etapas. {APP_CONFIG['privacy_notice']}\n"
            f"(Envie /cancelar para sair.)\n\n1/{len(QUESTIONS)} — {QUESTIONS[0][1]}")


def answer_project(user, answer: str) -> str:
    state = user.get("state") or {}
    index = int(state.get("index", 0))
    if index < 0 or index >= len(QUESTIONS):
        user["state"], user["project"] = None, {}
        save_db()
        return "O fluxo foi reiniciado por segurança. Use /novo_projeto para começar novamente."
    field, _ = QUESTIONS[index]
    user["project"][field] = answer[:APP_CONFIG["max_message_chars"]]
    index += 1
    if index < len(QUESTIONS):
        user["state"]["index"] = index
        save_db()
        return f"{index + 1}/{len(QUESTIONS)} — {QUESTIONS[index][1]}"
    finished = deepcopy(user["project"])
    finished["created_at"] = utc_now()
    user["projects"].append(finished)
    user["project"], user["state"] = {}, None
    save_db()
    audit("project_created", user["telegram_user_id"], {"nome": finished.get("nome")})
    return "Projeto registrado com sucesso! Use /diagnostico para a análise técnica e comercial."


def generate_diagnosis(user) -> str:
    if not user["projects"]:
        return "Ainda não há projeto registrado. Use /novo_projeto."
    project = {k: v for k, v in user["projects"][-1].items() if k != "created_at"}
    return ask_ai(
        "Analise este projeto/demanda e forneça diagnóstico técnico e comercial, riscos, próximos passos "
        "e sugestões. Diferencie fatos de hipóteses e não invente informações:\n"
        + json.dumps(project, ensure_ascii=False, indent=2)
    )


def help_text() -> str:
    return (
        f"{APP_CONFIG['brand_name']}\n\n"
        "/novo_projeto — Iniciar novo projeto ou atendimento\n"
        "/diagnostico — Análise técnica e comercial via IA\n"
        "/planos — Consultar planos e limites\n"
        "/cancelar — Cancelar o preenchimento atual\n"
        "/ajuda — Exibir comandos disponíveis\n\n"
        "Você também pode escrever uma dúvida diretamente aqui."
    )


def plans_text(user) -> str:
    lines = ["Planos disponíveis:\n"]
    for key, plan in APP_CONFIG["plans"].items():
        limits = plan["limits"]
        marker = "  ← seu plano" if key == user.get("plan") else ""
        lines.append(f"• {plan['label']}: {limits['projects']} projeto(s), "
                     f"{limits['landingpages_month']} landing page(s)/mês{marker}")
    return "\n".join(lines)


def handle_update(update: dict) -> None:
    message = update.get("message")
    if not message or not message.get("text"):
        return
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    uid, chat_id = sender.get("id"), chat.get("id")
    incoming = message["text"].strip()
    if uid is None or chat_id is None or not incoming:
        return
    if chat.get("type") != "private":
        send_message(chat_id, "Por segurança, use o bot em conversa privada.")
        return

    # Serializa mensagens do mesmo usuário; usuários diferentes podem ser atendidos em paralelo.
    with user_lock(uid):
        user = get_user(uid)
        state = user.get("state") or {}
        command = incoming.split()[0].split("@")[0].lower() if incoming.startswith("/") else None

        if command == "/start":
            user["consent_notice_seen"] = True
            save_db()
            send_message(chat_id, f"Olá! Sou o assistente da Plataforma Cultural & Dantas Wats.\n\n{help_text()}")
        elif command in ("/ajuda", "/help"):
            send_message(chat_id, help_text())
        elif command == "/planos":
            send_message(chat_id, plans_text(user))
        elif command == "/cancelar":
            user["state"], user["project"] = None, {}
            save_db()
            send_message(chat_id, "Preenchimento cancelado. Use /novo_projeto quando quiser recomeçar.")
        elif command == "/novo_projeto":
            send_message(chat_id, start_project(user))
        elif command == "/diagnostico":
            if not user["projects"]:
                send_message(chat_id, "Ainda não há projeto registrado. Use /novo_projeto.")
            elif too_fast(uid):
                send_message(chat_id, "Aguarde alguns segundos antes de pedir outra análise.")
            else:
                send_message(chat_id, "⚡ Analisando com inteligência artificial...")
                audit("diagnosis", uid)
                send_message(chat_id, generate_diagnosis(user))
        elif command is not None:
            send_message(chat_id, "Comando desconhecido. Use /ajuda.")
        elif state.get("flow") == "project":
            send_message(chat_id, answer_project(user, incoming))
        else:
            if too_fast(uid):
                send_message(chat_id, "Aguarde alguns segundos antes de enviar outra pergunta.")
                return
            send_message(chat_id, "⚡ Processando sua demanda...")
            audit("free_chat", uid)
            send_message(chat_id, ask_ai(incoming[:APP_CONFIG["max_message_chars"]]))


def process_update_safe(update: dict) -> None:
    try:
        handle_update(update)
    except Exception:
        log.exception("Falha ao processar update_id=%s", update.get("update_id"))


def run_bot() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Configuração ausente: defina TELEGRAM_BOT_TOKEN nas variáveis de ambiente.")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY não configurada: comandos básicos funcionam, mas a IA ficará indisponível.")

    me = tg("getMe", timeout=20)
    if not me.get("ok"):
        raise SystemExit(f"Telegram não autenticou o bot: {me.get('description', 'erro desconhecido')}")
    username = me.get("result", {}).get("username", "desconhecido")

    webhook = tg("deleteWebhook", timeout=20, drop_pending_updates=False)
    if not webhook.get("ok"):
        log.warning("Não foi possível remover webhook: %s", webhook.get("description"))
    commands = [
        {"command": "start", "description": "Iniciar o bot"},
        {"command": "novo_projeto", "description": "Iniciar novo projeto"},
        {"command": "diagnostico", "description": "Análise técnica e comercial"},
        {"command": "planos", "description": "Ver planos e limites"},
        {"command": "cancelar", "description": "Cancelar preenchimento"},
        {"command": "ajuda", "description": "Ver comandos"},
    ]
    tg("setMyCommands", timeout=20, commands=commands)
    log.info("Bot iniciado: @%s", username)

    offset = None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="telegram-update") as pool:
        while not STOP.is_set():
            try:
                params = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    params["offset"] = offset
                data = tg("getUpdates", timeout=35, **params)
                if not data.get("ok"):
                    code = data.get("error_code")
                    log.error("getUpdates falhou (%s): %s", code, data.get("description"))
                    if code == 409:
                        log.error("Conflito de polling: outro processo ou webhook está usando este bot.")
                        time.sleep(10)
                    elif code in (401, 404):
                        raise SystemExit("Token inválido/revogado. Gere um novo no @BotFather.")
                    else:
                        time.sleep(5)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    pool.submit(process_update_safe, update)
            except KeyboardInterrupt:
                log.info("Encerramento solicitado.")
                STOP.set()
            except SystemExit:
                raise
            except Exception:
                log.exception("Erro inesperado no loop de polling")
                time.sleep(3)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN nas variáveis de ambiente.")
    http_thread = threading.Thread(target=run_http_server, name="health-http", daemon=True)
    http_thread.start()
    run_bot()


if __name__ == "__main__":
    main()
