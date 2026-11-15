#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Управление» — Claude поверх экрана.

Здесь: чаты (какой из них использует оверлей), загрузка контекста — переписка
и файлы до начала работы, настройки, запуск и остановка оверлея.
Сам оверлей (горячие клавиши + панель) — отдельная программа overlay.py.
"""
import json
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image

from core import (
    ACTION_NAMES, ANCHORS, CHATS, CONFIG_PATH, DEFAULT_CONFIG, DEFAULT_TITLE, FILES, IMAGE_EXT,
    IS_WIN, MANAGER_TITLE, MODELS, MODEL_LABEL_DEFAULT, NO_WINDOW, WHO_NAMES, WORK,
    _MOD_KEYSYMS, ClaudeRunner, Overlay, acquire_chat, append_messages, auth_status,
    chat_busy_by, chats_signature, claude_env, claude_version, clear_pid,
    copy_unique, create_chat, delete_chat, docx_to_text, ensure_dirs, find_claude,
    get_active_chat, image_to_block, install_command, key_name_from_event, launch,
    list_chats, load_chat, load_config, load_state, migrate_old_state, mods_from_state,
    mtime, open_in_terminal, open_path, pretty_combo, release_chat, resolve_error,
    running_pid, save_json, send_command, set_active_chat, set_dpi_aware,
    set_hotkeys_paused, take_commands, update_chat, update_state, write_pid,
    THEME_LABELS, monitor_choices, DISPLAY_MODES, ScreenMarks, pick_monitor,
    MATH, MATH_MODES, insert_rich, math_note,
)

FAM = "Segoe UI" if IS_WIN else "Helvetica"


class SettingsWindow:
    def __init__(self, app, tab="account"):
        self.app = app
        self.cfg = json.loads(json.dumps(app.cfg))  # рабочая копия
        self.alive = True
        self.capturing = None
        self.mods = set()
        st = load_state()  # свежая позиция панели (её могли перетащить в оверлее)
        app.panel_state.clear()
        app.panel_state.update(st)
        self._login_deadline = 0
        self._install_deadline = 0
        set_hotkeys_paused(True)  # оверлей отпускает клавиши, пока открыты настройки

        t = self.top = tk.Toplevel(app.root)
        t.title("Настройки")
        sh = t.winfo_screenheight()
        t.geometry(f"820x{max(480, min(780, sh - 120))}")
        t.minsize(600, 420)
        t.transient(app.root)
        t.protocol("WM_DELETE_WINDOW", self.cancel)
        t.bind("<KeyPress>", self._key_press)
        t.bind("<KeyRelease>", self._key_release)

        # кнопки внизу упаковываются первыми — их никогда не вытеснит содержимое
        bar = ttk.Frame(t, padding=(8, 0, 8, 8))
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="Отмена", command=self.cancel).pack(side="right")
        ttk.Button(bar, text="Сохранить", command=self.save).pack(side="right", padx=6)

        nb = self.nb = ttk.Notebook(t)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs = {}
        for key, title, build in (("account", "Аккаунт", self._tab_account),
                                  ("hotkeys", "Горячие клавиши", self._tab_hotkeys),
                                  ("look", "Вид панели", self._tab_look),
                                  ("position", "Положение", self._tab_position),
                                  ("claude", "Claude", self._tab_claude)):
            outer, inner = self._scroll_tab(nb)
            nb.add(outer, text=title)
            build(inner)
            self.tabs[key] = outer
        self.select(tab)
        self._live_job = None
        for v in self.ov.values():
            v.trace_add("write", self._live)
        for k, v in self.pos.items():
            v.trace_add("write", self._live if k in ("display_mode", "marks_seconds", "marks_opacity")
                        else self._pos_changed)
        self.refresh_auth()
        self._ready = True

    def _scroll_tab(self, nb):
        """Вкладка с прокруткой: если содержимое не влезает по высоте, появляется полоса."""
        outer = ttk.Frame(nb)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0, bg=bg)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=14)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def update(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            need = inner.winfo_reqheight() > canvas.winfo_height() > 1
            if need and not vsb.winfo_ismapped():
                vsb.pack(side="right", fill="y", before=canvas)
            elif not need and vsb.winfo_ismapped():
                vsb.pack_forget()
                canvas.yview_moveto(0)

        def wheel(e):
            if inner.winfo_reqheight() <= canvas.winfo_height():
                return
            if e.num == 4:
                step = -1
            elif e.num == 5:
                step = 1
            else:
                step = -1 if e.delta > 0 else 1
            canvas.yview_scroll(step * 3, "units")

        inner.bind("<Configure>", update)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), update()))
        outer.bind("<Enter>", lambda e: [canvas.bind_all(ev, wheel) for ev in
                                         ("<MouseWheel>", "<Button-4>", "<Button-5>")])
        outer.bind("<Leave>", lambda e: [canvas.unbind_all(ev) for ev in
                                         ("<MouseWheel>", "<Button-4>", "<Button-5>")])
        return outer, inner

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
            "оверлея на паузе.")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
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

    def _tab_look(self, f):
        o = self.cfg["overlay"]
        self.ov = {
            "theme": tk.StringVar(value=dict(THEME_LABELS).get(o.get("theme"), "Тёмная")),
            "width": tk.IntVar(value=o["width"]),
            "max_height": tk.IntVar(value=o["max_height"]),
            "font_size": tk.IntVar(value=o["font_size"]),
            "opacity": tk.DoubleVar(value=o["opacity"]),
            "auto_hide_seconds": tk.IntVar(value=o["auto_hide_seconds"]),
        }
        ttk.Label(f, text="Тема").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.ov["theme"], state="readonly", width=16,
                     values=[t[1] for t in THEME_LABELS]).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(f, text="«Как в системе» — по теме Windows", foreground="#777").grid(
            row=0, column=2, sticky="w")
        self._spin(f, 1, "Ширина, px", self.ov["width"], 250, 1600, 10)
        self._spin(f, 2, "Макс. высота, px", self.ov["max_height"], 100, 1400, 10)
        self._spin(f, 3, "Размер шрифта", self.ov["font_size"], 8, 28)
        ttk.Label(f, text="Непрозрачность").grid(row=4, column=0, sticky="w", pady=4)
        op_lbl = ttk.Label(f, text=f"{int(o['opacity'] * 100)}%")
        ttk.Scale(f, from_=0.3, to=1.0, variable=self.ov["opacity"], length=160,
                  command=lambda v: op_lbl.configure(text=f"{int(float(v) * 100)}%")).grid(
            row=4, column=1, sticky="w", padx=8)
        op_lbl.grid(row=4, column=2, sticky="w")
        self._spin(f, 5, "Прятать через, сек", self.ov["auto_hide_seconds"], 0, 600, 5,
                   "0 — не прятать")
        ttk.Button(f, text="Предпросмотр", command=self.preview).grid(
            row=6, column=0, sticky="w", pady=(14, 0))
        ttk.Label(f, wraplength=640, foreground="#666", text=(
            "Изменения сразу видны в панели предпросмотра. Запущенный оверлей "
            "подхватит их после «Сохранить».")).grid(row=7, column=0, columnspan=3, sticky="w",
                                                     pady=(10, 0))

    def _tab_position(self, f):
        o = self.cfg["overlay"]
        self.monitors = monitor_choices()
        mon_label = dict(self.monitors).get(str(o.get("monitor", "cursor")), self.monitors[0][1])
        self.pos = {
            "monitor": tk.StringVar(value=mon_label),
            "anchor": tk.StringVar(value=o.get("anchor", "top-right")),
            "offset_x": tk.IntVar(value=o.get("offset_x", 24)),
            "offset_y": tk.IntVar(value=o.get("offset_y", 24)),
            "respect_taskbar": tk.BooleanVar(value=o.get("respect_taskbar", True)),
            "display_mode": tk.StringVar(value=o.get("display_mode", "panel")),
            "marks_seconds": tk.IntVar(value=o.get("marks_seconds", 0)),
            "marks_opacity": tk.DoubleVar(value=o.get("marks_opacity", 0.9)),
        }
        mf = ttk.LabelFrame(f, text="Как показывать ответ", padding=(10, 6))
        mf.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        for val, title in DISPLAY_MODES:
            ttk.Radiobutton(mf, text=title, value=val,
                            variable=self.pos["display_mode"]).pack(anchor="w")
        ttk.Label(mf, wraplength=680, foreground="#666", text=(
            "Пометки: Claude обводит нужные места рамками с номерами и пишет короткие подписи "
            "рядом — ответы у вопросов, «нажми сюда» у кнопок, ошибки в строках. Точность — "
            "до нескольких десятков пикселей, лучше всего со скриншотом области. Панель ниже "
            "тогда показывает только «думаю…» и ответ текстом, если пометки не получились. "
            "Скрыть/показать пометки — та же клавиша, что и панель.")).pack(anchor="w", pady=(4, 0))
        sr = ttk.Frame(mf)
        sr.pack(anchor="w", pady=(6, 0))
        ttk.Label(sr, text="Убирать пометки через, сек").pack(side="left")
        ttk.Spinbox(sr, from_=0, to=600, increment=5, width=6,
                    textvariable=self.pos["marks_seconds"]).pack(side="left", padx=8)
        ttk.Label(sr, text="0 — пока не скрыть клавишей", foreground="#777").pack(side="left")
        orow = ttk.Frame(mf)
        orow.pack(anchor="w", pady=(6, 0))
        ttk.Label(orow, text="Непрозрачность пометок").pack(side="left")
        mo_lbl = ttk.Label(orow, text=f"{int(o.get('marks_opacity', 0.9) * 100)}%", width=5)
        ttk.Scale(orow, from_=0.2, to=1.0, variable=self.pos["marks_opacity"], length=160,
                  command=lambda v: mo_lbl.configure(text=f"{int(float(v) * 100)}%")).pack(
            side="left", padx=8)
        mo_lbl.pack(side="left")
        ttk.Label(f, text="Панель с текстом", font=(FAM, 10, "bold")).grid(
            row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Монитор").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.pos["monitor"], state="readonly", width=34,
                     values=[m[1] for m in self.monitors]).grid(row=3, column=1, columnspan=2,
                                                               sticky="w", padx=8)

        ttk.Label(f, text="Где на экране").grid(row=4, column=0, sticky="nw", pady=(10, 4))
        screen = tk.Frame(f, bg="#9a958d", padx=3, pady=3)
        screen.grid(row=4, column=1, sticky="w", padx=8, pady=(10, 4))
        inner = tk.Frame(screen, bg="#e9e6e0")
        inner.pack()
        for i, (val, arrow, _) in enumerate(ANCHORS):
            tk.Radiobutton(inner, text=arrow, value=val, variable=self.pos["anchor"],
                           indicatoron=0, width=5, font=(FAM, 12), relief="flat",
                           bg="#e9e6e0", activebackground="#f3d9cc", selectcolor="#d97757",
                           bd=1, cursor="hand2").grid(row=i // 3, column=i % 3, padx=2, pady=2)
        self.anchor_lbl = ttk.Label(f, text="", foreground="#555")
        self.anchor_lbl.grid(row=4, column=2, sticky="w")

        self._spin(f, 5, "Отступ по горизонтали, px", self.pos["offset_x"], -4000, 4000, 4,
                   "от края; при центре — сдвиг вправо")
        self._spin(f, 6, "Отступ по вертикали, px", self.pos["offset_y"], -4000, 4000, 4,
                   "от края; при центре — сдвиг вниз")
        cb = ttk.Checkbutton(f, text="Не заходить на панель задач",
                             variable=self.pos["respect_taskbar"])
        cb.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        if not IS_WIN:
            cb.state(["disabled"])

        self.drag_var = tk.StringVar(value="")
        drow = ttk.Frame(f)
        drow.grid(row=8, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Label(drow, textvariable=self.drag_var, wraplength=480, foreground="#666").pack(
            side="left")
        self.drag_btn = ttk.Button(drow, text="Вернуть к привязке", command=self.reset_pos)
        ttk.Button(f, text="Предпросмотр", command=self.preview).grid(
            row=9, column=0, sticky="w", pady=(14, 0))
        f.columnconfigure(2, weight=1)
        self.reset_drag = False
        self._update_drag_note()

    def _update_drag_note(self):
        a = self.pos["anchor"].get()
        self.anchor_lbl.configure(text=dict((v, t) for v, _, t in ANCHORS).get(a, ""))
        dragged = bool(self.app.panel_state.get("overlay_pos")) and not self.reset_drag
        move = pretty_combo(self.cfg["hotkeys"].get("move", ""))
        if dragged:
            self.drag_var.set("Сейчас панель стоит там, куда её перетащили — привязка не "
                              "действует. Выбери место выше или нажми:")
            self.drag_btn.pack(side="left", padx=6)
        else:
            self.drag_var.set(f"Панель также можно перетащить мышью: {move} в оверлее."
                              if move != "— выкл —" else "")
            self.drag_btn.pack_forget()

    def _live(self, *_):
        """Живой предпросмотр при изменении вида или положения."""
        if not getattr(self, "_ready", False):
            return
        if self._live_job:
            self.top.after_cancel(self._live_job)
        self._live_job = self.top.after(150, lambda: self.preview(silent=True))

    def _pos_changed(self, *_):
        if getattr(self, "_ready", False):
            self.reset_drag = True  # выбрал место вручную — отменяем перетаскивание
            self.app.panel_state["overlay_pos"] = None
        self._update_drag_note()
        self._live()

    def preview(self, silent=False):
        self._live_job = None
        err = self._collect()
        if err:
            if not silent:
                messagebox.showerror("Настройки", err, parent=self.top)
            return
        ov = self.app.preview_panel()
        if self.reset_drag:
            self.app.panel_state["overlay_pos"] = None
        ov.apply_config(self.cfg)
        ov.show()
        if self.cfg["overlay"].get("display_mode") == "screen":
            ov.set_status("предпросмотр · пометки на экране")
            ov.set_text("")
            self.app.preview_marks(self.cfg)
        else:
            self.app.hide_preview_marks()
            ov.set_status("предпросмотр")
            ov.set_text("Так будет выглядеть ответ.\n\n• Пункт первый\n• Пункт второй\n\n"
                        "x² + 2x + 1 = (x + 1)²")

    def reset_pos(self):
        self.reset_drag = True
        self.app.panel_state["overlay_pos"] = None
        self._update_drag_note()
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
        ttk.Label(f, text="Формулы").grid(row=8, column=0, sticky="w", pady=(14, 4))
        self.math_var = tk.StringVar(value=dict(MATH_MODES).get(self.cfg.get("math", "latex"),
                                                                MATH_MODES[0][1]))
        ttk.Combobox(f, textvariable=self.math_var, values=[m[1] for m in MATH_MODES],
                     state="readonly", width=26).grid(row=8, column=1, columnspan=2, sticky="w",
                                                      padx=8, pady=(14, 4))
        ok = MATH._load()
        ttk.Label(f, wraplength=680, foreground="#666" if ok else "#b3261e", text=(
            "Формулы LaTeX рисуются картинками в панели и в переписке: дроби, корни, "
            "векторы, интегралы. В пометках по экрану — простым текстом."
            if ok else "Для рисования формул нужна библиотека matplotlib — запусти install.bat "
            "ещё раз. Пока формулы показываются как текст LaTeX.")).grid(
            row=9, column=0, columnspan=3, sticky="w")
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
            o["theme"] = {v: k for k, v in THEME_LABELS}.get(self.ov["theme"].get(), "dark")
            o["monitor"] = {v: k for k, v in self.monitors}.get(self.pos["monitor"].get(), "cursor")
            o["anchor"] = self.pos["anchor"].get() or "top-right"
            o["offset_x"] = num(self.pos["offset_x"], -4000, 4000, "Отступ по горизонтали")
            o["offset_y"] = num(self.pos["offset_y"], -4000, 4000, "Отступ по вертикали")
            o["respect_taskbar"] = bool(self.pos["respect_taskbar"].get())
            o["display_mode"] = self.pos["display_mode"].get() or "panel"
            o["marks_seconds"] = num(self.pos["marks_seconds"], 0, 600, "Убирать пометки через")
            o["marks_opacity"] = round(num(self.pos["marks_opacity"], 0.2, 1.0,
                                           "Непрозрачность пометок", float), 2)
            self.cfg["max_image_side"] = num(self.side_var, 600, 3000, "Размер скриншота")
        except ValueError as e:
            return str(e)
        m = self.model_var.get()
        self.cfg["model"] = "" if m == MODEL_LABEL_DEFAULT else m
        self.cfg["math"] = {v: k for k, v in MATH_MODES}.get(self.math_var.get(), "latex")
        self.cfg["screenshot_prompt"] = (self.prompt_txt.get("1.0", "end-1c").strip()
                                         or DEFAULT_CONFIG["screenshot_prompt"])
        self.cfg["claude_path"] = self.path_var.get().strip()
        return None

    def save(self):
        err = self._collect()
        if err:
            messagebox.showerror("Настройки", err, parent=self.top)
            return
        if self.reset_drag:
            update_state(overlay_pos=None)
        self.app.apply_settings(self.cfg)
        self._close()

    def cancel(self):
        self._close()

    def _close(self):
        if self._live_job:
            self.top.after_cancel(self._live_job)
        self.alive = False
        self.capturing = None
        self.top.destroy()
        self.app.settings = None
        self.app.hide_preview()
        set_hotkeys_paused(False)


# ----------------------------------------------------------------------------
# Главное окно «Управление»
# ----------------------------------------------------------------------------
class Manager:
    def __init__(self):
        ensure_dirs()
        migrate_old_state()
        if running_pid("manager"):
            send_command("focus", "manager")
            focus_window_safe()
            sys.exit(0)
        write_pid("manager")
        set_dpi_aware()

        self.cfg = load_config()
        save_json(CONFIG_PATH, self.cfg)
        self.q = queue.Queue()
        self.runner = ClaudeRunner(self.q.put)
        self.busy = False
        self.rid = None
        self.cur_text = ""
        self.attachments = []
        self.settings = None
        self._render_job = None
        self._preview = None
        self._preview_marks = None
        self.panel_state = load_state()
        self._logged_in = False

        self.chat = get_active_chat()
        self.chat_mtime = 0.0
        self.chats_sig = None
        self.chat_ids = []

        MATH.preload()
        self.root = root = tk.Tk()
        root.title(MANAGER_TITLE)
        root.geometry("980x700")
        root.minsize(720, 480)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._build_ui()
        self._refresh_chat_list(force=True)
        self._show_chat(self.chat["id"])
        self._check_claude()
        root.after(30, self._poll_queue)
        root.after(300, self._poll_files)

    # --- интерфейс ---------------------------------------------------------
    def _build_ui(self):
        r = self.root
        top = ttk.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Оверлей:").pack(side="left")
        self.ov_status = ttk.Label(top, text="…", width=14)
        self.ov_status.pack(side="left", padx=(4, 6))
        self.ov_btn = ttk.Button(top, text="▶ Запустить", command=self.toggle_overlay)
        self.ov_btn.pack(side="left")
        ttk.Button(top, text="📁 Папка", command=lambda: open_path(WORK)).pack(side="right")
        ttk.Button(top, text="⚙ Настройки", command=self.open_settings).pack(side="right", padx=6)
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var, foreground="#777").pack(side="right", padx=10)

        pw = ttk.PanedWindow(r, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # левая колонка: чаты
        left = ttk.Frame(pw, padding=(0, 0, 6, 0))
        pw.add(left, weight=0)
        ttk.Label(left, text="Чаты", font=(FAM, 10, "bold")).pack(anchor="w")
        ttk.Label(left, text="оверлей отвечает в выбранном", foreground="#777",
                  font=(FAM, 8)).pack(anchor="w")
        lb_frame = ttk.Frame(left)
        lb_frame.pack(fill="both", expand=True, pady=4)
        self.chat_list = tk.Listbox(lb_frame, width=28, activestyle="none", exportselection=False,
                                    font=(FAM, 10), relief="flat", bg="#f4f2ee",
                                    selectbackground="#d97757", selectforeground="white")
        sb = ttk.Scrollbar(lb_frame, command=self.chat_list.yview)
        self.chat_list.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat_list.pack(side="left", fill="both", expand=True)
        self.chat_list.bind("<<ListboxSelect>>", self._on_select_chat)
        self.chat_list.bind("<Double-Button-1>", lambda e: self.rename_chat())
        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="+ Новый чат", command=self.new_chat).pack(side="left", fill="x",
                                                                          expand=True)
        ttk.Button(btns, text="✎", width=3, command=self.rename_chat).pack(side="left", padx=2)
        ttk.Button(btns, text="🗑", width=3, command=self.remove_chat).pack(side="left")

        # правая часть: выбранный чат
        right = ttk.Frame(pw)
        pw.add(right, weight=1)
        self.title_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.title_var, font=(FAM, 12, "bold")).pack(anchor="w")
        pr = ttk.Frame(right)
        pr.pack(fill="x", pady=(4, 2))
        ttk.Label(pr, text="Что делать со скриншотами:").pack(side="left")
        self.prompt_var = tk.StringVar(value="")
        self.prompt_entry = ttk.Entry(pr, textvariable=self.prompt_var)
        self.prompt_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.prompt_entry.bind("<FocusOut>", lambda e: self._save_prompt())
        self.prompt_entry.bind("<Return>", lambda e: (self._save_prompt(), self.input.focus_set()))
        ttk.Label(right, foreground="#888", font=(FAM, 8), text=(
            "Пусто — берётся текст из настроек. Пример: «Реши задачу, дай ответ и короткое "
            "решение»")).pack(anchor="w")

        mid = ttk.Frame(right)
        self.chat_view = tk.Text(mid, wrap="word", font=(FAM, 10), padx=8, pady=6, width=40,
                                 height=10, relief="flat", bg="#fbfaf8")
        vsb = ttk.Scrollbar(mid, command=self.chat_view.yview)
        self.chat_view.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.chat_view.pack(side="left", fill="both", expand=True)
        self.chat_view.tag_configure("you", foreground="#1f5fbf", font=(FAM, 10, "bold"))
        self.chat_view.tag_configure("claude", foreground="#c15f3c", font=(FAM, 10, "bold"))
        self.chat_view.tag_configure("sys", foreground="#888", font=(FAM, 9, "italic"))
        self.chat_view.bind("<Key>", self._readonly_keys)

        att = ttk.Frame(right, padding=(0, 2))
        self.att_var = tk.StringVar(value="")
        ttk.Label(att, textvariable=self.att_var, foreground="#555").pack(side="left")
        self.att_clear = ttk.Button(att, text="убрать", command=self._clear_att)

        bot = ttk.Frame(right, padding=(0, 4))
        bb = ttk.Frame(bot)
        bb.pack(side="right", padx=(6, 0))
        ttk.Button(bb, text="Отправить", command=self.send).pack(fill="x")
        ttk.Button(bb, text="📎 Файлы", command=self._attach).pack(fill="x", pady=2)
        ttk.Button(bb, text="■ Стоп", command=self.stop).pack(fill="x")
        self.input = tk.Text(bot, height=4, width=30, wrap="word", font=(FAM, 10),
                             relief="solid", bd=1)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._enter)

        self.help_label = ttk.Label(right, text="", wraplength=640, foreground="#777",
                                    font=(FAM, 8))
        self.help_label.pack(side="bottom", fill="x", pady=(0, 4))
        bot.pack(side="bottom", fill="x")
        att.pack(side="bottom", fill="x")
        mid.pack(fill="both", expand=True, pady=(6, 0))
        self._update_help()

    def _update_help(self):
        hk = self.cfg["hotkeys"]
        short = {"shot": "скриншот", "region": "скриншот области", "toggle": "скрыть/показать",
                 "scroll_up": "прокрутка ↑", "scroll_down": "прокрутка ↓", "move": "двигать панель",
                 "chat": "это окно", "stop": "стоп", "quit": "закрыть оверлей"}
        items = [f"{pretty_combo(hk[a])} — {short[a]}" for a, _ in ACTION_NAMES if hk.get(a)]
        self.help_label.configure(text="Оверлей: " + " · ".join(items) +
                                  ".   Здесь: Enter — отправить, Shift+Enter — новая строка.")

    def _readonly_keys(self, e):
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a", "insert"):
            return None
        if e.keysym in ("Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"):
            return None
        return "break"

    def _enter(self, e):
        if e.state & 0x1:
            return None
        self.send()
        return "break"

    # --- список чатов -----------------------------------------------------
    def _refresh_chat_list(self, force=False):
        sig = chats_signature()
        if not force and sig == self.chats_sig:
            return
        self.chats_sig = sig
        chats = list_chats()
        self.chat_ids = [c["id"] for c in chats]
        self.chat_list.delete(0, "end")
        for c in chats:
            self.chat_list.insert("end", "  " + (c.get("title") or DEFAULT_TITLE))
        if self.chat and self.chat["id"] in self.chat_ids:
            i = self.chat_ids.index(self.chat["id"])
            self.chat_list.selection_clear(0, "end")
            self.chat_list.selection_set(i)
            self.chat_list.see(i)

    def _on_select_chat(self, _=None):
        sel = self.chat_list.curselection()
        if not sel:
            return
        cid = self.chat_ids[sel[0]]
        if cid == self.chat["id"]:
            return
        if self.busy:
            self.status_var.set("Дождись ответа Claude в текущем чате")
            self._refresh_chat_list(force=True)
            return
        self._save_prompt()
        set_active_chat(cid)
        self._show_chat(cid)

    def _show_chat(self, cid):
        c = load_chat(cid)
        if not c:
            c = get_active_chat()
        self.chat = c
        self.chat_mtime = mtime(CHATS / f"{c['id']}.json")
        self.title_var.set(c.get("title") or DEFAULT_TITLE)
        self.prompt_var.set(c.get("screenshot_prompt", ""))
        v = self.chat_view
        v.delete("1.0", "end")
        for m in c.get("history", []):
            self._append(m["role"], m["text"])
        if not c.get("history"):
            self._append("sys", "Новый чат. Расскажи, что будешь присылать со скриншотами и как "
                                "отвечать, прикрепи нужные файлы (методички, условия). Потом "
                                "запусти оверлей и жми горячую клавишу.")
        v.see("end")

    def new_chat(self):
        if self.busy:
            self.status_var.set("Дождись ответа Claude")
            return
        self._save_prompt()
        c = create_chat()
        set_active_chat(c["id"])
        self._refresh_chat_list(force=True)
        self._show_chat(c["id"])
        self._refresh_chat_list(force=True)
        self.input.focus_set()

    def rename_chat(self):
        name = simpledialog.askstring("Переименовать чат", "Название:", parent=self.root,
                                      initialvalue=self.chat.get("title", ""))
        if name and name.strip():
            update_chat(self.chat["id"], lambda c: c.__setitem__("title", name.strip()[:60]))
            self.title_var.set(name.strip()[:60])
            self._refresh_chat_list(force=True)

    def remove_chat(self):
        cid = self.chat["id"]
        who = chat_busy_by(cid)
        if who or self.busy:
            messagebox.showinfo("Удаление", "Сейчас в этом чате отвечает Claude — дождись ответа.",
                                parent=self.root)
            return
        if not messagebox.askyesno("Удалить чат", f"Удалить чат «{self.chat.get('title')}»?\n"
                                   "Переписка пропадёт.", parent=self.root):
            return
        delete_chat(cid)
        update_state(active_chat=None)
        self.chat = get_active_chat()
        self._refresh_chat_list(force=True)
        self._show_chat(self.chat["id"])
        self._refresh_chat_list(force=True)

    def _save_prompt(self):
        v = self.prompt_var.get().strip()
        if self.chat and v != (self.chat.get("screenshot_prompt") or ""):
            c = update_chat(self.chat["id"], lambda c: c.__setitem__("screenshot_prompt", v))
            if c:
                self.chat = c
                self.chat_mtime = mtime(CHATS / f"{c['id']}.json")

    # --- журнал -------------------------------------------------------------
    def _append(self, role, text):
        v = self.chat_view
        if role == "sys":
            v.insert("end", text + "\n\n", "sys")
        else:
            v.insert("end", ("Вы" if role == "you" else "Claude") + "\n", role)
            if role == "claude":
                insert_rich(v, text, fg="#1d1d1f", bg="#fbfaf8", size_pt=10,
                            enabled=self.cfg.get("math", "latex") == "latex")
                v.insert("end", "\n\n")
            else:
                v.insert("end", text + "\n\n")
        v.see("end")

    def _sys(self, text):
        self._append("sys", text)

    # --- вложения -----------------------------------------------------------
    def _attach(self):
        for p in filedialog.askopenfilenames(title="Файлы для Claude", parent=self.root):
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

    # --- отправка -----------------------------------------------------------
    def send(self):
        text = self.input.get("1.0", "end-1c").strip()
        if not text and not self.attachments:
            return
        if self.busy:
            self.status_var.set("Claude ещё отвечает…")
            return
        exe = find_claude(self.cfg.get("claude_path", ""))
        if not exe:
            self._sys("⚠ Claude Code не найден: ⚙ Настройки → Аккаунт.")
            self.open_settings("account")
            return
        self._save_prompt()
        cid = self.chat["id"]
        who = acquire_chat(cid, "manager")
        if who:
            self.status_var.set(f"Сейчас в этом чате отвечает {WHO_NAMES.get(who, who)} — подожди")
            return
        blocks, notes = self._prepare_attachments()
        prompt = text or "Посмотри на прикреплённые файлы."
        if notes:
            prompt += ("\n\nЯ прикрепил файлы (прочитай их инструментом Read):\n"
                       + "\n".join("- " + n for n in notes))
        prompt += "\n\n(" + math_note(self.cfg) + ")"
        content = [{"type": "text", "text": prompt}] + blocks
        shown = text
        if self.attachments:
            shown += ("\n" if shown else "") + "📎 " + ", ".join(p.name for p in self.attachments)
        self.input.delete("1.0", "end")
        self._clear_att()

        self.busy = True
        self.shown = shown
        self.cur_text = ""
        self.session_id = None
        self.req_chat = cid
        if not self.chat.get("history"):
            self.chat_view.delete("1.0", "end")
        self._append("you", shown)
        self.chat_view.insert("end", "Claude\n", "claude")
        self.chat_view.mark_set("ans", "end-1c")
        self.chat_view.mark_gravity("ans", "left")
        self.chat_view.insert("end", "…\n\n")
        self.chat_view.see("end")
        self.status_var.set("думаю…")
        fresh = load_chat(cid) or self.chat
        self.rid = self.runner.start(exe, content, fresh.get("session_id"), self.cfg.get("model"))

    def stop(self):
        if self.busy:
            self.runner.stop()

    def _poll_queue(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(30, self._poll_queue)

    def _handle(self, ev):
        kind = ev[0]
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
        if ev[1] != self.rid:
            return
        if kind == "session":
            self.session_id = ev[2]
        elif kind == "status":
            self.status_var.set(ev[2])
        elif kind == "msg_start":
            if self.cur_text and not self.cur_text.endswith("\n\n"):
                self.cur_text += "\n\n"
        elif kind == "delta":
            self.cur_text += ev[2]
            if not self._render_job:
                self._render_job = self.root.after(50, self._render)
        elif kind == "done":
            text, sid, is_err = ev[2], ev[3], ev[4]
            clear = False
            if is_err and not text.startswith("⏹"):
                text, clear = resolve_error(text)
                if "вход" in text:
                    self._logged_in = False
                    self.root.after(100, lambda: self.open_settings("account"))
            self._finish(text or self.cur_text, sid or self.session_id, clear)
        elif kind == "fail":
            text, clear = resolve_error(ev[2])
            self._finish(text, None, clear)

    def _render(self, final=False):
        self._render_job = None
        if self.req_chat != self.chat["id"]:
            return
        v = self.chat_view
        v.delete("ans", "end-1c")
        insert_rich(v, self.cur_text, fg="#1d1d1f", bg="#fbfaf8", size_pt=10,
                    enabled=self.cfg.get("math", "latex") == "latex")
        v.insert("end", "\n\n" if final else " ▍\n\n")
        v.see("end")

    def _finish(self, text, sid, clear_session):
        if self._render_job:
            self.root.after_cancel(self._render_job)
            self._render_job = None
        self.cur_text = text
        self._render(final=True)
        self.busy = False
        self.status_var.set("")
        try:
            c = append_messages(self.req_chat, [{"role": "you", "text": self.shown},
                                                {"role": "claude", "text": text}],
                                session_id=sid, clear_session=clear_session)
        finally:
            release_chat(self.req_chat)
        if c and c["id"] == self.chat["id"]:
            self.chat = c
            self.chat_mtime = mtime(CHATS / f"{c['id']}.json")
            self.title_var.set(c.get("title") or DEFAULT_TITLE)
        self._refresh_chat_list(force=True)

    # --- фоновая сверка с оверлеем -----------------------------------------
    def _poll_files(self):
        try:
            for cmd in take_commands("manager"):
                if cmd == "focus":
                    self.bring_to_front()
            ov = running_pid("overlay")
            self.ov_status.configure(text="● работает" if ov else "○ остановлен",
                                     foreground="#2e7d32" if ov else "#888")
            self.ov_btn.configure(text="■ Остановить" if ov else "▶ Запустить")
            self._refresh_chat_list()
            if not self.busy:
                path = CHATS / f"{self.chat['id']}.json"
                m = mtime(path)
                if not m:  # чат удалили
                    self.chat = get_active_chat()
                    self._show_chat(self.chat["id"])
                elif m != self.chat_mtime:
                    self._show_chat(self.chat["id"])  # оверлей дописал ответ
                who = chat_busy_by(self.chat["id"])
                if who == "overlay":
                    self.status_var.set("оверлей отвечает на скриншот…")
                elif self.status_var.get() == "оверлей отвечает на скриншот…":
                    self.status_var.set("")
        finally:
            self.root.after(700, self._poll_files)

    def toggle_overlay(self):
        if running_pid("overlay"):
            send_command("stop")
            self.ov_status.configure(text="останавливаю…")
        else:
            try:
                launch("overlay.py")
                self.ov_status.configure(text="запускаю…")
            except Exception as e:
                messagebox.showerror("Оверлей", f"Не удалось запустить оверлей: {e}",
                                     parent=self.root)

    def bring_to_front(self):
        r = self.root
        r.deiconify()
        r.lift()
        r.attributes("-topmost", True)
        r.after(300, lambda: r.attributes("-topmost", False))
        r.focus_force()

    # --- настройки ------------------------------------------------------------
    def open_settings(self, tab="account"):
        if self.settings and self.settings.alive:
            self.settings.lift(tab)
        else:
            self.settings = SettingsWindow(self, tab)

    def apply_settings(self, cfg):
        self.cfg = cfg
        save_json(CONFIG_PATH, cfg)  # оверлей подхватит изменения сам
        self._update_help()
        if not self.busy:
            self._show_chat(self.chat["id"])

    def preview_panel(self):
        if self._preview is None:
            self._preview = Overlay(self.root, self.cfg, self.panel_state, lambda: None)
        return self._preview

    def hide_preview(self):
        if self._preview is not None:
            self._preview.hide()
        self.hide_preview_marks()

    def preview_marks(self, cfg):
        """Пример пометок в центре выбранного монитора."""
        if self._preview_marks is None:
            self._preview_marks = ScreenMarks(self.root, cfg)
        pm = self._preview_marks
        pm.apply_config(cfg)
        try:
            mon = pick_monitor(cfg["overlay"].get("monitor", "cursor"), self.root.winfo_pointerxy())
        except tk.TclError:
            mon = pick_monitor("primary")
        cx, cy = mon["width"] // 2, mon["height"] // 2
        demo = {"summary": "Пример: так будут выглядеть пометки",
                "marks": [{"box": [cx - 260, cy - 90, cx - 40, cy - 40], "text": "Ответ: 42"},
                          {"box": [cx - 260, cy + 20, cx + 60, cy + 70],
                           "text": "Здесь ошибка: знак минус вместо плюса"}]}
        pm.draw(demo, {"ox": mon["left"], "oy": mon["top"], "scale": 1.0, "area": mon})

    def hide_preview_marks(self):
        if self._preview_marks is not None:
            self._preview_marks.clear()

    def on_auth(self, logged, desc):
        if logged and not self._logged_in:
            self._logged_in = True
            self.status_var.set(desc.splitlines()[0])

    def _check_claude(self):
        path = self.cfg.get("claude_path", "")

        def check():
            exe = find_claude(path)
            if not exe:
                self.q.put(("auth", None, "", False, ""))
                return
            self.q.put(("auth", exe, claude_version(exe)) + auth_status(exe))
        threading.Thread(target=check, daemon=True).start()

    def quit(self):
        try:
            self._save_prompt()
            if self.busy:
                self.runner.stop()
                release_chat(self.req_chat)
            if self.settings and self.settings.alive:
                set_hotkeys_paused(False)
        finally:
            clear_pid("manager")
            self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            clear_pid("manager")


def focus_window_safe():
    try:
        from core import focus_window
        focus_window(MANAGER_TITLE)
    except Exception:
        pass


if __name__ == "__main__":
    Manager().run()
