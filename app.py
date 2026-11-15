#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude поверх экрана — экранный помощник, работающий по ПОДПИСКЕ Claude (Pro/Max)
через установленный Claude Code (команда `claude`), без API-ключа.

• Окно чата: заранее обсуждаешь задачу, прикрепляешь файлы.
• Горячая клавиша: скриншот экрана/области → в тот же диалог → ответ в
  полупрозрачной панели поверх всех окон, которая пропускает клики мыши.
"""
import base64
import ctypes
import io
import json
import os
import queue
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
from tkinter import filedialog, messagebox, ttk

import mss
from PIL import Image, ImageEnhance, ImageTk

IS_WIN = sys.platform == "win32"
APP_DIR = Path(__file__).resolve().parent
WORK = APP_DIR / "workspace"
FILES = WORK / "files"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"

# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "claude_path": "",          # пусто = искать `claude` автоматически
    "model": "",                # "", "sonnet", "opus", "haiku", "fable"
    "screenshot_prompt": (
        "Это скриншот моего экрана. Помоги с тем, что на нём, "
        "учитывая всё, о чём мы договорились раньше."
    ),
    "max_image_side": 1600,     # px, длинная сторона скриншота
    "overlay": {
        "width": 460,
        "max_height": 420,
        "opacity": 0.85,
        "font_size": 11,
        "corner": "top-right",  # top-right / top-left / bottom-right / bottom-left
        "margin": 24,
        "auto_hide_seconds": 0,  # 0 = не прятать автоматически
    },
    "hotkeys": {
        "shot": "ctrl+alt+s",          # скрин всего экрана → Claude
        "region": "ctrl+alt+a",        # выделить область → Claude
        "toggle": "ctrl+alt+h",        # скрыть / показать панель
        "scroll_up": "ctrl+alt+pageup",
        "scroll_down": "ctrl+alt+pagedown",
        "move": "ctrl+alt+m",          # режим перемещения панели
        "chat": "ctrl+alt+c",          # открыть окно чата
        "stop": "ctrl+alt+x",          # остановить ответ
    },
}

DEFAULT_CLAUDE_MD = """# Экранный помощник

Ты работаешь как помощник поверх экрана. Пользователь присылает скриншоты своего
экрана (по горячей клавише) или пишет в окне чата. Ответ показывается в небольшой
полупрозрачной панели в углу экрана, поэтому:

- Отвечай по-русски, коротко и по делу: сначала сам ответ, потом, если нужно, пояснение.
- Не используй Markdown-таблицы и заголовки; простые списки можно.
- Формулы пиши простым Unicode-текстом (x², √x, ∫, ≤, π, →), не LaTeX — панель его не рисует.
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
def clean_md(s: str) -> str:
    """Лёгкая чистка Markdown для показа в простом окне."""
    s = re.sub(r"^\s*```.*$\n?", "", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"\1", s, flags=re.S)
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = re.sub(r"^(\s*)[-*]\s+", r"\1• ", s, flags=re.M)
    return s.strip("\n")


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
    """Скриншот монитора, на котором сейчас курсор."""
    with mss.mss() as sct:
        mons = sct.monitors[1:] or [sct.monitors[0]]
        mon = next((m for m in mons
                    if m["left"] <= x < m["left"] + m["width"]
                    and m["top"] <= y < m["top"] + m["height"]), mons[0])
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, dict(mon)


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
    ("chat", "Открыть окно чата"),
    ("stop", "Остановить ответ"),
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

    def start(self, exe, blocks, session_id, model):
        self.req_id += 1
        self.cancelled = False
        rid = self.req_id
        threading.Thread(target=self._run, args=(rid, exe, blocks, session_id, model),
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

    def _run(self, rid, exe, blocks, session_id, model):
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
class Overlay:
    BG, FG, DIM, ACCENT = "#16181d", "#ececec", "#8b8f98", "#d97757"

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

        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-alpha", self.cfg["opacity"])
        w.configure(bg=self.BG)

        self.border = tk.Frame(w, bg=self.BG, padx=2, pady=2)
        self.border.pack(fill="both", expand=True)
        inner = tk.Frame(self.border, bg=self.BG)
        inner.pack(fill="both", expand=True)

        head = tk.Frame(inner, bg=self.BG)
        head.pack(fill="x", padx=10, pady=(6, 0))
        self.status = tk.Label(head, text="Claude", fg=self.ACCENT, bg=self.BG,
                               font=self.small, anchor="w")
        self.status.pack(side="left")
        self.hint = tk.Label(head, text="", fg=self.DIM, bg=self.BG, font=self.small, anchor="e")
        self.hint.pack(side="right")
        self.head = head

        self.text = tk.Text(inner, wrap="word", bg=self.BG, fg=self.FG, bd=0,
                            highlightthickness=0, padx=10, pady=6, font=self.font,
                            cursor="arrow", insertwidth=0)
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")

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
        self._schedule_fit()

    # --- видимость -------------------------------------------------------
    def show(self):
        if not self.visible:
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
        if not IS_WIN:
            return
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            style = u.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            style |= 0x80000 | 0x80 | 0x08000000  # LAYERED | TOOLWINDOW | NOACTIVATE
            if self.move_mode:
                style &= ~0x20
            else:
                style |= 0x20  # TRANSPARENT: клики проходят насквозь
            u.SetWindowLongW(hwnd, -20, style)
        except Exception:
            pass

    # --- содержимое ------------------------------------------------------
    def set_status(self, s):
        self.status.configure(text=f"Claude · {s}" if s else "Claude")

    def set_text(self, s, scroll_end=False):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", clean_md(s))
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
        try:
            r = self.text.count("1.0", "end-1c", "displaylines")
            n = r[0] if isinstance(r, tuple) else (r or 0)
        except Exception:
            n = int(self.text.index("end-1c").split(".")[0])
        has_text = bool(self.text.get("1.0", "end-1c").strip())
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
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        m = self.cfg["margin"]
        corner = self.cfg.get("corner", "top-right")
        x = sw - w - m if "right" in corner else m
        y = sh - h - m - 40 if "bottom" in corner else m
        return x, y

    # --- перемещение -----------------------------------------------------
    def toggle_move(self):
        self.move_mode = not self.move_mode
        self.border.configure(bg=self.ACCENT if self.move_mode else self.BG)
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
        dark = ImageEnhance.Brightness(img).enhance(0.55)
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
        self._close(self.img.crop(box))

    def _close(self, crop):
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()
        (self.on_done(crop) if crop is not None else self.on_cancel())


# ----------------------------------------------------------------------------
# Окно настроек
# ----------------------------------------------------------------------------
CORNERS = [("top-right", "справа сверху"), ("top-left", "слева сверху"),
           ("bottom-right", "справа снизу"), ("bottom-left", "слева снизу")]
MODEL_LABEL_DEFAULT = "по умолчанию"
MODELS = [MODEL_LABEL_DEFAULT, "sonnet", "opus", "haiku", "fable"]


class SettingsWindow:
    def __init__(self, app, tab="account"):
        self.app = app
        self.cfg = json.loads(json.dumps(app.cfg))  # рабочая копия
        self.alive = True
        self.capturing = None
        self.mods = set()
        self._login_deadline = 0
        self._install_deadline = 0
        app.stop_hotkeys()  # чтобы при назначении клавиш ничего не срабатывало

        t = self.top = tk.Toplevel(app.root)
        t.title("Настройки")
        t.geometry("700x580")
        t.minsize(600, 500)
        t.transient(app.root)
        t.protocol("WM_DELETE_WINDOW", self.cancel)
        t.bind("<KeyPress>", self._key_press)
        t.bind("<KeyRelease>", self._key_release)

        nb = self.nb = ttk.Notebook(t)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs = {}
        for key, title, build in (("account", "Аккаунт", self._tab_account),
                                  ("hotkeys", "Горячие клавиши", self._tab_hotkeys),
                                  ("overlay", "Панель", self._tab_overlay),
                                  ("claude", "Claude", self._tab_claude)):
            f = ttk.Frame(nb, padding=14)
            nb.add(f, text=title)
            build(f)
            self.tabs[key] = f
        self.select(tab)

        bar = ttk.Frame(t, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Отмена", command=self.cancel).pack(side="right")
        ttk.Button(bar, text="Сохранить", command=self.save).pack(side="right", padx=6)
        self.refresh_auth()

    def select(self, tab):
        self.nb.select(self.tabs.get(tab, self.tabs["account"]))

    def lift(self, tab=None):
        if tab:
            self.select(tab)
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    # --- фоновые задачи ---------------------------------------------------
    def _bg(self, func, done):
        box = []
        threading.Thread(target=lambda: box.append(func()), daemon=True).start()

        def check():
            if not self.alive:
                return
            if box:
                done(box[0])
            else:
                self.top.after(150, check)
        self.top.after(150, check)

    def _exe(self):
        return find_claude(self.path_var.get().strip() if hasattr(self, "path_var")
                           else self.cfg.get("claude_path", ""))

    # --- вкладка «Аккаунт» ------------------------------------------------
    def _tab_account(self, f):
        ttk.Label(f, wraplength=640, text=(
            "Приложение работает через официальный Claude Code и расходует лимиты "
            "твоей подписки Claude (Pro или Max). API-ключ не нужен.")).pack(anchor="w")
        self.exe_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.exe_var, foreground="#555", wraplength=640).pack(
            anchor="w", pady=(12, 0))
        self.auth_var = tk.StringVar(value="Проверяю…")
        ttk.Label(f, textvariable=self.auth_var, wraplength=640,
                  font=("Segoe UI" if IS_WIN else "Helvetica", 11, "bold")).pack(anchor="w", pady=10)
        row = ttk.Frame(f)
        row.pack(anchor="w")
        self.btn_install = ttk.Button(row, text="Установить Claude Code", command=self.install)
        self.btn_login = ttk.Button(row, text="Войти в аккаунт Claude", command=self.login)
        self.btn_logout = ttk.Button(row, text="Выйти", command=self.logout)
        self.btn_refresh = ttk.Button(row, text="Обновить", command=self.refresh_auth)
        for b in (self.btn_install, self.btn_login, self.btn_logout, self.btn_refresh):
            b.pack(side="left", padx=(0, 6))
        ttk.Label(f, wraplength=640, foreground="#666", text=(
            "Как проходит вход: откроется окно терминала и браузер с claude.ai. Войди в "
            "аккаунт и подтверди доступ. Если браузер покажет код, скопируй его в окно "
            "терминала и нажми Enter. Статус здесь обновится сам.")).pack(anchor="w", pady=(14, 0))

    def _show_auth(self, exe, version, logged, desc):
        if not exe:
            self.exe_var.set("Claude Code не найден на этом компьютере.")
            self.auth_var.set("Сначала установи Claude Code — кнопка ниже. "
                              "Установка займёт пару минут.")
            self.btn_install.state(["!disabled"])
            self.btn_login.state(["disabled"])
            self.btn_logout.state(["disabled"])
            return
        self.exe_var.set(f"Claude Code {version}  ·  {exe}")
        self.auth_var.set(desc)
        self.btn_install.state(["disabled"])
        self.btn_login.state(["!disabled"])
        self.btn_logout.state(["!disabled" if logged else "disabled"])

    def refresh_auth(self, then=None):
        self.auth_var.set("Проверяю…")

        def work():
            exe = self._exe()
            if not exe:
                return None, "", False, ""
            return (exe, claude_version(exe)) + auth_status(exe)

        def done(res):
            self._show_auth(*res)
            if res[2]:
                self.app.on_auth(True, res[3])
            if then:
                then(res)
        self._bg(work, done)

    def login(self):
        exe = self._exe()
        if not exe:
            return
        try:
            open_in_terminal([exe, "auth", "login"])
        except Exception as e:
            self.auth_var.set(f"Не удалось запустить вход: {e}")
            return
        self.auth_var.set("Жду входа в браузере…")
        self._login_deadline = time.time() + 600
        self.top.after(4000, self._poll_login)

    def _poll_login(self):
        if not self.alive or not self._login_deadline:
            return

        def done(res):
            if res[2]:
                self._login_deadline = 0
                self._show_auth(*res)
                self.app.on_auth(True, res[3])
            elif time.time() > self._login_deadline:
                self._login_deadline = 0
                self._show_auth(*res)
                self.auth_var.set("Вход так и не завершился. Попробуй ещё раз.")
            else:
                self.auth_var.set("Жду входа в браузере…")
                self.top.after(3000, self._poll_login)
        exe = self._exe()
        self._bg(lambda: (exe, claude_version(exe)) + auth_status(exe), done)

    def logout(self):
        exe = self._exe()
        if not exe or not messagebox.askyesno(
                "Выход", "Выйти из аккаунта Claude в Claude Code?", parent=self.top):
            return
        self.auth_var.set("Выхожу…")
        self._bg(lambda: subprocess.run([exe, "auth", "logout"], capture_output=True, timeout=60,
                                        env=claude_env(), creationflags=NO_WINDOW),
                 lambda _: self.refresh_auth())

    def install(self):
        try:
            open_in_terminal(install_command())
        except Exception as e:
            self.auth_var.set(f"Не удалось запустить установку: {e}")
            return
        self.auth_var.set("Идёт установка в отдельном окне… Как только Claude Code "
                          "появится, здесь станет доступна кнопка входа.")
        self._install_deadline = time.time() + 900
        self.top.after(5000, self._poll_install)

    def _poll_install(self):
        if not self.alive or not self._install_deadline:
            return
        if self._exe():
            self._install_deadline = 0
            self.refresh_auth()
        elif time.time() < self._install_deadline:
            self.top.after(4000, self._poll_install)

    # --- вкладка «Горячие клавиши» ----------------------------------------
    def _tab_hotkeys(self, f):
        ttk.Label(f, wraplength=640, foreground="#666", text=(
            "Нажми «Изменить», затем нужное сочетание. Работают сочетания с Ctrl, Alt "
            "или Win, а также F1–F24. Пока открыто окно настроек, горячие клавиши "
            "отключены.")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        self.hk_vars = {}
        for i, (action, name) in enumerate(ACTION_NAMES, start=1):
            ttk.Label(f, text=name).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=pretty_combo(self.cfg["hotkeys"].get(action, "")))
            self.hk_vars[action] = v
            ttk.Label(f, textvariable=v, width=20, anchor="center", relief="groove",
                      padding=(4, 3)).grid(row=i, column=1, padx=8)
            ttk.Button(f, text="Изменить",
                       command=lambda a=action: self.start_capture(a)).grid(row=i, column=2)
            ttk.Button(f, text="✕", width=3,
                       command=lambda a=action: self.set_hotkey(a, "")).grid(row=i, column=3,
                                                                             padx=(4, 0))
        n = len(ACTION_NAMES) + 1
        self.hk_msg = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.hk_msg, foreground="#b3261e", wraplength=640).grid(
            row=n, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(f, text="Вернуть по умолчанию", command=self.reset_hotkeys).grid(
            row=n + 1, column=0, sticky="w", pady=(8, 0))
        f.columnconfigure(0, weight=1)

    def start_capture(self, action):
        if self.capturing:
            self.hk_vars[self.capturing].set(pretty_combo(self.cfg["hotkeys"].get(self.capturing)))
        self.capturing = action
        self.mods = set()
        self.hk_vars[action].set("нажми сочетание… (Esc — отмена)")
        self.hk_msg.set("")
        self.top.focus_force()

    def _key_press(self, e):
        if not self.capturing:
            return None
        ks = e.keysym
        if ks in _MOD_KEYSYMS:
            if _MOD_KEYSYMS[ks]:
                self.mods.add(_MOD_KEYSYMS[ks])
            return "break"
        action = self.capturing
        if ks == "Escape":
            self.capturing = None
            self.hk_vars[action].set(pretty_combo(self.cfg["hotkeys"].get(action)))
            return "break"
        key = key_name_from_event(e)
        if not key:
            self.hk_msg.set(f"Клавишу «{ks}» назначить нельзя — выбери другую.")
            return "break"
        # Ctrl/Alt/Shift берём из состояния клавиатуры в момент нажатия (не залипают),
        # Win — из отслеживания нажатий (в state его нет)
        mods = mods_from_state(e.state) | ({"win"} & self.mods)
        if not ({"ctrl", "alt", "win"} & mods) and not re.fullmatch(r"f\d+", key):
            self.hk_msg.set("Добавь Ctrl, Alt или Win — иначе клавиша будет мешать обычному "
                            "набору текста. Без модификаторов можно только F1–F24.")
            return "break"
        combo = "+".join([m for m in ("ctrl", "alt", "shift", "win") if m in mods] + [key])
        for other, name in ACTION_NAMES:
            if other != action and self.cfg["hotkeys"].get(other) == combo:
                self.hk_msg.set(f"{pretty_combo(combo)} уже назначено: «{name}».")
                return "break"
        self.set_hotkey(action, combo)
        return "break"

    def _key_release(self, e):
        m = _MOD_KEYSYMS.get(e.keysym)
        if m:
            self.mods.discard(m)

    def set_hotkey(self, action, combo):
        self.cfg["hotkeys"][action] = combo
        self.hk_vars[action].set(pretty_combo(combo))
        if self.capturing == action:
            self.capturing = None
        self.hk_msg.set("")

    def reset_hotkeys(self):
        self.capturing = None
        for action, _ in ACTION_NAMES:
            self.set_hotkey(action, DEFAULT_CONFIG["hotkeys"][action])

    # --- вкладка «Панель» -------------------------------------------------
    def _spin(self, f, row, text, var, lo, hi, step=1, hint=""):
        ttk.Label(f, text=text).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Spinbox(f, from_=lo, to=hi, increment=step, textvariable=var, width=8).grid(
            row=row, column=1, sticky="w", padx=8)
        if hint:
            ttk.Label(f, text=hint, foreground="#777").grid(row=row, column=2, sticky="w")

    def _tab_overlay(self, f):
        o = self.cfg["overlay"]
        self.ov = {
            "width": tk.IntVar(value=o["width"]),
            "max_height": tk.IntVar(value=o["max_height"]),
            "font_size": tk.IntVar(value=o["font_size"]),
            "opacity": tk.DoubleVar(value=o["opacity"]),
            "auto_hide_seconds": tk.IntVar(value=o["auto_hide_seconds"]),
            "corner": tk.StringVar(value=dict(CORNERS).get(o.get("corner"), CORNERS[0][1])),
        }
        self._spin(f, 0, "Ширина, px", self.ov["width"], 250, 1600, 10)
        self._spin(f, 1, "Макс. высота, px", self.ov["max_height"], 100, 1400, 10)
        self._spin(f, 2, "Размер шрифта", self.ov["font_size"], 8, 28)
        ttk.Label(f, text="Непрозрачность").grid(row=3, column=0, sticky="w", pady=4)
        op_lbl = ttk.Label(f, text=f"{int(o['opacity'] * 100)}%")
        ttk.Scale(f, from_=0.3, to=1.0, variable=self.ov["opacity"], length=160,
                  command=lambda v: op_lbl.configure(text=f"{int(float(v) * 100)}%")).grid(
            row=3, column=1, sticky="w", padx=8)
        op_lbl.grid(row=3, column=2, sticky="w")
        ttk.Label(f, text="Угол экрана").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.ov["corner"], state="readonly", width=16,
                     values=[c[1] for c in CORNERS]).grid(row=4, column=1, columnspan=2,
                                                          sticky="w", padx=8)
        self._spin(f, 5, "Прятать через, сек", self.ov["auto_hide_seconds"], 0, 600, 5,
                   "0 — не прятать")
        row = ttk.Frame(f)
        row.grid(row=6, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(row, text="Предпросмотр", command=self.preview).pack(side="left")
        ttk.Button(row, text="Вернуть панель в угол", command=self.reset_pos).pack(side="left",
                                                                                   padx=6)
        ttk.Label(f, wraplength=640, foreground="#666", text=(
            "Если панель перетаскивали, она остаётся на новом месте. Чтобы снова "
            "привязать её к углу, нажми «Вернуть панель в угол».")).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def preview(self):
        err = self._collect()
        if err:
            messagebox.showerror("Настройки", err, parent=self.top)
            return
        ov = self.app.overlay
        ov.apply_config(self.cfg)
        ov.show()
        ov.set_status("предпросмотр")
        ov.set_text("Так будет выглядеть ответ.\n\n• Пункт первый\n• Пункт второй\n\n"
                    "x² + 2x + 1 = (x + 1)²")

    def reset_pos(self):
        self.app.state["overlay_pos"] = None
        self.app.save_state()
        self.preview()

    # --- вкладка «Claude» -------------------------------------------------
    def _tab_claude(self, f):
        ttk.Label(f, text="Модель").grid(row=0, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=self.cfg.get("model") or MODEL_LABEL_DEFAULT)
        ttk.Combobox(f, textvariable=self.model_var, values=MODELS, state="readonly",
                     width=16).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(f, text="sonnet — быстрее, opus — умнее", foreground="#777").grid(
            row=0, column=2, sticky="w")
        self.side_var = tk.IntVar(value=self.cfg.get("max_image_side", 1600))
        self._spin(f, 1, "Размер скриншота, px", self.side_var, 600, 3000, 100,
                   "по длинной стороне")
        ttk.Label(f, text="Текст к скриншоту по горячей клавише:").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.prompt_txt = tk.Text(f, height=4, wrap="word", relief="solid", bd=1,
                                  font=("Segoe UI" if IS_WIN else "Helvetica", 10))
        self.prompt_txt.insert("1.0", self.cfg.get("screenshot_prompt", ""))
        self.prompt_txt.grid(row=3, column=0, columnspan=3, sticky="ew")
        ttk.Label(f, text="Путь к Claude Code (если не находится сам):").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(12, 2))
        self.path_var = tk.StringVar(value=self.cfg.get("claude_path", ""))
        prow = ttk.Frame(f)
        prow.grid(row=5, column=0, columnspan=3, sticky="ew")
        ttk.Entry(prow, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(prow, text="Обзор…", command=self._browse).pack(side="left", padx=(6, 0))
        ttk.Button(f, text="Открыть постоянные инструкции (CLAUDE.md)",
                   command=lambda: open_path(WORK / "CLAUDE.md")).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Label(f, wraplength=640, foreground="#666", text=(
            "В CLAUDE.md — то, что Claude учитывает в каждом ответе: язык, стиль, "
            "краткость. Изменения действуют с нового диалога.")).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        f.columnconfigure(2, weight=1)

    def _browse(self):
        p = filedialog.askopenfilename(parent=self.top, title="Где находится claude",
                                       filetypes=[("claude", "claude*"), ("Все файлы", "*")])
        if p:
            self.path_var.set(p)

    # --- сохранение -------------------------------------------------------
    def _collect(self):
        def num(var, lo, hi, name, cast=int):
            try:
                v = cast(var.get())
            except (tk.TclError, ValueError):
                raise ValueError(f"«{name}»: нужно число")
            return max(lo, min(hi, v))
        try:
            o = self.cfg["overlay"]
            o["width"] = num(self.ov["width"], 250, 1600, "Ширина")
            o["max_height"] = num(self.ov["max_height"], 100, 1400, "Макс. высота")
            o["font_size"] = num(self.ov["font_size"], 8, 28, "Размер шрифта")
            o["opacity"] = round(num(self.ov["opacity"], 0.3, 1.0, "Непрозрачность", float), 2)
            o["auto_hide_seconds"] = num(self.ov["auto_hide_seconds"], 0, 600, "Прятать через")
            o["corner"] = {v: k for k, v in CORNERS}.get(self.ov["corner"].get(), "top-right")
            self.cfg["max_image_side"] = num(self.side_var, 600, 3000, "Размер скриншота")
        except ValueError as e:
            return str(e)
        m = self.model_var.get()
        self.cfg["model"] = "" if m == MODEL_LABEL_DEFAULT else m
        self.cfg["screenshot_prompt"] = (self.prompt_txt.get("1.0", "end-1c").strip()
                                         or DEFAULT_CONFIG["screenshot_prompt"])
        self.cfg["claude_path"] = self.path_var.get().strip()
        return None

    def save(self):
        err = self._collect()
        if err:
            messagebox.showerror("Настройки", err, parent=self.top)
            return
        self.app.apply_settings(self.cfg)
        self._close()

    def cancel(self):
        self.app.overlay.apply_config(self.app.cfg)  # откатить предпросмотр
        self._close()

    def _close(self):
        self.alive = False
        self.capturing = None
        self.top.destroy()
        self.app.settings = None
        self.app.start_hotkeys()


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
# Главное окно (чат)
# ----------------------------------------------------------------------------
class App:
    def __init__(self):
        WORK.mkdir(exist_ok=True)
        FILES.mkdir(exist_ok=True)
        md = WORK / "CLAUDE.md"
        if not md.exists():
            md.write_text(DEFAULT_CLAUDE_MD, encoding="utf-8")

        self.cfg = deep_merge(DEFAULT_CONFIG, load_json(CONFIG_PATH, {}))
        save_json(CONFIG_PATH, self.cfg)
        self.state = load_json(STATE_PATH, {}) or {}
        self.state.setdefault("history", [])

        self.q = queue.Queue()
        self.runner = ClaudeRunner(self.q.put)
        self.busy = False
        self.rid = None
        self.cur_text = ""
        self.to_overlay = False
        self.attachments = []
        self._render_job = None
        self.settings = None
        self.hotkeys = None

        if IS_WIN:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

        self.root = root = tk.Tk()
        root.title("Claude поверх экрана — чат")
        root.geometry("620x700")
        root.minsize(420, 420)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._build_ui()

        self.overlay = Overlay(root, self.cfg, self.state, self.save_state)

        self.start_hotkeys()

        for item in self.state["history"]:
            self._append(item["role"], item["text"])
        self._check_claude()
        root.after(30, self._poll)

    # --- интерфейс -------------------------------------------------------
    def _build_ui(self):
        r = self.root
        fam = "Segoe UI" if IS_WIN else "Helvetica"
        top = ttk.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Модель:").pack(side="left")
        self.model_var = tk.StringVar(value=self.cfg.get("model") or MODEL_LABEL_DEFAULT)
        cb = ttk.Combobox(top, textvariable=self.model_var, width=13, state="readonly",
                          values=MODELS)
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", self._model_changed)
        ttk.Button(top, text="Новый диалог", command=self.new_dialog).pack(side="left")
        ttk.Button(top, text="Папка", command=self._open_folder).pack(side="left", padx=4)
        ttk.Button(top, text="⚙ Настройки", command=self.open_settings).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var, foreground="#777").pack(side="right")

        mid = ttk.Frame(r, padding=(8, 0))
        self.chat = tk.Text(mid, wrap="word", font=(fam, 10), padx=8, pady=6, width=40,
                            height=10, relief="flat", bg="#fbfaf8")
        sb = ttk.Scrollbar(mid, command=self.chat.yview)
        self.chat.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure("you", foreground="#1f5fbf", font=(fam, 10, "bold"))
        self.chat.tag_configure("claude", foreground="#c15f3c", font=(fam, 10, "bold"))
        self.chat.tag_configure("sys", foreground="#888", font=(fam, 9, "italic"))
        self.chat.bind("<Key>", self._readonly_keys)

        self.att_var = tk.StringVar(value="")
        att = ttk.Frame(r, padding=(8, 2))
        ttk.Label(att, textvariable=self.att_var, foreground="#555").pack(side="left")
        self.att_clear = ttk.Button(att, text="убрать", command=self._clear_att)

        bot = ttk.Frame(r, padding=8)
        btns = ttk.Frame(bot)
        btns.pack(side="right", padx=(6, 0))
        self.input = tk.Text(bot, height=4, width=30, wrap="word", font=(fam, 10),
                             relief="solid", bd=1)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._enter)
        ttk.Button(btns, text="Отправить", command=self.send_chat).pack(fill="x")
        ttk.Button(btns, text="📎 Файл", command=self._attach).pack(fill="x", pady=2)
        ttk.Button(btns, text="📷 + скрин", command=lambda: self.send_chat(True)).pack(fill="x")
        ttk.Button(btns, text="■ Стоп", command=self.stop).pack(fill="x", pady=(2, 0))

        self.help_label = ttk.Label(r, text=self._help_text(), wraplength=600, foreground="#777",
                                    padding=(8, 0, 8, 8), font=(fam, 8))
        self.help_label.pack(side="bottom", fill="x")
        bot.pack(side="bottom", fill="x")
        att.pack(side="bottom", fill="x")
        mid.pack(fill="both", expand=True)

    def _help_text(self):
        hk = self.cfg["hotkeys"]
        short = {"shot": "скрин экрана", "region": "скрин области", "toggle": "скрыть/показать панель",
                 "scroll_up": "прокрутка вверх", "scroll_down": "прокрутка вниз",
                 "move": "двигать панель", "chat": "этот чат", "stop": "стоп"}
        items = [f"{pretty_combo(hk[a])} — {short[a]}" for a, _ in ACTION_NAMES if hk.get(a)]
        return " · ".join(items) + ".   Enter — отправить, Shift+Enter — новая строка."

    # --- настройки и горячие клавиши -------------------------------------
    def open_settings(self, tab="account"):
        if self.settings and self.settings.alive:
            self.settings.lift(tab)
        else:
            self.settings = SettingsWindow(self, tab)

    def apply_settings(self, cfg):
        self.cfg = cfg
        save_json(CONFIG_PATH, cfg)
        self.overlay.apply_config(cfg)
        self.model_var.set(cfg.get("model") or MODEL_LABEL_DEFAULT)
        self.help_label.configure(text=self._help_text())

    def start_hotkeys(self):
        self.stop_hotkeys()
        hk = self.cfg["hotkeys"]
        self.hotkeys = WinHotkeys(hk, self.q.put) if IS_WIN else PynputHotkeys(hk, self.q.put)
        self.hotkeys.start()

    def stop_hotkeys(self):
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
            self.hotkeys = None

    def on_auth(self, logged, desc):
        if logged and not getattr(self, "_logged_in", False):
            self._logged_in = True
            self._sys(desc.splitlines()[0])

    def _readonly_keys(self, e):
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a", "insert"):
            return None
        if e.keysym in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    def _enter(self, e):
        if e.state & 0x1:  # Shift+Enter
            return None
        self.send_chat()
        return "break"

    def _model_changed(self, _=None):
        v = self.model_var.get()
        self.cfg["model"] = "" if v == MODEL_LABEL_DEFAULT else v
        save_json(CONFIG_PATH, self.cfg)

    def _open_folder(self):
        open_path(WORK)

    # --- журнал чата -----------------------------------------------------
    def _append(self, role, text):
        label = {"you": "Вы", "claude": "Claude", "sys": ""}[role]
        if role == "sys":
            self.chat.insert("end", text + "\n\n", "sys")
        else:
            self.chat.insert("end", label + "\n", role)
            self.chat.insert("end", (clean_md(text) if role == "claude" else text) + "\n\n")
        self.chat.see("end")

    def _remember(self, role, text):
        self.state["history"].append({"role": role, "text": text})
        self.state["history"] = self.state["history"][-300:]
        self.save_state()

    def _sys(self, text):
        self._append("sys", text)

    def save_state(self):
        try:
            save_json(STATE_PATH, self.state)
        except Exception:
            pass

    # --- вложения --------------------------------------------------------
    def _attach(self):
        paths = filedialog.askopenfilenames(title="Файлы для Claude")
        for p in paths:
            self.attachments.append(Path(p))
        self._refresh_att()

    def _clear_att(self):
        self.attachments = []
        self._refresh_att()

    def _refresh_att(self):
        if self.attachments:
            self.att_var.set("📎 " + ", ".join(p.name for p in self.attachments))
            self.att_clear.pack(side="left", padx=6)
        else:
            self.att_var.set("")
            self.att_clear.pack_forget()

    def _prepare_attachments(self):
        blocks, notes = [], []
        for src in self.attachments:
            try:
                dest = copy_unique(src, FILES)
                ext = dest.suffix.lower()
                if ext in IMAGE_EXT:
                    blocks.append({"type": "text", "text": f"Картинка: {dest.name}"})
                    blocks.append(image_to_block(Image.open(dest), self.cfg["max_image_side"]))
                elif ext == ".docx":
                    txt = dest.with_suffix(".docx.txt")
                    txt.write_text(docx_to_text(dest), encoding="utf-8")
                    notes.append(f"{dest.name} (текст извлечён в {txt})")
                else:
                    notes.append(str(dest))
            except Exception as e:
                self._sys(f"Не удалось подготовить {src.name}: {e}")
        return blocks, notes

    # --- отправка --------------------------------------------------------
    def send_chat(self, with_screenshot=False):
        text = self.input.get("1.0", "end-1c").strip()
        if not text and not self.attachments and not with_screenshot:
            return
        if self.busy:
            self.status_var.set("Claude ещё отвечает…")
            return
        if with_screenshot:
            self.capture(lambda img: self._send_chat(text, img), region=False)
        else:
            self._send_chat(text, None)

    def _send_chat(self, text, img):
        blocks, notes = self._prepare_attachments()
        prompt = text or ("Посмотри на скриншот." if img else "Посмотри на прикреплённые файлы.")
        if notes:
            prompt += ("\n\nЯ прикрепил файлы (прочитай их инструментом Read):\n"
                       + "\n".join("- " + n for n in notes))
        content = [{"type": "text", "text": prompt}] + blocks
        if img is not None:
            content.append(image_to_block(img, self.cfg["max_image_side"]))
        shown = text
        if self.attachments:
            shown += ("\n" if shown else "") + "📎 " + ", ".join(p.name for p in self.attachments)
        if img is not None:
            shown += ("\n" if shown else "") + "📷 скриншот"
        self.input.delete("1.0", "end")
        self._clear_att()
        self.start_request(content, shown, to_overlay=img is not None)

    def hotkey_shot(self, region):
        if self.busy:
            self.overlay.show()
            self.overlay.set_status(f"ещё отвечаю… ({pretty_combo(self.cfg['hotkeys']['stop'])} — стоп)")
            return
        prompt = self.cfg["screenshot_prompt"]
        self.capture(lambda img: self.start_request(
            [{"type": "text", "text": prompt},
             image_to_block(img, self.cfg["max_image_side"])],
            "📷 скриншот" + (" области" if region else ""), to_overlay=True), region=region)

    def capture(self, callback, region):
        was_overlay = self.overlay.visible
        chat_visible = self.root.state() == "normal"
        self.overlay.hide()
        if chat_visible:
            self.root.withdraw()
        px, py = self.root.winfo_pointerxy()

        def restore_chat():
            if chat_visible:
                self.root.deiconify()

        def do():
            try:
                img, mon = grab_screen_at(px, py)
            except Exception as e:
                restore_chat()
                self._sys(f"Не удалось сделать скриншот: {e}")
                return
            restore_chat()
            if not region:
                callback(img)
                return

            def cancel():
                if was_overlay:
                    self.overlay.show()
            RegionSelector(self.root, img, mon, callback, cancel)

        self.root.after(200, do)

    def start_request(self, content, shown, to_overlay):
        exe = find_claude(self.cfg.get("claude_path", ""))
        if not exe:
            self._sys("⚠ Claude Code не найден. Установи его: ⚙ Настройки → Аккаунт.")
            self.open_settings("account")
            return
        self.busy = True
        self.cur_text = ""
        self.to_overlay = to_overlay
        self._append("you", shown)
        self._remember("you", shown)
        self.chat.insert("end", "Claude\n", "claude")
        self.chat.mark_set("ans", "end-1c")
        self.chat.mark_gravity("ans", "left")
        self.chat.insert("end", "…\n\n")
        self.chat.see("end")
        self.status_var.set("думаю…")
        if to_overlay:
            self.overlay.cancel_hide()
            self.overlay.show()
            self.overlay.set_text("")
            self.overlay.set_status("думаю…")
        self.rid = self.runner.start(exe, content, self.state.get("session_id"), self.cfg.get("model"))

    def stop(self):
        if self.busy:
            self.runner.stop()

    def new_dialog(self):
        if self.busy:
            self.runner.stop()
        self.rid = None
        self.busy = False
        self.status_var.set("")
        self.state["session_id"] = None
        self.state["history"] = []
        self.save_state()
        self.chat.delete("1.0", "end")
        self.overlay.set_text("")
        self.overlay.hide()
        self._sys("Новый диалог. Расскажи, что будем делать, и пришли нужные файлы.")

    # --- события из потоков ---------------------------------------------
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "hotkey":
            self._hotkey(ev[1])
            return
        if kind == "hotkey_fail":
            self._sys("Не удалось назначить горячие клавиши: " + "; ".join(ev[1])
                      + ". Поменяй их: ⚙ Настройки → Горячие клавиши.")
            return
        if kind == "auth":
            _, exe, version, logged, desc = ev
            self.status_var.set(version)
            if not exe:
                self._sys("⚠ Claude Code не найден. Установи его: ⚙ Настройки → Аккаунт → "
                          "«Установить Claude Code».")
                self.open_settings("account")
            elif not logged:
                self._sys("⚠ Нужно войти в аккаунт Claude: ⚙ Настройки → Аккаунт → «Войти».")
                self.open_settings("account")
            else:
                self.on_auth(True, desc)
            return
        rid = ev[1]
        if rid != self.rid:
            return
        if kind == "session":
            self.state["session_id"] = ev[2]
        elif kind == "status":
            self.status_var.set(ev[2])
            if self.to_overlay:
                self.overlay.set_status(ev[2])
        elif kind == "msg_start":
            if self.cur_text and not self.cur_text.endswith("\n\n"):
                self.cur_text += "\n\n"
        elif kind == "delta":
            self.cur_text += ev[2]
            self._schedule_render()
        elif kind == "done":
            text, sid, is_err = ev[2], ev[3], ev[4]
            if sid:
                self.state["session_id"] = sid
            if is_err and not text.startswith("⏹"):
                text = self._explain_error(text)
            self._finish(text or self.cur_text)
        elif kind == "fail":
            self._finish(self._explain_error(ev[2]))

    def _schedule_render(self):
        if not self._render_job:
            self._render_job = self.root.after(50, self._render)

    def _render(self, final=False):
        self._render_job = None
        self.chat.delete("ans", "end-1c")
        self.chat.insert("end", clean_md(self.cur_text) + ("\n\n" if final else " ▍\n\n"))
        self.chat.see("end")
        if self.to_overlay:
            self.overlay.set_text(self.cur_text, scroll_end=not final)
            self.overlay.set_status("" if final else "пишу…")

    def _finish(self, text):
        if self._render_job:
            self.root.after_cancel(self._render_job)
        self.cur_text = text
        self._render(final=True)
        self._remember("claude", text)
        self.busy = False
        self.status_var.set("")
        if self.to_overlay:
            self.overlay.schedule_hide()

    def _explain_error(self, text):
        t = (text or "").strip()
        low = t.lower()
        if "no conversation found" in low or ("session" in low and "not found" in low):
            self.state["session_id"] = None
            self.save_state()
            return "⚠ Старая сессия не найдена — начат новый диалог. Повтори запрос."
        if any(k in low for k in ("login", "log in", "/login", "authenticat", "oauth", "invalid api key")):
            self._logged_in = False
            self.root.after(100, lambda: self.open_settings("account"))
            return ("⚠ Не выполнен вход в аккаунт Claude. Открой ⚙ Настройки → Аккаунт → "
                    "«Войти в аккаунт Claude».\n\n" + t)
        if "limit" in low:
            return "⚠ Похоже, исчерпан лимит подписки на текущее окно.\n\n" + t
        return "⚠ " + (t or "Неизвестная ошибка")

    # --- горячие клавиши -------------------------------------------------
    def _hotkey(self, action):
        if action == "shot":
            self.hotkey_shot(region=False)
        elif action == "region":
            self.hotkey_shot(region=True)
        elif action == "toggle":
            self.overlay.cancel_hide()
            self.overlay.toggle()
        elif action == "scroll_up":
            self.overlay.scroll(-1)
        elif action == "scroll_down":
            self.overlay.scroll(1)
        elif action == "move":
            self.overlay.toggle_move()
        elif action == "stop":
            self.stop()
        elif action == "chat":
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
            self.input.focus_set()

    def _check_claude(self):
        path = self.cfg.get("claude_path", "")

        def check():
            exe = find_claude(path)
            if not exe:
                self.q.put(("auth", None, "", False, ""))
                return
            self.q.put(("auth", exe, claude_version(exe)) + auth_status(exe))
        threading.Thread(target=check, daemon=True).start()
        if not self.state["history"]:
            self._sys("Готово. Напиши здесь, что нужно делать со скриншотами, и прикрепи файлы, "
                      "если нужны. Потом сверни окно и жми горячую клавишу.")

    def quit(self):
        try:
            self.runner.stop()
            self.stop_hotkeys()
        finally:
            self.save_state()
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
