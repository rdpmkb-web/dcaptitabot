# -*- coding: utf-8 -*-
"""
Plataforma Cultural & Dantas Wats — Bot Telegram + Groq (Versão Produção Render)
"""
import os
import time
import json
import secrets
import logging
from copy import deepcopy
from pathlib import Path
from datetime import datetime, timezone
from getpass import getpass

import requests
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ==========================================
# 1. CONFIGURAÇÃO E SEGREDOS
# ==========================================
def get_secret(name):
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:
        from google.colab import userdata
        value = userdata.get(name)
        if value:
            return value.strip()
    except Exception:
        pass
    return getpass(f"Digite {name}: ").strip()

TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = get_secret("GROQ_API_KEY")
ADMIN_TELEGRAM_ID = "5672036643"
GROQ_MODEL = "openai/gpt-oss-120b"

_default_dir = "/content/plataforma_cultural" if Path("/content").exists() else "./plataforma_cultural"
BASE_DIR = Path(os.environ.get("PLATAFORMA_DIR", _default_dir))
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

client_groq = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
HTTP = requests.Session()

# ==========================================
# 2. PERSISTÊNCIA
# ==========================================
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (json.JSONDecodeError, OSError):
        return default

def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def audit(event, actor_id=None, details=None):
    item = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "actor_id": str(actor_id) if actor_id else None,
        "details": details or {},
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

DB = load_json(DATA_FILE, {"users": {}, "referrals": {}})
DB.setdefault("users", {})
DB.setdefault("referrals", {})

def save_db():
    save_json(DATA_FILE, DB)

def get_user(uid):
    k = str(uid)
    if k not in DB["users"]:
        ref = secrets.token_urlsafe(6)
        DB["users"][k] = {
            "telegram_user_id": k,
            "plan": "institucional" if k == ADMIN_TELEGRAM_ID else "gratuito",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state": None,
            "project": {},
            "projects": [],
            "last_message_at": 0,
            "referral_code": ref,
            "consent_notice_seen": False,
        }
        DB["referrals"][ref] = {"owner_id": k, "signups": 0}
        save_db()
        audit("user_created", k)
    return DB["users"][k]

# ==========================================
# 3. TELEGRAM
# ==========================================
def tg(method, timeout=30, **payload):
    try:
        r = HTTP.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=timeout)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
    except requests.RequestException as e:
        return {"ok": False, "error_code": 0, "description": str(e)}

def split_text(text, size=4000):
    text = str(text).strip() or "(sem conteúdo)"
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks

def send_message(chat_id, text):
    last = None
    for chunk in split_text(text):
        last = tg("sendMessage", chat_id=chat_id, text=chunk)
        if not last.get("ok"):
            log.warning("Falha no sendMessage: %s", last.get("description"))
    return last

# ==========================================
# 4. IA
# ==========================================
SYSTEM_PROMPT = (
    "Você é o assistente técnico, comercial e estratégico de Romilson Dantas Pereira, em Salvador/BA. "
    "Atua em duas frentes: (1) captação e assessoria cultural (capoeira, shows, rider técnico, projetos "
    "para editais); (2) serviços elétricos e automação (Dantas Wats: painéis de comando, soft starters, "
    "NBR 5410). Responda em português, de forma clara, objetiva e prática. Não invente normas, valores "
    "ou prazos; quando faltar informação, diga o que precisa ser confirmado."
)

def ask_ai(user_prompt):
    try:
        comp = client_groq.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_completion_tokens=2000,
            timeout=60,
        )
        return (comp.choices[0].message.content or "").strip() or "A IA não retornou conteúdo. Tente novamente."
    except Exception:
        log.exception("Erro ao consultar a Groq")
        return "Não consegui consultar a IA agora. Tente novamente em instantes."

def too_fast(user):
    now = time.time()
    if now - float(user.get("last_message_at") or 0) < APP_CONFIG["min_seconds_between_ai_calls"]:
        return True
    user["last_message_at"] = now
    save_db()
    return False

# ==========================================
# 5. FLUXO DE PROJETOS
# ==========================================
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

def start_project(user):
    limit = APP_CONFIG["plans"][user["plan"]]["limits"]["projects"]
    if len(user["projects"]) >= limit:
        return f"Seu plano permite {limit} projeto(s) e o limite foi atingido. Veja /planos."
    user["project"] = {}
    user["state"] = {"flow": "project", "index": 0}
    save_db()
    return (f"Vamos estruturar seu projeto por etapas. {APP_CONFIG['privacy_notice']}\n"
            f"(Envie /cancelar para sair.)\n\n1/{len(QUESTIONS)} — {QUESTIONS[0][1]}")

def answer_project(user, answer):
    i = int(user["state"]["index"])
    field, _ = QUESTIONS[i]
    user["project"][field] = answer[:APP_CONFIG["max_message_chars"]]
    i += 1
    if i < len(QUESTIONS):
        user["state"]["index"] = i
        save_db()
        return f"{i + 1}/{len(QUESTIONS)} — {QUESTIONS[i][1]}"
    finished = deepcopy(user["project"])
    finished["created_at"] = datetime.now(timezone.utc).isoformat()
    user["projects"].append(finished)
    user["project"] = {}
    user["state"] = None
    save_db()
    audit("project_created", user["telegram_user_id"], {"nome": finished.get("nome")})
    return "Projeto registrado com sucesso! Use /diagnostico para a análise técnica e comercial."

def generate_diagnosis(user):
    if not user["projects"]:
        return "Ainda não há projeto registrado. Use /novo_projeto."
    project = {k: v for k, v in user["projects"][-1].items() if k != "created_at"}
    data = json.dumps(project, ensure_ascii=False, indent=2)
    return ask_ai(f"Analise este projeto/demanda e forneça um diagnóstico técnico e comercial, "
                  f"com riscos, próximos passos e sugestões:\n{data}")

# ==========================================
# 6. COMANDOS
# ==========================================
def help_text():
    return (
        f"{APP_CONFIG['brand_name']}\n\n"
        "/novo_projeto — Iniciar novo projeto ou atendimento\n"
        "/diagnostico — Análise técnica e comercial via IA\n"
        "/planos — Consultar planos e limites\n"
        "/cancelar — Cancelar o preenchimento atual\n"
        "/ajuda — Exibir comandos disponíveis\n\n"
        "Você também pode escrever uma dúvida direto aqui que eu respondo."
    )

def plans_text(user):
    lines = ["Planos disponíveis:\n"]
    for key, p in APP_CONFIG["plans"].items():
        lim = p["limits"]
        mark = "  ← seu plano" if key == user["plan"] else ""
        lines.append(f"• {p['label']}: {lim['projects']} projeto(s), "
                     f"{lim['landingpages_month']} landing page(s)/mês{mark}")
    return "\n".join(lines)

def handle_update(update):
    msg = update.get("message")
    if not msg or not msg.get("text"):
        return

    chat = msg.get("chat", {})
    sender = msg.get("from", {})
    uid, chat_id = sender.get("id"), chat.get("id")
    text_in = msg["text"].strip()
    if uid is None or chat_id is None or not text_in:
        return
    if chat.get("type") != "private":
        send_message(chat_id, "Por segurança, use o bot em conversa privada.")
        return

    user = get_user(uid)
    state = user.get("state") or {}
    cmd = text_in.split()[0].split("@")[0].lower() if text_in.startswith("/") else None

    if cmd == "/start":
        user["consent_notice_seen"] = True
        save_db()
        send_message(chat_id, f"Olá! Sou o assistente de Romilson Dantas.\n\n{help_text()}")
    elif cmd in ("/ajuda", "/help"):
        send_message(chat_id, help_text())
    elif cmd == "/planos":
        send_message(chat_id, plans_text(user))
    elif cmd == "/cancelar":
        user["state"], user["project"] = None, {}
        save_db()
        send_message(chat_id, "Preenchimento cancelado. Use /novo_projeto quando quiser recomeçar.")
    elif cmd == "/novo_projeto":
        send_message(chat_id, start_project(user))
    elif cmd == "/diagnostico":
        if not user["projects"]:
            send_message(chat_id, "Ainda não há projeto registrado. Use /novo_projeto.")
        elif too_fast(user):
            send_message(chat_id, "Aguarde alguns segundos antes de pedir outra análise.")
        else:
            send_message(chat_id, "⚡ Analisando com inteligência artificial...")
            audit("diagnosis", uid)
            send_message(chat_id, generate_diagnosis(user))
    elif cmd is not None:
        send_message(chat_id, "Comando desconhecido. Use /ajuda.")
    elif state.get("flow") == "project":
        send_message(chat_id, answer_project(user, text_in))
    else:
        if too_fast(user):
            send_message(chat_id, "Aguarde alguns segundos antes de enviar outra pergunta.")
            return
        send_message(chat_id, "⚡ Processando sua demanda...")
        audit("free_chat", uid)
        send_message(chat_id, ask_ai(text_in[:APP_CONFIG["max_message_chars"]]))

# ==========================================
# 7. LOOP DE ESCUTA (long polling)
# ==========================================
def run_bot():
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit(f"Token inválido ou sem acesso ao Telegram: {me.get('description')}")
    username = me["result"].get("username")

    tg("deleteWebhook", drop_pending_updates=False)
    tg("setMyCommands", commands=[
        {"command": "novo_projeto", "description": "Iniciar novo projeto"},
        {"command": "diagnostico", "description": "Análise técnica e comercial"},
        {"command": "planos", "description": "Planos e limites"},
        {"command": "cancelar", "description": "Cancelar preenchimento"},
        {"command": "ajuda", "description": "Ver comandos"},
    ])

    log.info("🚀 Bot iniciado: @%s — pressione Stop para encerrar.", username)
    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            data = tg("getUpdates", timeout=30, **params)

            if not data.get
