# -*- coding: utf-8 -*-
"""
Нейросети, кроме Claude:

• ChatGPT — через официальный Codex CLI (вход по подписке ChatGPT, в т. ч. бесплатной);
• Gemini, OpenRouter, Ollama — через OpenAI-совместимый API (ключ или локальный сервер);
• GigaChat — через API Сбера (ключ авторизации → токен на 30 минут).

Для всех один «Runner»: события в очередь те же, что у Claude Code
(status / msg_start / reset / delta / done / fail), поэтому «Управление» и оверлей
работают с любой нейросетью одинаково.
"""
import base64
import ctypes
import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from core import (
    DATA, DEFAULT_CONFIG, IS_WIN, NO_WINDOW, WORK, ClaudeRunner, find_claude, load_json,
    resolve_error, save_json,
)

PROVIDERS = [
    ("claude", "Claude — подписка Claude"),
    ("codex", "ChatGPT — через Codex (бесплатно или подписка)"),
    ("gemini", "Gemini — бесплатный API-ключ Google"),
    ("openrouter", "OpenRouter — бесплатные модели по ключу"),
    ("ollama", "Ollama — локально на компьютере"),
    ("gigachat", "GigaChat — Сбер, без VPN"),
]
PROVIDER_SHORT = {"claude": "Claude", "codex": "ChatGPT", "gemini": "Gemini",
                  "openrouter": "OpenRouter", "ollama": "Ollama", "gigachat": "GigaChat"}

# OpenAI-совместимые API
API_PRESETS = {
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-flash-latest", "key_url": "https://aistudio.google.com/apikey",
               "effort": "reasoning_effort"},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "model": "openrouter/free",
                   "key_url": "https://openrouter.ai/keys", "effort": "openrouter"},
    "ollama": {"base": "http://localhost:11434/v1", "model": "qwen2.5vl:7b",
               "key_url": "https://ollama.com/download", "effort": None, "no_key": True},
}
GIGACHAT = {"oauth": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            "base": "https://api.giga.chat/v1", "model": "GigaChat-2-Max",
            "key_url": "https://developers.sber.ru/studio"}
GIGA_SCOPES = ["GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"]

DEFAULT_PROVIDERS_CFG = DEFAULT_CONFIG["providers"]

API_HISTORY_LIMIT = 30          # сколько последних сообщений помнят API-нейросети
CONTEXT_CHARS = 15000           # сколько текста прошлой переписки передавать при смене нейросети


def provider_of(chat, cfg):
    chat = chat or {}
    p = chat.get("provider")
    if not p:  # чаты из прошлых версий (уже с перепиской) — это Claude
        p = "claude" if (chat.get("history") or chat.get("session_id")) else \
            (cfg.get("providers", {}).get("default") or "claude")
    return p if p in PROVIDER_SHORT else "claude"


def pcfg(cfg, provider):
    return cfg.get("providers", {}).get(provider, {}) or {}


# ----------------------------------------------------------------------------
# Ключи: хранятся отдельно от настроек, на Windows — зашифрованы для твоей учётной записи
# ----------------------------------------------------------------------------
SECRETS_PATH = DATA / "secrets.json"


def _dpapi(data: bytes, protect: bool) -> bytes:
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    c = ctypes.windll.crypt32
    fn = c.CryptProtectData if protect else c.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI не сработал")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def get_secret(name):
    raw = (load_json(SECRETS_PATH, {}) or {}).get(name, "")
    try:
        if raw.startswith("dpapi:"):
            return _dpapi(base64.b64decode(raw[6:]), False).decode("utf-8")
        if raw.startswith("plain:"):
            return raw[6:]
    except Exception:
        return ""
    return raw or ""


def set_secret(name, value):
    data = load_json(SECRETS_PATH, {}) or {}
    value = (value or "").strip()
    if not value:
        data.pop(name, None)
    elif IS_WIN:
        try:
            data[name] = "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode()
        except Exception:
            data[name] = "plain:" + value
    else:
        data[name] = "plain:" + value
    DATA.mkdir(parents=True, exist_ok=True)
    save_json(SECRETS_PATH, data)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Codex CLI (ChatGPT)
# ----------------------------------------------------------------------------
def find_codex(cfg_path=""):
    if cfg_path and Path(cfg_path).exists():
        return cfg_path
    p = shutil.which("codex")
    if p:
        return p
    home = Path.home()
    for cand in (Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd",
                 home / ".local" / "bin" / ("codex.exe" if IS_WIN else "codex"),
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "codex" / "codex.exe"):
        if str(cand) not in (".", "") and cand.exists():
            return str(cand)
    return None


def codex_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=30,
                           creationflags=NO_WINDOW)
        return r.stdout.decode("utf-8", "replace").strip()[:40]
    except Exception:
        return ""


def codex_status(exe):
    """(вошёл ли, описание) — через `codex login status`."""
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True, timeout=40,
                           creationflags=NO_WINDOW)
    except Exception as e:
        return False, f"Не удалось проверить вход: {e}"
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    low = out.lower()
    if r.returncode == 0 and "not logged in" not in low:
        how = "аккаунт ChatGPT" if "chatgpt" in low else ("API-ключ" if "api key" in low else "")
        return True, "✔ Вход выполнен" + (f" · {how}" if how else "")
    return False, "Вход не выполнен."


def codex_install_command():
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-Command",
                "$env:CODEX_INSTALLER_USE_RELEASES_OPENAI_COM='false'; "
                "irm https://chatgpt.com/codex/install.ps1 | iex"]
    return ["bash", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"]


CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


# ----------------------------------------------------------------------------
# HTTP-помощники (без сторонних библиотек)
# ----------------------------------------------------------------------------
def _ssl_ctx(cafile=""):
    ctx = ssl.create_default_context()
    if cafile and Path(cafile).exists():
        ctx.load_verify_locations(cafile=cafile)
    return ctx


def _http_json(url, data=None, headers=None, timeout=60, cafile="", method=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode("utf-8") if data is not None else None)
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(cafile)) as r:
        return json.loads(r.read().decode("utf-8"))


def explain_http_error(provider, e):
    """Понятный текст ошибки сети/API."""
    name = PROVIDER_SHORT.get(provider, provider)
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        msg = body
        try:
            j = json.loads(body)
            err = j.get("error", j)
            msg = err.get("message") if isinstance(err, dict) else str(err)
            msg = msg or body
        except Exception:
            pass
        msg = (msg or "").strip()[:400]
        if e.code in (401, 403):
            return f"⚠ {name}: ключ не подходит или нет доступа (код {e.code}). Проверь ключ в " \
                   f"⚙ Настройки → Нейросети.\n\n{msg}"
        if e.code == 429:
            return f"⚠ {name}: исчерпан лимит запросов (часто — бесплатный дневной). " \
                   f"Попробуй позже или выбери другую модель.\n\n{msg}"
        if e.code == 404:
            return f"⚠ {name}: модель или адрес не найдены (код 404). Выбери модель в " \
                   f"⚙ Настройки → Нейросети → «Список моделей».\n\n{msg}"
        return f"⚠ {name}: ошибка {e.code}.\n\n{msg}"
    if isinstance(e, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
        extra = (" Для GigaChat нужен сертификат НУЦ Минцифры: установи «Russian Trusted Root CA» "
                 "в Windows (Госуслуги) или укажи файл сертификата в настройках GigaChat."
                 if provider == "gigachat" else "")
        return f"⚠ {name}: не удалось проверить сертификат сервера.{extra}\n\n{e}"
    if isinstance(e, urllib.error.URLError):
        reason = str(getattr(e, "reason", e))
        if provider == "ollama":
            return ("⚠ Ollama не отвечает. Установи и запусти Ollama (ollama.com), затем скачай "
                    "модель с картинками, например: ollama pull qwen2.5vl:7b\n\n" + reason)
        return f"⚠ {name}: нет связи с сервером ({reason}). Проверь интернет или VPN."
    return f"⚠ {name}: {e}"


# --- GigaChat: токен на 30 минут ------------------------------------------------
_giga_token = {"value": None, "exp": 0, "key": None}
_giga_lock = threading.Lock()


def giga_token(auth_key, scope, cafile=""):
    with _giga_lock:
        if (_giga_token["value"] and _giga_token["key"] == (auth_key, scope)
                and time.time() < _giga_token["exp"] - 120):
            return _giga_token["value"]
        j = _http_json(GIGACHAT["oauth"], raw=f"scope={scope}".encode(), cafile=cafile, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
            "RqUID": str(uuid.uuid4()), "Authorization": f"Basic {auth_key.strip()}"})
        tok = j.get("access_token")
        if not tok:
            raise RuntimeError(f"GigaChat не выдал токен: {j}")
        exp = j.get("expires_at", 0)
        exp = exp / 1000 if exp > 1e11 else (exp or time.time() + 1800)
        _giga_token.update(value=tok, exp=exp, key=(auth_key, scope))
        return tok


def giga_upload(token, data: bytes, mime, cafile=""):
    boundary = "----overlay" + uuid.uuid4().hex
    ext = "png" if "png" in mime else "jpg"
    parts = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\ngeneral\r\n"
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"screen.{ext}\"\r\nContent-Type: {mime}\r\n\r\n").encode() + data + \
        f"\r\n--{boundary}--\r\n".encode()
    j = _http_json(GIGACHAT["base"] + "/files", raw=parts, cafile=cafile, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=120)
    return j["id"]


# ----------------------------------------------------------------------------
# Готовность нейросети и список моделей (для настроек и проверки при запуске)
# ----------------------------------------------------------------------------
def provider_ready(provider, cfg):
    """(готова ли, что сделать). Без сетевых запросов, кроме проверки входа CLI."""
    if provider == "claude":
        return (bool(find_claude(cfg.get("claude_path", ""))),
                "Claude Code не найден: ⚙ Настройки → Аккаунт.")
    if provider == "codex":
        exe = find_codex(pcfg(cfg, "codex").get("path", ""))
        if not exe:
            return False, "Codex не установлен: ⚙ Настройки → Нейросети → ChatGPT → «Установить»."
        ok, _ = codex_status(exe)
        return ok, "" if ok else "Не выполнен вход в ChatGPT: ⚙ Настройки → Нейросети → ChatGPT."
    if provider in API_PRESETS:
        if API_PRESETS[provider].get("no_key") or get_secret(provider):
            return True, ""
        return False, f"Нет ключа {PROVIDER_SHORT[provider]}: ⚙ Настройки → Нейросети."
    if provider == "gigachat":
        return (bool(get_secret("gigachat")),
                "Нет ключа GigaChat: ⚙ Настройки → Нейросети → GigaChat.")
    return False, "Неизвестная нейросеть"


def list_models(provider, cfg):
    """Список моделей с сервера (для выбора в настройках)."""
    if provider in API_PRESETS:
        p = API_PRESETS[provider]
        base = (pcfg(cfg, provider).get("base") or p["base"]).rstrip("/")
        hdr = {"Accept": "application/json"}
        key = get_secret(provider)
        if key:
            hdr["Authorization"] = f"Bearer {key}"
        j = _http_json(base + "/models", headers=hdr, timeout=30)
        items = j.get("data", [])
        ids = []
        for m in items:
            mid = str(m.get("id", "")).removeprefix("models/")
            if provider == "openrouter":
                mods = (m.get("architecture") or {}).get("input_modalities") or []
                if "image" not in mods:
                    continue  # для скриншотов нужны модели, которые видят картинки
                free = mid.endswith(":free")
                ids.append((0 if free else 1, mid))
            else:
                ids.append((0, mid))
        ids.sort()
        out = [m for _, m in ids]
        if provider == "openrouter":
            out = ["openrouter/free"] + out
        return out
    if provider == "gigachat":
        g = pcfg(cfg, "gigachat")
        tok = giga_token(get_secret("gigachat"), g.get("scope", "GIGACHAT_API_PERS"), g.get("ca_file", ""))
        j = _http_json(GIGACHAT["base"] + "/models", cafile=g.get("ca_file", ""),
                       headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        return [m.get("id") for m in j.get("data", []) if m.get("id")]
    return []


# ----------------------------------------------------------------------------
# Подготовка запроса
# ----------------------------------------------------------------------------
def _instructions():
    try:
        return (WORK / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _transcript(chat):
    """Текст прошлой переписки чата — для нейросети, у которой ещё нет своей истории."""
    lines = []
    for m in (chat or {}).get("history", []):
        who = "Пользователь" if m.get("role") == "you" else "Ассистент"
        if m.get("role") in ("you", "claude"):
            lines.append(f"{who}: {m.get('text', '')}")
    text = "\n\n".join(lines)
    return text[-CONTEXT_CHARS:]


def build_spec(provider, chat, cfg, blocks, inline_texts=()):
    """Всё, что нужно для запроса к выбранной нейросети.
    blocks — блоки в формате Claude (text / image base64); inline_texts — содержимое
    прикреплённых текстовых файлов (для нейросетей, которые не читают файлы сами)."""
    text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text")
    images = [(b["source"]["media_type"], b["source"]["data"]) for b in blocks
              if b.get("type") == "image"]
    spec = {"provider": provider, "cfg": cfg, "effort": cfg.get("effort", ""),
            "text": text, "images": images, "blocks": blocks}
    hist = (chat or {}).get("history", [])

    if provider == "claude":
        spec["exe"] = find_claude(cfg.get("claude_path", ""))
        spec["session"] = chat.get("session_id")
        spec["model"] = cfg.get("model", "")
        if not spec["session"] and hist:  # чат начинали с другой нейросетью
            spec["blocks"] = [{"type": "text", "text": "Ранее в этом чате обсуждалось:\n\n"
                               + _transcript(chat)}] + blocks
        return spec

    if provider == "codex":
        c = pcfg(cfg, "codex")
        spec["exe"] = find_codex(c.get("path", ""))
        spec["model"] = c.get("model", "")
        spec["session"] = chat.get("codex_session")
        if not spec["session"]:
            pre = _instructions()
            if hist:
                pre += "\n\nРанее в этом чате обсуждалось:\n\n" + _transcript(chat)
            spec["text"] = (f"Постоянные инструкции:\n{pre}\n\n---\n\n" if pre else "") + text
        return spec

    # API-нейросети: историю хранит само приложение
    extra = "\n\n".join(inline_texts)
    if extra:
        spec["text"] = text + "\n\n" + extra
    api_hist = list(chat.get("api_history") or [])
    if not api_hist and hist:  # переносим прошлую переписку
        for m in hist[-API_HISTORY_LIMIT:]:
            if m.get("role") == "you":
                api_hist.append({"role": "user", "content": m.get("text", "")})
            elif m.get("role") == "claude":
                api_hist.append({"role": "assistant", "content": m.get("text", "")})
    spec["history"] = api_hist[-API_HISTORY_LIMIT:]
    spec["system"] = _instructions()
    if provider in API_PRESETS:
        p = API_PRESETS[provider]
        c = pcfg(cfg, provider)
        spec["base"] = (c.get("base") or p["base"]).rstrip("/")
        spec["model"] = c.get("model") or p["model"]
        spec["key"] = get_secret(provider)
    elif provider == "gigachat":
        c = pcfg(cfg, "gigachat")
        spec["model"] = c.get("model") or GIGACHAT["model"]
        spec["key"] = get_secret("gigachat")
        spec["scope"] = c.get("scope", "GIGACHAT_API_PERS")
        spec["ca_file"] = c.get("ca_file", "")
    return spec


def finish_chat(cid, spec, shown, text, session_id, clear_session):
    """Сохранить ответ в чат: видимую переписку + «память» нужной нейросети."""
    from core import update_chat
    provider = spec["provider"]
    user_note = shown if shown else spec.get("text", "")[:2000]

    def fn(c):
        c.setdefault("history", []).extend([{"role": "you", "text": shown},
                                            {"role": "claude", "text": text, "by": provider}])
        c["provider"] = provider
        if provider == "claude":
            if clear_session:
                c["session_id"] = None
            elif session_id:
                c["session_id"] = session_id
        elif provider == "codex":
            if clear_session:
                c["codex_session"] = None
            elif session_id:
                c["codex_session"] = session_id
        elif not text.startswith(("⚠", "⏹")):
            h = list(spec.get("history") or [])
            utext = spec.get("text", "") or user_note
            if spec.get("images"):
                utext += "\n[к сообщению был приложен скриншот]"
            h += [{"role": "user", "content": utext}, {"role": "assistant", "content": text}]
            c["api_history"] = h[-API_HISTORY_LIMIT:]
        if c.get("title") == "Новый чат":
            t = (shown or "").strip()
            if t and t[0] not in "📷📎":
                c["title"] = t.splitlines()[0][:40]
    return update_chat(cid, fn)


def explain_error(provider, text):
    """(сообщение, сбросить_сессию)"""
    if provider == "claude":
        return resolve_error(text)
    t = (text or "").strip()
    low = t.lower()
    if t.startswith("⚠"):
        return t, False
    if provider == "codex":
        if "not logged in" in low or "login" in low or "unauthorized" in low or "401" in low:
            return ("⚠ Не выполнен вход в ChatGPT: «Управление» → ⚙ Настройки → Нейросети → "
                    "ChatGPT → «Войти».\n\n" + t), False
        if ("session" in low or "rollout" in low or "thread" in low) and "not found" in low:
            return "⚠ Старая сессия ChatGPT не найдена — чат продолжится заново. Повтори запрос.", True
        if "limit" in low or "usage" in low:
            return "⚠ Похоже, исчерпан лимит ChatGPT на текущее окно.\n\n" + t, False
    return "⚠ " + (t or "Неизвестная ошибка"), False


# ----------------------------------------------------------------------------
# Единый исполнитель запросов
# ----------------------------------------------------------------------------
class Runner(ClaudeRunner):
    """Запуск запроса к любой нейросети. События: см. описание модуля."""

    def __init__(self, post):
        super().__init__(post)
        self._resp = None
        self._tmp = []

    def start_spec(self, spec):
        self.req_id += 1
        self.cancelled = False
        rid = self.req_id
        p = spec["provider"]
        if p == "claude":
            target, args = self._run, (rid, spec["exe"], spec["blocks"], spec.get("session"),
                                       spec.get("model", ""), spec.get("effort", ""))
        elif p == "codex":
            target, args = self._run_codex, (rid, spec)
        elif p == "gigachat":
            target, args = self._run_giga, (rid, spec)
        else:
            target, args = self._run_api, (rid, spec)
        threading.Thread(target=target, args=args, daemon=True).start()
        return rid

    def stop(self):
        super().stop()
        r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    # --- Codex CLI -------------------------------------------------------------
    def _run_codex(self, rid, spec):
        post = lambda *ev: self.post((ev[0], rid) + ev[1:])
        exe = spec.get("exe")
        if not exe:
            post("fail", "⚠ Codex не установлен: ⚙ Настройки → Нейросети → ChatGPT → «Установить».")
            return
        shots = WORK / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        for old in sorted(shots.glob("codex_*"))[:-20]:  # не копим старые снимки
            try:
                old.unlink()
            except OSError:
                pass
        paths = []
        for i, (mt, data) in enumerate(spec.get("images", [])):
            p = shots / f"codex_{int(time.time() * 1000)}_{i}.{'png' if 'png' in mt else 'jpg'}"
            p.write_bytes(base64.b64decode(data))
            paths.append(str(p))
        args = [exe, "exec", "--json", "--skip-git-repo-check", "-s", "read-only"]
        if spec.get("model"):
            args += ["-m", spec["model"]]
        eff = CODEX_EFFORT.get(spec.get("effort") or "")
        if eff:
            args += ["-c", f"model_reasoning_effort={eff}"]
        for p in paths:
            args += ["-i", p]
        if spec.get("session"):
            args += ["resume", spec["session"]]  # текст запроса — через stdin
        else:
            args += ["-"]
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)  # чтобы шла подписка ChatGPT, а не оплата по API
        try:
            self.proc = proc = subprocess.Popen(
                args, cwd=str(WORK), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except Exception as e:
            post("fail", f"⚠ Не удалось запустить Codex: {e}")
            return
        payload = spec.get("text", "").encode("utf-8")

        def feed():
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except Exception:
                pass
        err = []
        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()
        sid, answer, failed = None, None, None
        post("status", "думаю…")
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            item = ev.get("item") or {}
            itype = item.get("type") or item.get("item_type")
            if t == "thread.started" and ev.get("thread_id"):
                sid = ev["thread_id"]
                post("session", sid)
            elif t == "item.started" and itype == "command_execution":
                post("status", "читаю файл…")
            elif t == "item.completed" and itype in ("agent_message", "assistant_message"):
                answer = item.get("text") or ""
                post("reset")
                post("delta", answer)
            elif t == "item.completed" and itype == "reasoning":
                post("status", "думаю…")
            elif t == "turn.failed":
                failed = ((ev.get("error") or {}).get("message")) or "Codex: ошибка"
            elif t == "error":
                failed = ev.get("message") or str(ev)
        proc.wait()
        stderr = b"".join(x for x in err if x).decode("utf-8", "replace").strip()
        if self.cancelled:
            post("done", "⏹ Остановлено.", None, True)
        elif failed and not answer:
            post("done", failed + ("\n" + stderr[-500:] if stderr else ""), sid, True)
        elif answer is None:
            post("fail", stderr or f"Codex завершился без ответа (код {proc.returncode}).")
        else:
            post("done", answer, sid, False)

    # --- OpenAI-совместимый API ---------------------------------------------------
    def _api_messages(self, spec, image_content):
        msgs = []
        if spec.get("system"):
            msgs.append({"role": "system", "content": spec["system"]})
        msgs += spec.get("history", [])
        msgs.append({"role": "user", "content": image_content})
        return msgs

    def _stream(self, rid, req, cafile=""):
        """Читает SSE-поток OpenAI-формата, шлёт delta. Возвращает текст ответа."""
        post = lambda *ev: self.post((ev[0], rid) + ev[1:])
        text = ""
        with urllib.request.urlopen(req, timeout=300, context=_ssl_ctx(cafile)) as resp:
            self._resp = resp
            post("status", "пишу…")
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if ev.get("error"):
                    e = ev["error"]
                    raise RuntimeError(e.get("message") if isinstance(e, dict) else str(e))
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        text += piece
                        post("delta", piece)
        self._resp = None
        return text

    def _run_api(self, rid, spec):
        post = lambda *ev: self.post((ev[0], rid) + ev[1:])
        p = spec["provider"]
        content = [{"type": "text", "text": spec.get("text", "")}]
        for mt, data in spec.get("images", []):
            content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}})
        body = {"model": spec["model"], "stream": True,
                "messages": self._api_messages(spec, content)}
        eff = spec.get("effort") or ""
        mode = API_PRESETS.get(p, {}).get("effort")
        if eff and mode:
            level = {"xhigh": "high", "max": "high"}.get(eff, eff)
            if mode == "reasoning_effort":
                body["reasoning_effort"] = level
            elif mode == "openrouter":
                body["reasoning"] = {"effort": level}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if spec.get("key"):
            headers["Authorization"] = f"Bearer {spec['key']}"
        elif not API_PRESETS.get(p, {}).get("no_key"):
            post("fail", f"⚠ Нет ключа {PROVIDER_SHORT[p]}: ⚙ Настройки → Нейросети.")
            return
        if p == "openrouter":
            headers["X-Title"] = "Claude Overlay"
        req = urllib.request.Request(spec["base"] + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=headers)
        post("status", "думаю…")
        try:
            text = self._stream(rid, req)
        except Exception as e:
            if self.cancelled:
                post("done", "⏹ Остановлено.", None, True)
            else:
                post("fail", explain_http_error(p, e))
            return
        if self.cancelled:
            post("done", "⏹ Остановлено.", None, True)
        elif not text.strip():
            post("fail", f"⚠ {PROVIDER_SHORT[p]} вернул пустой ответ. Возможно, модель не "
                         "понимает картинки — выбери другую в ⚙ Настройки → Нейросети.")
        else:
            post("done", text, None, False)

    # --- GigaChat -------------------------------------------------------------------
    def _run_giga(self, rid, spec):
        post = lambda *ev: self.post((ev[0], rid) + ev[1:])
        if not spec.get("key"):
            post("fail", "⚠ Нет ключа GigaChat: ⚙ Настройки → Нейросети → GigaChat.")
            return
        ca = spec.get("ca_file", "")
        try:
            post("status", "вхожу в GigaChat…")
            tok = giga_token(spec["key"], spec.get("scope", "GIGACHAT_API_PERS"), ca)
            attach = []
            if spec.get("images"):  # GigaChat принимает одно изображение на сообщение
                post("status", "загружаю скриншот…")
                mt, data = spec["images"][-1]
                attach = [giga_upload(tok, base64.b64decode(data), mt, ca)]
            msgs = []
            if spec.get("system"):
                msgs.append({"role": "system", "content": spec["system"]})
            msgs += spec.get("history", [])
            user = {"role": "user", "content": spec.get("text", "")}
            if attach:
                user["attachments"] = attach
            msgs.append(user)
            body = {"model": spec["model"], "messages": msgs, "stream": True}
            req = urllib.request.Request(GIGACHAT["base"] + "/chat/completions",
                                         data=json.dumps(body).encode("utf-8"), headers={
                                             "Content-Type": "application/json",
                                             "Accept": "text/event-stream",
                                             "Authorization": f"Bearer {tok}"})
            post("status", "думаю…")
            text = self._stream(rid, req, ca)
        except Exception as e:
            if self.cancelled:
                post("done", "⏹ Остановлено.", None, True)
            else:
                post("fail", explain_http_error("gigachat", e))
            return
        if self.cancelled:
            post("done", "⏹ Остановлено.", None, True)
        elif not text.strip():
            post("fail", "⚠ GigaChat вернул пустой ответ.")
        else:
            post("done", text, None, False)


def test_provider(provider, cfg):
    """Короткий проверочный запрос. Возвращает текст ответа или ошибку."""
    import queue as _q
    q = _q.Queue()
    r = Runner(q.put)
    spec = build_spec(provider, {"history": []}, cfg,
                      [{"type": "text", "text": "Ответь одним словом: работает"}])
    spec["system"] = ""
    r.start_spec(spec)
    text, deadline = "", time.time() + 120
    while time.time() < deadline:
        try:
            ev = q.get(timeout=1)
        except _q.Empty:
            continue
        if ev[0] == "delta":
            text += ev[2]
        elif ev[0] == "reset":
            text = ""
        elif ev[0] == "done":
            return ("✔ " + (ev[2] or text).strip()[:200]) if not ev[4] else explain_error(provider, ev[2])[0]
        elif ev[0] == "fail":
            return ev[2]
    r.stop()
    return "⚠ Нет ответа за 2 минуты."
