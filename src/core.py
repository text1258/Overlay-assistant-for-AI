#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общее ядро «ИИ-помощника поверх экрана»: настройки, работа с Claude Code (по подписке),
горячие клавиши, панель-оверлей, хранилище чатов и связь между двумя программами:

• manager.py — «Управление»: чаты, загрузка контекста, файлы, настройки;
• overlay.py — оверлей: горячие клавиши, скриншоты, ответ в панели поверх экрана.
"""
import base64
import ctypes
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import mss
from PIL import Image, ImageEnhance, ImageTk

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
SRC_DIR = Path(__file__).resolve().parent      # код программы (папка src)
APP_DIR = SRC_DIR.parent                        # главная папка: запускалки и данные
WORK = APP_DIR / "workspace"
FILES = WORK / "files"
CONFIG_PATH = APP_DIR / "config.json"
DATA = APP_DIR / "data"
CHATS = DATA / "chats"
RUNTIME = DATA / "runtime"
STATE_PATH = DATA / "state.json"
OLD_STATE = APP_DIR / "state.json"  # от первой версии — переносится автоматически

# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "claude_path": "",          # пусто = искать `claude` автоматически
    "model": "",                # "", "sonnet", "opus", "haiku", "fable"
    "effort": "",               # "", low, medium, high, xhigh, max — усилия модели
    "screenshot_prompt": (
        "Это скриншот моего экрана. Помоги с тем, что на нём, "
        "учитывая всё, о чём мы договорились раньше."
    ),
    "max_image_side": 1600,     # px, длинная сторона скриншота
    "math": "latex",            # latex — формулы картинками, plain — простым текстом
    "providers": {               # нейросети (см. providers.py); ключи — в data/secrets.json
        "default": "claude",
        "claude": {"mode": "subscription"},          # subscription / token / api
        "codex": {"path": "", "model": "", "effort": "", "mode": "subscription",
                  "api_model": "gpt-5-mini", "api_effort": ""},
        "gemini": {"model": "gemini-flash-latest", "effort": ""},
        "grok": {"model": "grok-4"},
        "openrouter": {"model": "openrouter/free", "effort": ""},
        "ollama": {"base": "http://localhost:11434/v1", "model": "qwen2.5vl:7b"},
        "gigachat": {"model": "GigaChat-2-Max", "scope": "GIGACHAT_API_PERS", "ca_file": ""},
    },
    "overlay": {
        "width": 460,
        "max_height": 420,
        "opacity": 0.85,
        "font_size": 11,
        "theme": "dark",           # dark / light / system
        "monitor": "cursor",       # cursor / primary / 1, 2, … (номер монитора)
        "anchor": "top-right",     # top-left, top, top-right, left, center, right, bottom-…
        "offset_x": 24,            # отступ от края по горизонтали, px
        "offset_y": 24,            # отступ от края по вертикали, px
        "respect_taskbar": True,   # не заходить на панель задач
        "display_mode": "panel",   # panel — текстом в панели, screen — пометками по экрану
        "marks_seconds": 0,        # через сколько секунд убирать пометки (0 — по клавише)
        "marks_opacity": 0.9,      # непрозрачность рамок и подписей пометок
        "auto_hide_seconds": 0,    # 0 = не прятать автоматически
    },
    "hotkeys": {
        "shot": "ctrl+alt+s",          # скрин всего экрана → Claude
        "region": "ctrl+alt+a",        # выделить область → Claude
        "toggle": "ctrl+alt+h",        # скрыть / показать панель
        "scroll_up": "ctrl+alt+pageup",
        "scroll_down": "ctrl+alt+pagedown",
        "move": "ctrl+alt+m",          # режим перемещения панели
        "chat": "ctrl+alt+c",          # открыть «Управление»
        "stop": "ctrl+alt+x",          # остановить ответ
        "quit": "ctrl+alt+q",          # закрыть оверлей
    },
}

DEFAULT_CLAUDE_MD = """# Экранный помощник

Ты работаешь как помощник поверх экрана. Пользователь заранее обсуждает с тобой
задачу и присылает файлы в окне «Управление», а потом присылает скриншоты своего
экрана по горячей клавише. Ответ показывается в небольшой
полупрозрачной панели в углу экрана, поэтому:

- Отвечай по-русски, коротко и по делу: сначала сам ответ, потом, если нужно, пояснение.
- Не используй Markdown-таблицы и заголовки; простые списки можно.
- Формулы оформляй в LaTeX ($...$ в строке, $$...$$ отдельной строкой) — приложение рисует их.
- Если на скриншоте несколько вопросов или задач — ответь на каждый, пронумеровав.
- Учитывай всё, что обсуждалось в этом диалоге раньше: договорённости, присланные файлы.
- Прикреплённые файлы лежат в папке files/ — читай их инструментом Read.

(Этот файл можно редактировать. Изменения действуют с нового диалога.)
"""


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Вспомогательное
# ----------------------------------------------------------------------------
def clean_md(s: str, strip=True) -> str:
    """Лёгкая чистка Markdown для показа в простом окне."""
    s = re.sub(r"^\s*```.*$\n?", "", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"\1", s, flags=re.S)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s, flags=re.M)
    return s.strip("\n") if strip else s


def image_to_block(img: Image.Image, max_side: int) -> dict:
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data, mt = buf.getvalue(), "image/png"
    if len(data) > 3_500_000:  # лимит API на картинку ~5 МБ
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        data, mt = buf.getvalue(), "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mt,
                   "data": base64.b64encode(data).decode("ascii")},
    }


def docx_to_text(path: Path) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(ns + "p"):
        parts = []
        for node in p.iter():
            if node.tag == ns + "t":
                parts.append(node.text or "")
            elif node.tag == ns + "tab":
                parts.append("\t")
            elif node.tag in (ns + "br", ns + "cr"):
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras)


def pdf_to_text(path):
    """Текст из PDF (нужна библиотека pypdf; без неё — пустая строка)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
    except Exception:
        return ""


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def copy_unique(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    i = 2
    while dest.exists():
        dest = folder / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.copy2(src, dest)
    return dest


def grab_screen_at(x, y):
    """Скриншот монитора, на котором сейчас курсор (x, y — в единицах Tk).
    Возвращает картинку в пикселях и монитор в единицах Tk."""
    s = _SCALE
    px, py = x * s, y * s
    with mss.mss() as sct:
        mons = sct.monitors[1:] or [sct.monitors[0]]
        mon = next((m for m in mons
                    if m["left"] <= px < m["left"] + m["width"]
                    and m["top"] <= py < m["top"] + m["height"]), mons[0])
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, _to_tk(mon)


# ----------------------------------------------------------------------------
# Глобальные горячие клавиши
# ----------------------------------------------------------------------------
_VK = {"pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
       "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
       "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B,
       "insert": 0x2D, "delete": 0x2E, "`": 0xC0, "-": 0xBD, "=": 0xBB,
       "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF}
_MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}


def parse_combo_win(combo: str):
    mods, vk = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _VK:
            vk = _VK[part]
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x70 + int(part[1:]) - 1
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())  # по физической клавише — работает и в русской раскладке
        else:
            raise ValueError(f"не понимаю клавишу «{part}» в «{combo}»")
    if vk is None:
        raise ValueError(f"нет основной клавиши в «{combo}»")
    return mods, vk


class WinHotkeys(threading.Thread):
    """RegisterHotKey: не зависит от раскладки, не требует прав администратора."""

    def __init__(self, bindings, post):
        super().__init__(daemon=True)
        self.bindings, self.post, self.tid = bindings, post, None

    def run(self):
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.tid = kernel32.GetCurrentThreadId()
        ids, failed = {}, []
        for i, (action, combo) in enumerate(self.bindings.items(), start=1):
            if not combo:
                continue  # сочетание отключено
            try:
                mods, vk = parse_combo_win(combo)
            except ValueError as e:
                failed.append(str(e))
                continue
            if user32.RegisterHotKey(None, i, mods | 0x4000, vk):  # 0x4000 = MOD_NOREPEAT
                ids[i] = action
            else:
                failed.append(f"{combo} (уже занято другой программой)")
        if failed:
            self.post(("hotkey_fail", failed))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in ids:  # WM_HOTKEY
                self.post(("hotkey", ids[msg.wParam]))
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        for _ in range(50):  # поток мог ещё не успеть стартовать
            if self.tid or not self.is_alive():
                break
            time.sleep(0.01)
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT
        self.join(1.5)  # дождаться UnregisterHotKey, иначе новые клавиши будут «заняты»


class PynputHotkeys:
    """Запасной вариант для macOS / Linux."""

    def __init__(self, bindings, post):
        self.listener = None
        try:
            from pynput import keyboard
        except ImportError:
            post(("hotkey_fail", ["модуль pynput не установлен"]))
            return
        names = {"ctrl": "<ctrl>", "control": "<ctrl>", "alt": "<alt>", "shift": "<shift>",
                 "win": "<cmd>", "pageup": "<page_up>", "pagedown": "<page_down>",
                 "space": "<space>", "enter": "<enter>", "esc": "<esc>", "up": "<up>",
                 "down": "<down>", "left": "<left>", "right": "<right>"}
        mapping = {}
        for action, combo in bindings.items():
            if not combo:
                continue
            parts = [names.get(p, f"<{p}>" if re.fullmatch(r"f\d+", p) else p)
                     for p in combo.lower().replace(" ", "").split("+")]
            mapping["+".join(parts)] = (lambda a=action: post(("hotkey", a)))
        try:
            self.listener = keyboard.GlobalHotKeys(mapping)
            self.listener.start()
        except Exception as e:
            post(("hotkey_fail", [str(e)]))

    def start(self):
        pass

    def stop(self):
        if self.listener:
            self.listener.stop()


ACTION_NAMES = [
    ("shot", "Скриншот всего экрана → Claude"),
    ("region", "Скриншот области → Claude"),
    ("toggle", "Скрыть / показать панель"),
    ("scroll_up", "Прокрутить ответ вверх"),
    ("scroll_down", "Прокрутить ответ вниз"),
    ("move", "Режим перемещения панели"),
    ("chat", "Открыть «Управление»"),
    ("stop", "Остановить ответ"),
    ("quit", "Закрыть оверлей"),
]

_PRETTY = {"pageup": "PgUp", "pagedown": "PgDn", "ctrl": "Ctrl", "alt": "Alt",
           "shift": "Shift", "win": "Win", "space": "Space", "enter": "Enter"}


def pretty_combo(combo: str) -> str:
    if not combo:
        return "— выкл —"
    return "+".join(_PRETTY.get(p, p.upper() if len(p) <= 3 else p.capitalize())
                    for p in combo.lower().replace(" ", "").split("+"))


_MOD_KEYSYMS = {"Control_L": "ctrl", "Control_R": "ctrl", "Alt_L": "alt", "Alt_R": "alt",
                "Shift_L": "shift", "Shift_R": "shift", "Win_L": "win", "Win_R": "win",
                "Super_L": "win", "Super_R": "win", "Meta_L": "alt", "Meta_R": "alt",
                "ISO_Level3_Shift": "alt", "App": None, "Caps_Lock": None, "Num_Lock": None}
_VK_NAMES = {v: k for k, v in _VK.items() if k != "esc"}
_KEYSYM_NAMES = {"Prior": "pageup", "Next": "pagedown", "Up": "up", "Down": "down",
                 "Left": "left", "Right": "right", "space": "space", "Return": "enter",
                 "Tab": "tab", "Home": "home", "End": "end", "Insert": "insert",
                 "Delete": "delete", "grave": "`", "minus": "-", "equal": "=",
                 "bracketleft": "[", "bracketright": "]", "semicolon": ";",
                 "apostrophe": "'", "comma": ",", "period": ".", "slash": "/"}


def key_name_from_event(e):
    """Имя основной клавиши по физической кнопке (не зависит от раскладки на Windows)."""
    if IS_WIN:
        vk = e.keycode
        if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
            return chr(vk).lower()
        if 0x70 <= vk <= 0x87:
            return f"f{vk - 0x6F}"
        return _VK_NAMES.get(vk)
    ks = e.keysym
    if ks in _KEYSYM_NAMES:
        return _KEYSYM_NAMES[ks]
    if re.fullmatch(r"F\d+", ks):
        return ks.lower()
    if len(ks) == 1 and ks.isascii() and ks.isalnum():
        return ks.lower()
    return None


def mods_from_state(state: int) -> set:
    mods = set()
    if state & 0x4:
        mods.add("ctrl")
    if state & 0x1:
        mods.add("shift")
    if (IS_WIN and state & 0x20000) or (not IS_WIN and state & 0x8):
        mods.add("alt")
    return mods


# ----------------------------------------------------------------------------
# Запуск Claude Code (по подписке)
# ----------------------------------------------------------------------------
NO_WINDOW = 0x08000000 if IS_WIN else 0


def claude_env():
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # чтобы точно шла подписка, а не API
    return env


def _find_value(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)) and str(v):
                return str(v)
        for v in obj.values():
            r = _find_value(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_value(v, keys)
            if r:
                return r
    return None


def claude_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=30,
                           env=claude_env(), creationflags=NO_WINDOW)
        return r.stdout.decode("utf-8", "replace").strip()[:40]
    except Exception:
        return ""


def auth_status(exe):
    """(вошёл ли, описание) — через `claude auth status`."""
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, timeout=40,
                           env=claude_env(), creationflags=NO_WINDOW)
    except Exception as e:
        return False, f"Не удалось проверить вход: {e}"
    out = r.stdout.decode("utf-8", "replace").strip()
    try:
        j = json.loads(out)
    except ValueError:
        j = None
    j = j if isinstance(j, dict) else {}
    method = j.get("authMethod")
    logged = r.returncode == 0 and method != "none" and j.get("loggedIn", True) is not False
    if not logged:
        return False, "Вход не выполнен."
    email = _find_value(j, {"email", "emailAddress", "accountEmail"})
    plan = _find_value(j, {"subscriptionType", "subscription", "plan"})
    parts = ["✔ Вход выполнен"]
    if email:
        parts.append(email)
    if plan:
        parts.append(f"подписка: {plan}")
    desc = " · ".join(parts)
    if method in ("api_key", "api_key_helper"):
        desc += ("\n⚠ Сейчас используется API-ключ, а не подписка. Нажми «Выйти» "
                 "и войди заново аккаунтом claude.ai.")
    elif method and method != "claude.ai":
        desc += f"\n(способ входа: {method})"
    return True, desc


def open_in_terminal(args):
    """Запустить команду в отдельном окне терминала (нужно для входа и установки)."""
    if IS_WIN:
        return subprocess.Popen(args, env=claude_env(), creationflags=subprocess.CREATE_NEW_CONSOLE)
    cmd = " ".join(shlex.quote(a) for a in args)
    if sys.platform == "darwin":
        script = cmd.replace("\\", "\\\\").replace('"', '\\"')
        return subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                                 "-e", 'tell application "Terminal" to activate'])
    for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"],
                 ["konsole", "-e"], ["xterm", "-e"]):
        if shutil.which(term[0]):
            return subprocess.Popen(term + ["bash", "-c", cmd + "; read -p 'Enter — закрыть '"],
                                    env=claude_env())
    return subprocess.Popen(args, env=claude_env())


def install_command():
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit",
                "-Command", "irm https://claude.ai/install.ps1 | iex"]
    return ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]


def find_claude(cfg_path: str):
    if cfg_path and Path(cfg_path).exists():
        return cfg_path
    p = shutil.which("claude")
    if p:
        return p
    home = Path.home()
    for cand in (home / ".local" / "bin" / ("claude.exe" if IS_WIN else "claude"),
                 home / ".claude" / "local" / ("claude.exe" if IS_WIN else "claude"),
                 Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"):
        if cand.exists():
            return str(cand)
    return None


class ClaudeRunner:
    def __init__(self, post):
        self.post = post
        self.proc = None
        self.req_id = 0
        self.cancelled = False

    def start(self, exe, blocks, session_id, model, effort="", env_extra=None):
        self.req_id += 1
        self.cancelled = False
        rid = self.req_id
        threading.Thread(target=self._run,
                         args=(rid, exe, blocks, session_id, model, effort, env_extra),
                         daemon=True).start()
        return rid

    def stop(self):
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                                   capture_output=True, creationflags=0x08000000)
                else:
                    p.terminate()
            except Exception:
                pass

    def _run(self, rid, exe, blocks, session_id, model, effort="", env_extra=None):
        post = lambda *ev: self.post((ev[0], rid) + ev[1:])
        args = [exe, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages"]
        if session_id:
            args += ["--resume", session_id]
        if model:
            args += ["--model", model]
        # Только чтение файлов: никаких команд, правок и интернета
        args += ["--tools", "Read", "--allowedTools", "Read", "--disallowedTools", "mcp__*"]

        env = claude_env()
        env.update(env_extra or {})  # токен подписки или API-ключ, если выбран такой способ
        if effort:  # переменная, а не флаг: старые версии Claude Code её просто проигнорируют
            env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        msg = {"type": "user", "message": {"role": "user", "content": blocks},
               "parent_tool_use_id": None}
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")

        try:
            self.proc = proc = subprocess.Popen(
                args, cwd=str(WORK), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=0x08000000 if IS_WIN else 0)
        except Exception as e:
            post("fail", f"Не удалось запустить Claude Code: {e}")
            return

        def feed():
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except Exception:
                pass

        err_chunks = []
        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True).start()

        result = None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                post("session", ev["session_id"])
            elif t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "message_start":
                    post("msg_start")
                elif et == "content_block_start":
                    cb = e.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        post("status", "читаю файл…" if cb.get("name") == "Read"
                             else f"инструмент {cb.get('name')}…")
                    elif cb.get("type") == "thinking":
                        post("status", "думаю…")
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta":
                        post("delta", d.get("text", ""))
            elif t == "system" and ev.get("subtype") == "api_retry":
                post("status", f"повтор запроса ({ev.get('error', '')})…")
            elif t == "result":
                result = ev
        proc.wait()
        time.sleep(0.05)
        stderr = b"".join(c for c in err_chunks if c).decode("utf-8", "replace").strip()

        if self.cancelled:
            post("done", "⏹ Остановлено.", None, True)
        elif result is None:
            post("fail", stderr or f"Claude Code завершился без ответа (код {proc.returncode}).")
        else:
            text = result.get("result")
            if not text and result.get("errors"):
                text = "\n".join(map(str, result["errors"]))
            post("done", text or "", result.get("session_id"), bool(result.get("is_error")))


# ----------------------------------------------------------------------------
# Панель поверх экрана
# ----------------------------------------------------------------------------
OVERLAY_THEMES = {
    "dark": {"bg": "#16181d", "fg": "#ececec", "dim": "#8b8f98", "accent": "#d97757",
             "edge": "#2c3038", "sel": "#3a3f4a"},
    "light": {"bg": "#f8f7f4", "fg": "#1d1d1f", "dim": "#76716a", "accent": "#c15f3c",
              "edge": "#d3cdc3", "sel": "#eadfd2"},
}
THEME_LABELS = [("dark", "Тёмная"), ("light", "Светлая"), ("system", "Как в системе")]

ANCHORS = [  # (значение, стрелка на схеме, подпись) — по строкам сетки 3×3
    ("top-left", "↖", "слева сверху"), ("top", "↑", "сверху по центру"),
    ("top-right", "↗", "справа сверху"),
    ("left", "←", "слева по центру"), ("center", "•", "по центру экрана"),
    ("right", "→", "справа по центру"),
    ("bottom-left", "↙", "слева снизу"), ("bottom", "↓", "снизу по центру"),
    ("bottom-right", "↘", "справа снизу"),
]


def system_theme():
    """'light' или 'dark' — тема приложений в системе."""
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, timeout=3)
            return "dark" if b"Dark" in r.stdout else "light"
        if IS_LINUX and shutil.which("gsettings"):
            r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                               capture_output=True, timeout=3)
            out = r.stdout.decode("utf-8", "replace")
            if "dark" in out:
                return "dark"
            if "light" in out or "default" in out:
                return "light"
    except Exception:
        pass
    return "dark"


def theme_colors(name):
    if name == "system":
        name = system_theme()
    return OVERLAY_THEMES.get(name, OVERLAY_THEMES["dark"])


def list_monitors():
    """Мониторы в единицах Tk (на Windows/Linux — пиксели): [{'left','top','width','height'}, …]."""
    try:
        with mss.mss() as sct:
            mons = [_to_tk(m) for m in sct.monitors[1:]]
            return mons or [_to_tk(sct.monitors[0])]
    except Exception:
        return [{"left": 0, "top": 0, "width": 1920, "height": 1080}]


def primary_index(mons):
    for i, m in enumerate(mons):
        if m["left"] <= 0 < m["left"] + m["width"] and m["top"] <= 0 < m["top"] + m["height"]:
            return i
    return 0


def monitor_choices():
    """[(значение, подпись)] для выбора монитора в настройках."""
    mons = list_monitors()
    prim = primary_index(mons)
    out = [("cursor", "Тот, где курсор"), ("primary", "Основной")]
    for i, m in enumerate(mons):
        extra = " (основной)" if i == prim else ""
        out.append((str(i + 1), f"Монитор {i + 1} — {m['width']}×{m['height']}{extra}"))
    return out


def work_area(mon):
    """Часть монитора без панели задач / панелей рабочего стола / меню-бара и Дока."""
    if IS_LINUX:
        return _linux_workarea(mon)
    if IS_MAC:
        return _mac_workarea(mon)
    if not IS_WIN:
        return dict(mon)
    try:
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        u = ctypes.windll.user32
        u.MonitorFromPoint.restype = ctypes.c_void_p
        u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        pt = wintypes.POINT(mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
        hmon = u.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            return {"left": r.left, "top": r.top, "width": r.right - r.left,
                    "height": r.bottom - r.top}
    except Exception:
        pass
    return dict(mon)


def pick_monitor(choice, pointer=None):
    mons = list_monitors()
    if choice == "cursor" and pointer:
        x, y = pointer
        for m in mons:
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return m
    if str(choice).isdigit() and 1 <= int(choice) <= len(mons):
        return mons[int(choice) - 1]
    return mons[primary_index(mons)]


def anchor_position(area, w, h, anchor, ox, oy):
    """Левый верхний угол панели w×h для привязки к области area."""
    L, T, W, H = area["left"], area["top"], area["width"], area["height"]
    if anchor in ("top-left", "left", "bottom-left"):
        x = L + ox
    elif anchor in ("top", "center", "bottom"):
        x = L + (W - w) // 2 + ox
    else:
        x = L + W - w - ox
    if anchor in ("top-left", "top", "top-right"):
        y = T + oy
    elif anchor in ("left", "center", "right"):
        y = T + (H - h) // 2 + oy
    else:
        y = T + H - h - oy
    return int(x), int(y)


class Overlay:

    def __init__(self, root, cfg, state, on_moved):
        self.root, self.state, self.on_moved = root, state, on_moved
        self.full, self.cfg = cfg, cfg["overlay"]
        self.visible = False
        self.move_mode = False
        self._hide_job = None
        self._fit_job = None

        fam = "Segoe UI" if IS_WIN else "Helvetica"
        self.font = tkfont.Font(family=fam, size=self.cfg["font_size"])
        self.small = tkfont.Font(family=fam, size=max(8, self.cfg["font_size"] - 2))

        self._area = None

        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.title(f"claude-overlay-panel-{id(w)}")  # для поиска окна на macOS
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-alpha", self.cfg["opacity"])

        self.border = tk.Frame(w, padx=2, pady=2)
        self.border.pack(fill="both", expand=True)
        self.inner = inner = tk.Frame(self.border)
        inner.pack(fill="both", expand=True)

        head = tk.Frame(inner)
        head.pack(fill="x", padx=10, pady=(6, 0))
        self.who = "Claude"  # подпись: какая нейросеть отвечает
        self.status = tk.Label(head, text=self.who, font=self.small, anchor="w")
        self.status.pack(side="left")
        self.hint = tk.Label(head, text="", font=self.small, anchor="e")
        self.hint.pack(side="right")
        self.head = head

        self.text = tk.Text(inner, wrap="word", bd=0, highlightthickness=0, padx=10, pady=6,
                            font=self.font, cursor="arrow", insertwidth=0)
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")
        self._apply_theme()

        for wd in (head, self.status, self.hint, self.text):
            wd.bind("<ButtonPress-1>", self._drag_start)
            wd.bind("<B1-Motion>", self._drag_move)
            wd.bind("<ButtonRelease-1>", self._drag_end)
        self._keep_on_top()

    def apply_config(self, cfg):
        self.full, self.cfg = cfg, cfg["overlay"]
        self.font.configure(size=self.cfg["font_size"])
        self.small.configure(size=max(8, self.cfg["font_size"] - 2))
        self.win.attributes("-alpha", self.cfg["opacity"])
        self._apply_theme()
        self._area = None
        self._schedule_fit()

    def _apply_theme(self):
        c = self.colors = theme_colors(self.cfg.get("theme", "dark"))
        self.win.configure(bg=c["bg"])
        self.border.configure(bg=c["accent"] if self.move_mode else c["edge"])
        for wd in (self.inner, self.head):
            wd.configure(bg=c["bg"])
        self.status.configure(bg=c["bg"], fg=c["accent"])
        self.hint.configure(bg=c["bg"], fg=c["dim"])
        self.text.configure(bg=c["bg"], fg=c["fg"], selectbackground=c["sel"],
                            selectforeground=c["fg"], inactiveselectbackground=c["sel"])

    # --- видимость -------------------------------------------------------
    def show(self):
        if not self.visible:
            self._area = None  # монитор выбирается заново при каждом появлении
            if self.cfg.get("theme") == "system":
                self._apply_theme()
            self.win.deiconify()
            self.visible = True
            self.win.update_idletasks()
            self._apply_click_through()
        self._fit()

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False

    def toggle(self):
        self.hide() if self.visible else self.show()

    def _keep_on_top(self):
        if self.visible:
            try:
                self.win.attributes("-topmost", True)
            except tk.TclError:
                pass
        self.root.after(3000, self._keep_on_top)

    def _apply_click_through(self):
        make_click_through(self.win, enable=not self.move_mode)

    # --- содержимое ------------------------------------------------------
    def set_who(self, name):
        if name and name != self.who:
            self.who = name
            self.status.configure(text=name)

    def set_status(self, s):
        self.status.configure(text=f"{self.who} · {s}" if s else self.who)

    def set_text(self, s, scroll_end=False):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        insert_rich(self.text, (s or "").strip("\n"), fg=self.colors["fg"], bg=self.colors["bg"],
                    size_pt=self.cfg["font_size"], enabled=self.full.get("math", "latex") == "latex")
        self.text.configure(state="disabled")
        if scroll_end:
            self.text.see("end")
        else:
            self.text.yview_moveto(0)
        self._schedule_fit()

    def scroll(self, direction):
        self.text.yview_scroll(direction * 3, "units")

    def schedule_hide(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        sec = self.cfg.get("auto_hide_seconds") or 0
        if sec > 0:
            self._hide_job = self.root.after(int(sec * 1000), self.hide)

    def hide_after(self, seconds):
        self.cancel_hide()
        self._hide_job = self.root.after(int(seconds * 1000), self.hide)

    def flash(self, text, seconds=6):
        """Короткое служебное сообщение в панели."""
        self.show()
        self.set_status("")
        self.set_text(text)
        self.hide_after(seconds)

    def cancel_hide(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None

    # --- размер и положение ---------------------------------------------
    def _schedule_fit(self):
        if not self._fit_job:
            self._fit_job = self.root.after(30, self._fit)

    def _fit(self):
        self._fit_job = None
        if not self.visible:
            return
        width, max_h = self.cfg["width"], self.cfg["max_height"]
        head_h = self.head.winfo_reqheight() + 6
        x, y = self._position(width, max_h)
        self.win.geometry(f"{width}x{max_h}+{x}+{y}")
        self.win.update_idletasks()
        has_text = bool(self.text.get("1.0", "end-1c").strip()) or bool(self.text.image_names())
        try:  # высота в пикселях — учитывает и картинки-формулы
            r = self.text.count("1.0", "end", "ypixels")
            px = r[0] if isinstance(r, tuple) else (r or 0)
            body = px + 12 + 6 if has_text else 4
        except Exception:
            try:
                r = self.text.count("1.0", "end-1c", "displaylines")
                n = r[0] if isinstance(r, tuple) else (r or 0)
            except Exception:
                n = int(self.text.index("end-1c").split(".")[0])
            body = (n + 1) * self.font.metrics("linespace") + 14 if has_text else 4
        h = max(head_h + 8, min(max_h, head_h + body + 4))
        x, y = self._position(width, h)
        self.win.geometry(f"{width}x{h}+{x}+{y}")
        hk = self.full["hotkeys"]
        scroll = f"{pretty_combo(hk.get('scroll_up'))} / {pretty_combo(hk.get('scroll_down'))} — прокрутка"
        self.hint.configure(text=scroll if body + head_h > max_h else "")

    def _position(self, w, h):
        pos = self.state.get("overlay_pos")
        if pos:
            return int(pos[0]), int(pos[1])
        if self._area is None:
            try:
                pointer = self.root.winfo_pointerxy()
            except tk.TclError:
                pointer = None
            mon = pick_monitor(self.cfg.get("monitor", "cursor"), pointer)
            self._area = work_area(mon) if self.cfg.get("respect_taskbar", True) else mon
        return anchor_position(self._area, w, h, self.cfg.get("anchor", "top-right"),
                               int(self.cfg.get("offset_x", 24)), int(self.cfg.get("offset_y", 24)))

    # --- перемещение -----------------------------------------------------
    def toggle_move(self):
        self.move_mode = not self.move_mode
        self.border.configure(bg=self.colors["accent"] if self.move_mode else self.colors["edge"])
        if self.move_mode:
            self.show()
            self.set_status("перемещение: тащи мышью, колесо — прокрутка, "
                            f"{pretty_combo(self.full['hotkeys'].get('move'))} — закрепить")
        else:
            self.set_status("")
        self._apply_click_through()

    def _drag_start(self, e):
        if not self.move_mode:
            return
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()
        return "break"

    def _drag_move(self, e):
        if not self.move_mode:
            return
        x, y = e.x_root - self._dx, e.y_root - self._dy
        self.win.geometry(f"+{x}+{y}")
        self.state["overlay_pos"] = [x, y]
        return "break"

    def _drag_end(self, e):
        if self.move_mode:
            self.on_moved()
            return "break"


# ----------------------------------------------------------------------------
# Выделение области
# ----------------------------------------------------------------------------
class RegionSelector:
    def __init__(self, root, img, mon, on_done, on_cancel):
        self.img, self.on_done, self.on_cancel = img, on_done, on_cancel
        t = self.top = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
        self.k = img.width / max(1, mon["width"])  # пикселей снимка на единицу Tk
        shown = img if abs(self.k - 1) < 0.01 else img.resize((mon["width"], mon["height"]))
        dark = ImageEnhance.Brightness(shown).enhance(0.55)
        self.photo = ImageTk.PhotoImage(dark)
        self.bright = img
        c = self.canvas = tk.Canvas(t, highlightthickness=0, cursor="crosshair",
                                    width=mon["width"], height=mon["height"])
        c.pack(fill="both", expand=True)
        c.create_image(0, 0, image=self.photo, anchor="nw")
        c.create_text(mon["width"] // 2, 30, fill="white", font=("Segoe UI", 14),
                      text="Выдели область мышью · Esc — отмена")
        self.rect = None
        self.start = None
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        t.bind("<Escape>", lambda e: self._close(None))
        t.bind("<ButtonPress-3>", lambda e: self._close(None))
        t.after(50, lambda: (t.focus_force(), t.grab_set()))

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#d97757", width=2)

    def _drag(self, e):
        if self.start:
            self.canvas.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            self._close(None)
            return
        k = self.k
        box = tuple(int(round(v * k)) for v in box)  # в пиксели снимка
        crop = self.img.crop(box)
        crop.info["origin"] = (box[0], box[1])  # где область лежит на мониторе, в пикселях
        self._close(crop)

    def _close(self, crop):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()
        (self.on_done(crop) if crop is not None else self.on_cancel())


MODEL_LABEL_DEFAULT = "по умолчанию"
EFFORTS = [("", "по умолчанию (как у модели)"), ("low", "низкие — быстрее всего"),
           ("medium", "средние"), ("high", "высокие"), ("xhigh", "очень высокие"),
           ("max", "максимальные — дольше всего")]
MODELS = [MODEL_LABEL_DEFAULT, "sonnet", "opus", "haiku", "fable"]


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не открыть {path}: {e}")


# ----------------------------------------------------------------------------
# Общие данные двух программ: чаты, состояние, блокировки, команды
# ----------------------------------------------------------------------------
MANAGER_TITLE = "AI Overlay — управление"
DEFAULT_TITLE = "Новый чат"


def ensure_dirs():
    for d in (WORK, FILES, DATA, CHATS, RUNTIME):
        d.mkdir(parents=True, exist_ok=True)
    md = WORK / "CLAUDE.md"
    if not md.exists():
        md.write_text(DEFAULT_CLAUDE_MD, encoding="utf-8")
    else:  # старая строка «не LaTeX» из прошлых версий → новая
        old = '- Формулы пиши простым Unicode-текстом (x², √x, ∫, ≤, π, →), не LaTeX — панель его не рисует.\n'
        txt = md.read_text(encoding="utf-8")
        if old in txt:
            md.write_text(txt.replace(old, '- Формулы оформляй в LaTeX ($...$ в строке, $$...$$ отдельной строкой) — приложение рисует их.\n'), encoding="utf-8")


def load_config():
    raw = load_json(CONFIG_PATH, {})
    raw = raw if isinstance(raw, dict) else {}
    ov = raw.get("overlay")
    if isinstance(ov, dict):  # перенос настроек положения из прошлых версий
        if "anchor" not in ov and ov.get("corner"):
            ov["anchor"] = ov["corner"]
        if "offset_x" not in ov and "margin" in ov:
            ov["offset_x"] = ov["offset_y"] = ov["margin"]
        ov.pop("corner", None)
        ov.pop("margin", None)
    return deep_merge(DEFAULT_CONFIG, raw)


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def set_dpi_aware():
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


class FileLock:
    """Короткая межпроцессная блокировка на время чтения-изменения-записи файла."""

    def __init__(self, path, timeout=3.0):
        self.path, self.timeout, self.owned = str(path), timeout, False

    def __enter__(self):
        end = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.owned = True
                return self
            except FileExistsError:
                if time.time() - mtime(self.path) > 10:  # зависшая блокировка
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() > end:
                    return self
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self.owned:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def write_pid(name):
    (RUNTIME / f"{name}.pid").write_text(str(os.getpid()))


def clear_pid(name):
    p = RUNTIME / f"{name}.pid"
    try:
        if int(p.read_text()) == os.getpid():
            p.unlink()
    except (OSError, ValueError):
        pass


def running_pid(name):
    """PID другой запущенной копии программы `name` или None."""
    try:
        pid = int((RUNTIME / f"{name}.pid").read_text())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and pid_alive(pid) else None


def send_command(cmd, target="overlay"):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME / f"cmd_{target}_{time.time_ns()}_{os.getpid()}.tmp"
    tmp.write_text(cmd, encoding="utf-8")
    os.replace(tmp, tmp.with_suffix(".cmd"))


def take_commands(target):
    out = []
    for p in sorted(RUNTIME.glob(f"cmd_{target}_*.cmd")):
        try:
            out.append(p.read_text(encoding="utf-8").strip())
            p.unlink()
        except OSError:
            pass
    return out


PAUSE_FLAG = RUNTIME / "hotkeys_paused"


def set_hotkeys_paused(paused):
    try:
        if paused:
            PAUSE_FLAG.write_text(str(os.getpid()))
        else:
            PAUSE_FLAG.unlink()
    except OSError:
        pass


def hotkeys_paused():
    try:
        return pid_alive(int(PAUSE_FLAG.read_text()))
    except (OSError, ValueError):
        return False


# --- состояние (активный чат, позиция панели) ---------------------------------
def load_state():
    st = load_json(STATE_PATH, {})
    return st if isinstance(st, dict) else {}


def update_state(**kw):
    with FileLock(STATE_PATH.with_suffix(".lock")):
        st = load_state()
        st.update(kw)
        save_json(STATE_PATH, st)
        return st


# --- чаты ----------------------------------------------------------------------
def _chat_path(cid):
    return CHATS / f"{cid}.json"


def list_chats():
    out = []
    for p in CHATS.glob("*.json"):
        c = load_json(p, None)
        if isinstance(c, dict) and c.get("id"):
            out.append(c)
    out.sort(key=lambda c: c.get("updated", ""), reverse=True)
    return out


def chats_signature():
    return tuple(sorted((p.name, mtime(p)) for p in CHATS.glob("*.json")))


def load_chat(cid):
    if not cid:
        return None
    c = load_json(_chat_path(cid), None)
    return c if isinstance(c, dict) else None


def create_chat(title=None):
    cid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    c = {"id": cid, "title": title or DEFAULT_TITLE, "created": now_iso(), "updated": now_iso(),
         "session_id": None, "screenshot_prompt": "", "history": []}
    save_json(_chat_path(cid), c)
    return c


def update_chat(cid, fn):
    with FileLock(CHATS / f"{cid}.lock"):
        c = load_chat(cid)
        if c is None:
            return None
        fn(c)
        c["updated"] = now_iso()
        c["history"] = c.get("history", [])[-400:]
        save_json(_chat_path(cid), c)
        return c


def append_messages(cid, msgs, session_id=None, clear_session=False):
    def fn(c):
        c.setdefault("history", []).extend(msgs)
        if clear_session:
            c["session_id"] = None
        elif session_id:
            c["session_id"] = session_id
        if c.get("title") == DEFAULT_TITLE:
            for m in msgs:
                t = (m.get("text") or "").strip()
                if m.get("role") == "you" and t and t[0] not in "📷📎":
                    c["title"] = t.splitlines()[0][:40]
                    break
    return update_chat(cid, fn)


def delete_chat(cid):
    try:
        _chat_path(cid).unlink()
    except OSError:
        pass


def set_active_chat(cid):
    update_state(active_chat=cid)


def get_active_chat():
    c = load_chat(load_state().get("active_chat"))
    if c:
        return c
    chats = list_chats()
    c = chats[0] if chats else create_chat()
    set_active_chat(c["id"])
    return c


# --- «занятость» чата: в один чат одновременно пишет только одна программа ----
def _busy_path(cid):
    return RUNTIME / f"busy_{cid}.json"


def acquire_chat(cid, who):
    """None — чат захвачен нами; иначе — кто сейчас его занимает."""
    p = _busy_path(cid)
    for _ in range(3):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "who": who}).encode())
            os.close(fd)
            return None
        except FileExistsError:
            info = load_json(p, {}) or {}
            if info and not pid_alive(info.get("pid")):
                try:
                    p.unlink()
                except OSError:
                    pass
                continue
            if not info:
                time.sleep(0.05)
                continue
            return info.get("who", "?")
    return "?"


def release_chat(cid):
    p = _busy_path(cid)
    info = load_json(p, {}) or {}
    if info.get("pid") == os.getpid():
        try:
            p.unlink()
        except OSError:
            pass


def chat_busy_by(cid):
    info = load_json(_busy_path(cid), {}) or {}
    return info.get("who") if info and pid_alive(info.get("pid")) else None


WHO_NAMES = {"overlay": "оверлей", "manager": "«Управление»"}


def migrate_old_state():
    """Перенос единственного диалога из старой версии (state.json рядом с программой)."""
    if not OLD_STATE.exists():
        return
    old = load_json(OLD_STATE, {}) or {}
    if (old.get("history") or old.get("session_id")) and not list_chats():
        c = create_chat("Чат 1")
        c["history"] = old.get("history") or []
        c["session_id"] = old.get("session_id")
        save_json(_chat_path(c["id"]), c)
        set_active_chat(c["id"])
    if old.get("overlay_pos"):
        update_state(overlay_pos=old["overlay_pos"])
    try:
        os.replace(OLD_STATE, OLD_STATE.with_name("state.old.json"))
    except OSError:
        pass


# --- запуск и переключение между программами ----------------------------------
def python_gui_exe():
    exe = Path(sys.executable)
    if IS_WIN:
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def launch(script):
    kw = {"cwd": str(APP_DIR)}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([python_gui_exe(), str(SRC_DIR / script)], **kw)


def focus_window(title):
    if not IS_WIN:
        return False
    u = ctypes.windll.user32
    h = u.FindWindowW(None, title)
    if not h:
        return False
    u.ShowWindow(h, 9)  # SW_RESTORE
    u.SetForegroundWindow(h)
    return True


def resolve_error(text):
    """Понятное объяснение ошибки Claude Code + что сделать. (сообщение, сбросить_сессию)."""
    t = (text or "").strip()
    low = t.lower()
    if "no conversation found" in low or ("session" in low and "not found" in low):
        return "⚠ Старая сессия Claude не найдена — чат продолжится с чистого листа. Повтори запрос.", True
    if any(k in low for k in ("login", "log in", "/login", "authenticat", "oauth", "invalid api key")):
        return ("⚠ Не выполнен вход в Claude. Открой «Управление» → ⚙ Настройки → Нейросети → "
                "Claude: войди в аккаунт или выбери токен / API-ключ.\n\n" + t), False
    if "limit" in low:
        return "⚠ Похоже, исчерпан лимит подписки на текущее окно.\n\n" + t, False
    return "⚠ " + (t or "Неизвестная ошибка"), False


# ----------------------------------------------------------------------------
# Режим «пометки по всему экрану»
# ----------------------------------------------------------------------------
DISPLAY_MODES = [("panel", "Текстом в панели"), ("screen", "Пометками по всему экрану")]

SCREEN_PROMPT = """Ответь в режиме «пометки на экране»: ответ будет нарисован поверх экрана \
рамками и подписями в нужных местах.
Размер изображения: {w}×{h} пикселей, начало координат — левый верхний угол.
Верни ТОЛЬКО JSON, без текста до и после:
{{"summary": "общий ответ одной-двумя фразами (можно пусто)",
 "marks": [{{"box": [x1, y1, x2, y2], "text": "короткая подпись"}}]}}
Правила:
- box — прямоугольник в пикселях ЭТОГО изображения вокруг места, к которому относится \
подпись: вопрос, вариант ответа, поле, кнопка, строка, ошибка.
- Одна пометка — одно место, не больше 12 пометок, по порядку чтения.
- В text — сам ответ или действие (до ~15 слов), без рассуждений.
- Формулы в text пиши простым текстом (x², √x, (a+b)/c), без LaTeX.
- Если показывать на экране нечего — пустой marks и ответ в summary."""


def fit_for_vision(img, max_side):
    """Уменьшить так, чтобы модель видела картинку без дополнительного сжатия.
    Возвращает (картинка, масштаб = новая ширина / исходная)."""
    w, h = img.size
    k = min(1.0, max_side / max(w, h), 1568 / max(w, h), (1_150_000 / (w * h)) ** 0.5)
    if k >= 1.0:
        return img.convert("RGB"), 1.0
    nw, nh = max(1, int(w * k)), max(1, int(h * k))
    return img.convert("RGB").resize((nw, nh), Image.LANCZOS), nw / w


def parse_marks(text):
    """JSON с пометками из ответа Claude → {'summary', 'marks'} или None."""
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(t[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    marks = []
    for m in data.get("marks") or []:
        if not isinstance(m, dict):
            continue
        b = m.get("box")
        p = m.get("point")
        if isinstance(b, (list, tuple)) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
            x1, y1, x2, y2 = b
        elif isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p):
            x1, y1, x2, y2 = p[0] - 12, p[1] - 12, p[0] + 12, p[1] + 12
        else:
            continue
        marks.append({"box": [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)],
                      "text": str(m.get("text") or "").strip()[:300]})
    return {"summary": str(data.get("summary") or "").strip(), "marks": marks[:20]}


def marks_as_text(parsed):
    lines = [parsed["summary"]] if parsed["summary"] else []
    lines += [f"{i}. {m['text']}" for i, m in enumerate(parsed["marks"], 1) if m["text"]]
    return "\n".join(lines) or "(пометок нет)"


def make_click_through(win, enable=True):
    """Окно пропускает клики мыши насквозь."""
    if IS_LINUX:
        return _x11_click_through(win, enable)
    if IS_MAC:
        return _mac_click_through(win, enable)
    if not IS_WIN:
        return
    try:
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        style = u.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        style |= 0x80000 | 0x80 | 0x08000000  # LAYERED | TOOLWINDOW | NOACTIVATE
        style = (style | 0x20) if enable else (style & ~0x20)  # TRANSPARENT
        u.SetWindowLongW(hwnd, -20, style)
    except Exception:
        pass


class ScreenMarks:
    """Рамки с номерами и подписи поверх экрана. Каждая деталь — маленькое окно без рамки,
    поэтому всё остальное на экране остаётся видимым и кликабельным."""

    EDGE = 3

    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg["overlay"]
        self.wins = []
        self.visible = False
        self._hide_job = None
        fam = "Segoe UI" if IS_WIN else "Helvetica"
        self.font = tkfont.Font(family=fam, size=self.cfg["font_size"])
        self.badge_font = tkfont.Font(family=fam, size=max(8, self.cfg["font_size"] - 1), weight="bold")

    def apply_config(self, cfg):
        self.cfg = cfg["overlay"]
        self.font.configure(size=self.cfg["font_size"])
        self.badge_font.configure(size=max(8, self.cfg["font_size"] - 1))

    @property
    def has_marks(self):
        return bool(self.wins)

    def clear(self):
        self._cancel_hide()
        for w in self.wins:
            try:
                w.destroy()
            except tk.TclError:
                pass
        self.wins = []
        self.visible = False

    def hide(self):
        self._cancel_hide()
        for w in self.wins:
            w.withdraw()
        self.visible = False

    def show(self):
        for w in self.wins:
            w.deiconify()
            make_click_through(w)
        self.visible = bool(self.wins)

    def toggle(self):
        self.hide() if self.visible else self.show()

    def _cancel_hide(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None

    def _win(self, bg, alpha=1.0):
        t = tk.Toplevel(self.root)
        t.withdraw()
        t.title(f"claude-overlay-mark-{id(t)}")
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        if alpha < 1.0:
            t.attributes("-alpha", alpha)
        t.configure(bg=bg)
        self.wins.append(t)
        return t

    def _place(self, t, x, y, w, h):
        t.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")

    def draw(self, parsed, mapping):
        """mapping: ox, oy — где на экране левый верхний угол скриншота; scale — во сколько
        раз картинка для Claude меньше снимка; area — монитор, за который не выходить."""
        self.clear()
        c = theme_colors(self.cfg.get("theme", "dark"))
        area = mapping["area"]
        AL, AT = area["left"], area["top"]
        AR, AB = AL + area["width"], AT + area["height"]
        k = mapping["scale"] or 1.0
        placed = []
        wrap = max(200, min(420, int(self.cfg.get("width", 460) * 0.8)))
        alpha = min(1.0, max(0.2, float(self.cfg.get("marks_opacity", 0.9))))

        def label(text, number=None):
            t = self._win(c["accent"], alpha)
            fr = tk.Frame(t, bg=c["bg"])
            fr.pack(padx=2, pady=2)
            if number is not None:
                tk.Label(fr, text=str(number), bg=c["accent"], fg="white", font=self.badge_font,
                         padx=6).pack(side="left", fill="y")
            tk.Label(fr, text=text, bg=c["bg"], fg=c["fg"], font=self.font, justify="left",
                     wraplength=wrap, padx=8, pady=4).pack(side="left")
            t.update_idletasks()
            return t, t.winfo_reqwidth(), t.winfo_reqheight()

        def fits(x, y, w, h):
            if x < AL or y < AT or x + w > AR or y + h > AB:
                return False
            return all(x + w <= px or px + pw <= x or y + h <= py or py + ph <= y
                       for px, py, pw, ph in placed)

        boxes = []  # сначала все рамки — подписи не должны их закрывать
        for i, m in enumerate(parsed["marks"], 1):
            x1, y1, x2, y2 = m["box"]
            x1, x2 = mapping["ox"] + x1 / k, mapping["ox"] + x2 / k
            y1, y2 = mapping["oy"] + y1 / k, mapping["oy"] + y2 / k
            pad = 4
            x1, y1 = max(AL, x1 - pad), max(AT, y1 - pad)
            x2, y2 = min(AR, x2 + pad), min(AB, y2 + pad)
            if x2 - x1 < 6 or y2 - y1 < 6:
                continue
            boxes.append((i, m, x1, y1, x2, y2))
            placed.append((x1, y1, x2 - x1, y2 - y1))

        for i, m, x1, y1, x2, y2 in boxes:
            e = self.EDGE
            for (x, y, w, h) in ((x1, y1, x2 - x1, e), (x1, y2 - e, x2 - x1, e),
                                 (x1, y1, e, y2 - y1), (x2 - e, y1, e, y2 - y1)):
                self._place(self._win(c["accent"], alpha), x, y, w, h)
            t, w, h = label(strip_latex(m["text"]) or "•", i)
            gap = 8
            for (x, y) in ((x2 + gap, y1), (x1 - gap - w, y1), (x1, y2 + gap), (x1, y1 - gap - h),
                           (x2 - w, y2 + gap), (x2 + gap, y2 - h)):
                if fits(x, y, w, h):
                    break
            else:  # места нет — ставим как можно ближе, не выходя за экран
                x = min(max(AL, x2 + gap), AR - w)
                y = min(max(AT, y1), AB - h)
            placed.append((x, y, w, h))
            self._place(t, x, y, w, h)

        if parsed["summary"]:
            t, w, h = label(strip_latex(parsed["summary"]))
            cx = mapping["ox"] + 16
            cy = mapping["oy"] + 16
            for (x, y) in ((cx, cy), (AR - w - 16, AT + 16), (AL + 16, AB - h - 16), (AR - w - 16, AB - h - 16)):
                if fits(x, y, w, h):
                    break
            else:
                x, y = min(max(AL, cx), AR - w), min(max(AT, cy), AB - h)
            placed.append((x, y, w, h))
            self._place(t, x, y, w, h)

        self.show()
        sec = self.cfg.get("marks_seconds") or 0
        if sec > 0:
            self._hide_job = self.root.after(int(sec * 1000), self.hide)


# ----------------------------------------------------------------------------
# Формулы LaTeX: рисуются картинками через matplotlib (mathtext)
# ----------------------------------------------------------------------------
MATH_MODES = [("latex", "LaTeX — рисовать формулы"), ("plain", "Простой текст (x², √x)")]

LATEX_NOTE = ("Формулы оформляй в LaTeX: в строке — $...$, отдельной строкой — $$...$$. "
              "Деление — \\frac{}{}, над векторами — \\vec{}. Без окружений "
              "(cases, matrix, align) — их приложение не нарисует.")
PLAIN_NOTE = "Формулы пиши простым текстом (x², √x, ∫, ≤, π, →), без LaTeX."

# $$…$$, \[…\], \(…\), $…$ (одиночный $ — не у цифр и букв, чтобы «5$» не считалось формулой)
MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)"
                     r"|(?<![\\$\w])\$(?![\s$])([^$\n]+?)(?<![\s\\])\$(?![\w$])", re.S)


def math_note(cfg):
    return LATEX_NOTE if cfg.get("math", "latex") == "latex" else PLAIN_NOTE


def strip_latex(s):
    """Грубо убрать LaTeX-разметку (для мест, где формулы не рисуются)."""
    s = MATH_RE.sub(lambda m: next(g for g in m.groups() if g is not None), s)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\(?:vec|overrightarrow|mathbf|text|mathrm)\{([^{}]*)\}", r"\1", s)
    s = s.replace("\\cdot", "·").replace("\\times", "×").replace("\\sqrt", "√")
    s = s.replace("\\pi", "π").replace("\\le", "≤").replace("\\ge", "≥").replace("\\pm", "±")
    return re.sub(r"\\([a-zA-Z]+)", r"\1", s)


class MathRenderer:
    """LaTeX → картинка. Без matplotlib просто возвращает None (формула останется текстом)."""

    def __init__(self):
        self.available = None
        self._lock = threading.Lock()
        self._cache = {}

    def _load(self):
        with self._lock:
            if self.available is None:
                try:
                    import matplotlib
                    matplotlib.use("Agg")
                    matplotlib.rcParams["mathtext.fontset"] = "cm"
                    import importlib
                    importlib.import_module("matplotlib.figure")
                    importlib.import_module("matplotlib.backends.backend_agg")
                    self.available = True
                except Exception:
                    self.available = False
            return self.available

    def preload(self):
        threading.Thread(target=self._load, daemon=True).start()

    def image(self, tex, size_pt, fg, bg, dpi, display=False, max_w=None):
        """PhotoImage с формулой или None, если нарисовать не вышло."""
        key = (tex, size_pt, fg, bg, round(dpi), display, max_w)
        if key in self._cache:
            return self._cache[key]
        img = None
        if self._load():
            try:
                from matplotlib.figure import Figure
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                from matplotlib.font_manager import FontProperties
                fig = Figure(figsize=(1, 1))
                FigureCanvasAgg(fig)
                prop = FontProperties(size=size_pt * (1.3 if display else 1.12))
                fig.text(0, 0, f"${tex.strip()}$", fontproperties=prop, color=fg)
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=dpi, facecolor=bg,
                            bbox_inches="tight", pad_inches=0.03)
                pil = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
                if max_w and pil.width > max_w:
                    pil = pil.resize((max_w, max(1, int(pil.height * max_w / pil.width))),
                                     Image.LANCZOS)
                img = ImageTk.PhotoImage(pil)
            except Exception:
                img = None
        if len(self._cache) > 400:
            self._cache.clear()
        self._cache[key] = img
        return img


MATH = MathRenderer()


def insert_rich(widget, text, *, fg, bg, size_pt, enabled=True, tags=()):
    """Дописать в конец tk.Text текст с Markdown-чисткой и формулами-картинками."""
    if not enabled:
        widget.insert("end", clean_md(text, strip=False), tags)
        return
    imgs = widget.__dict__.setdefault("_math_imgs", [])
    try:
        dpi = widget.winfo_fpixels("1i")
    except tk.TclError:
        dpi = 96
    max_w = max(120, widget.winfo_width() - 40) if widget.winfo_width() > 50 else None
    widget.tag_configure("math_center", justify="center")
    pos = 0
    for m in MATH_RE.finditer(text):
        widget.insert("end", clean_md(text[pos:m.start()], strip=False), tags)
        pos = m.end()
        display = m.group(1) is not None or m.group(2) is not None
        tex = next(g for g in m.groups() if g is not None)
        img = MATH.image(tex, size_pt, fg, bg, dpi, display, max_w)
        if img is None:
            widget.insert("end", m.group(0), tags)
            continue
        imgs.append(img)
        if display:
            if widget.get("end-2c", "end-1c") not in ("\n", ""):
                widget.insert("end", "\n", tags)
            widget.image_create("end", image=img, align="center", padx=2, pady=4)
            widget.insert("end", "\n", ("math_center",) + tuple(tags))
            widget.tag_add("math_center", "end-2l linestart", "end-2l lineend")
            # пропустить перевод строки сразу после $$…$$, чтобы не было пустой строки
            if text[pos:pos + 1] == "\n":
                pos += 1
        else:
            widget.image_create("end", image=img, align="center", padx=1)
    widget.insert("end", clean_md(text[pos:], strip=False), tags)


# ----------------------------------------------------------------------------
# Вход по ссылке: `claude auth login` без терминала, ссылка и код — через окно
# ----------------------------------------------------------------------------
_URL_RE = re.compile(r"https://[^\s\x1b\x07\"'<>]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class LinkLogin:
    """Запускает `claude auth login`, достаёт из вывода ссылку для входа и передаёт код."""

    def __init__(self, exe):
        self.url = None
        self.output = ""
        env = claude_env()
        # Не открывать браузер самим: Claude Code запускает команду из BROWSER,
        # а эта команда ничего не делает — ссылку пользователь откроет сам.
        env["BROWSER"] = "cmd.exe /c rem" if IS_WIN else "true"
        self.proc = subprocess.Popen(
            [exe, "auth", "login"], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        raw = b""
        while True:
            try:
                chunk = self.proc.stdout.read1(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            raw += chunk
            text = raw.decode("utf-8", "replace")
            if not self.url:
                urls = [u.rstrip(".,;)]") for u in _URL_RE.findall(text)]
                good = [u for u in urls if "oauth" in u or "authorize" in u or "login" in u]
                if good or urls:
                    self.url = max(good or urls, key=len)
            self.output = _ANSI_RE.sub("", text)[-2000:]

    def send_code(self, code):
        try:
            self.proc.stdin.write((code.strip() + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def press_enter(self):
        self.send_code("")

    @property
    def finished(self):
        return self.proc.poll() is not None

    def cancel(self):
        if self.proc.poll() is None:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                                   capture_output=True, creationflags=NO_WINDOW)
                else:
                    self.proc.terminate()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Особенности Linux (X11) и macOS
# ----------------------------------------------------------------------------
_SCALE = 1.0  # сколько пикселей снимка в одной единице Tk (на Retina-маках — 2)


def set_screen_scale(root):
    """На macOS Tk работает в «точках», а снимок экрана — в пикселях (Retina ×2)."""
    global _SCALE
    _SCALE = 1.0
    if IS_MAC:
        try:
            with mss.mss() as sct:
                px = sct.monitors[1]["width"]
            pt = root.winfo_screenwidth()
            if pt > 0 and px > pt:
                _SCALE = round(px / pt, 3)
        except Exception:
            pass
    return _SCALE


def screen_scale():
    return _SCALE


def _to_tk(m):
    s = _SCALE
    return {"left": int(round(m["left"] / s)), "top": int(round(m["top"] / s)),
            "width": int(round(m["width"] / s)), "height": int(round(m["height"] / s))}


_xdisplay = None


def _x11_display():
    global _xdisplay
    if _xdisplay is None:
        from Xlib import display
        _xdisplay = display.Display()
    return _xdisplay


def _x11_click_through(win, enable):
    """X11: пустая «входная» форма окна — клики проходят насквозь (расширение SHAPE)."""
    try:
        from Xlib import X
        from Xlib.ext import shape
        d = _x11_display()
        win.update_idletasks()
        wid = int(win.wm_frame(), 16)
        w = d.create_resource_object("window", wid)
        if enable:
            w.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
        else:
            w.shape_mask(shape.SO.Set, shape.SK.Input, 0, 0, X.NONE)
        d.sync()
    except Exception:
        pass


def _mac_click_through(win, enable):
    """macOS: setIgnoresMouseEvents у NSWindow (нужен pyobjc-framework-Cocoa)."""
    try:
        from AppKit import NSApplication
        win.update_idletasks()
        title = win.title()
        for nw in NSApplication.sharedApplication().windows():
            if str(nw.title()) == title:
                nw.setIgnoresMouseEvents_(bool(enable))
    except Exception:
        pass


def _linux_workarea(mon):
    """Linux: часть монитора без панелей рабочего стола (_NET_WORKAREA)."""
    try:
        d = _x11_display()
        root = d.screen().root
        atom = d.intern_atom("_NET_WORKAREA")
        prop = root.get_full_property(atom, 0)
        if not prop or len(prop.value) < 4:
            return dict(mon)
        x, y, w, h = [int(v) for v in prop.value[:4]]
        L = max(mon["left"], x)
        T = max(mon["top"], y)
        R = min(mon["left"] + mon["width"], x + w)
        B = min(mon["top"] + mon["height"], y + h)
        if R - L > 100 and B - T > 100:
            return {"left": L, "top": T, "width": R - L, "height": B - T}
    except Exception:
        pass
    return dict(mon)


def _mac_workarea(mon):
    """macOS: видимая часть экрана без меню-бара и Дока (NSScreen.visibleFrame)."""
    try:
        from AppKit import NSScreen
        screens = NSScreen.screens()
        main_h = screens[0].frame().size.height
        for sc in screens:
            fr, vf = sc.frame(), sc.visibleFrame()
            left, top = int(fr.origin.x), int(main_h - fr.origin.y - fr.size.height)
            if abs(left - mon["left"]) <= 2 and abs(top - mon["top"]) <= 2:
                return {"left": int(vf.origin.x),
                        "top": int(main_h - vf.origin.y - vf.size.height),
                        "width": int(vf.size.width), "height": int(vf.size.height)}
    except Exception:
        pass
    # без pyobjc — грубо: меню-бар сверху и Док снизу
    if mon["top"] == 0:
        return {"left": mon["left"], "top": 25, "width": mon["width"], "height": mon["height"] - 95}
    return dict(mon)


_X11_KEYSYMS = {"pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left",
                "right": "Right", "space": "space", "enter": "Return", "tab": "Tab",
                "esc": "Escape", "home": "Home", "end": "End", "insert": "Insert",
                "delete": "Delete", "`": "grave", "-": "minus", "=": "equal",
                "[": "bracketleft", "]": "bracketright", ";": "semicolon", "'": "apostrophe",
                ",": "comma", ".": "period", "/": "slash"}


class X11Hotkeys(threading.Thread):
    """Linux (X11): XGrabKey по физическим клавишам — работает в любой раскладке."""

    MODS = {"ctrl": 4, "control": 4, "shift": 1, "alt": 8, "win": 64}  # Control/Shift/Mod1/Mod4
    IGNORED = (0, 2, 16, 2 | 16)  # CapsLock, NumLock — не должны мешать

    def __init__(self, bindings, post):
        super().__init__(daemon=True)
        self.bindings, self.post = bindings, post
        self._halt = False

    def _parse(self, d, combo):
        from Xlib import XK
        mods, kc = 0, None
        for part in combo.lower().replace(" ", "").split("+"):
            if part in self.MODS:
                mods |= self.MODS[part]
                continue
            name = _X11_KEYSYMS.get(part) or (part.upper() if re.fullmatch(r"f\d+", part) else part)
            sym = XK.string_to_keysym(name)
            kc = d.keysym_to_keycode(sym) if sym else 0
            if not kc:
                raise ValueError(f"не понимаю клавишу «{part}» в «{combo}»")
        if kc is None:
            raise ValueError(f"нет основной клавиши в «{combo}»")
        return mods, kc

    def run(self):
        try:
            from Xlib import X, error
            from Xlib import display as xdisplay
            d = xdisplay.Display()  # своё соединение: этот поток читает события
        except Exception as e:
            self.post(("hotkey_fail", [f"X11 недоступен ({e}). Нужен сеанс Xorg, не Wayland."]))
            return
        root = d.screen().root
        table, failed = {}, []
        for action, combo in self.bindings.items():
            if not combo:
                continue
            try:
                mods, kc = self._parse(d, combo)
            except ValueError as e:
                failed.append(str(e))
                continue
            ec = error.CatchError(error.BadAccess)
            for extra in self.IGNORED:
                root.grab_key(kc, mods | extra, True, X.GrabModeAsync, X.GrabModeAsync, onerror=ec)
            d.sync()
            if ec.get_error():
                failed.append(f"{combo} (уже занято другой программой)")
                continue
            table[(kc, mods)] = action
        if failed:
            self.post(("hotkey_fail", failed))
        mask = 1 | 4 | 8 | 64
        try:
            while not self._halt:
                while d.pending_events():
                    ev = d.next_event()
                    if ev.type == X.KeyPress:
                        a = table.get((ev.detail, ev.state & mask))
                        if a:
                            self.post(("hotkey", a))
                time.sleep(0.03)
        finally:
            try:
                for (kc, mods) in table:
                    for extra in self.IGNORED:
                        root.ungrab_key(kc, mods | extra)
                d.sync()
                d.close()
            except Exception:
                pass

    def stop(self):
        self._halt = True
        self.join(1.5)


def make_hotkeys(bindings, post):
    """Глобальные горячие клавиши для текущей системы."""
    if IS_WIN:
        return WinHotkeys(bindings, post)
    if IS_LINUX and os.environ.get("DISPLAY"):
        import importlib.util
        if importlib.util.find_spec("Xlib"):  # python-xlib
            return X11Hotkeys(bindings, post)
    return PynputHotkeys(bindings, post)
