#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Оверлей «ИИ-помощника поверх экрана»: работает в фоне без окон.

Горячая клавиша → скриншот экрана/области → активный чат (выбирается в «Управлении»)
→ ответ в полупрозрачной панели поверх всех окон, которая пропускает клики мыши.
"""
import queue
import sys
import threading
import tkinter as tk

from providers import (
    PROVIDER_SHORT, Runner, build_spec, explain_error, finish_chat, provider_of, provider_ready,
    access_mode,
)
from core import (
    ACTION_NAMES, CONFIG_PATH, STATE_PATH, WHO_NAMES, Overlay, make_hotkeys, screen_scale,
    RegionSelector, set_screen_scale, MANAGER_TITLE, SCREEN_PROMPT, ScreenMarks,
    acquire_chat, fit_for_vision, marks_as_text, parse_marks, MATH, math_note,
    auth_status, claude_version, clear_pid, ensure_dirs, find_claude, focus_window,
    get_active_chat, grab_screen_at, hotkeys_paused, image_to_block, launch, load_chat,
    load_config, load_state, migrate_old_state, mtime, pretty_combo, release_chat,
    running_pid, send_command, set_dpi_aware, take_commands, update_state,
    write_pid,
)


class OverlayApp:
    def __init__(self):
        ensure_dirs()
        migrate_old_state()
        if running_pid("overlay"):
            send_command("hello")  # уже запущен — пусть покажет себя
            sys.exit(0)
        write_pid("overlay")
        set_dpi_aware()

        self.cfg = load_config()
        self.cfg_mtime = mtime(CONFIG_PATH)
        self.state = load_state()
        self.state_mtime = mtime(STATE_PATH)

        self.q = queue.Queue()
        self.runner = Runner(self.q.put)
        self.busy = False
        self.capturing = False  # идёт снимок / выделение области
        self.rid = None
        self.cur_text = ""
        self.chat_id = None
        self.session_id = None
        self.shown = ""
        self._render_job = None

        MATH.preload()
        self.root = tk.Tk()
        self.root.withdraw()
        set_screen_scale(self.root)
        self.root.title("AI Overlay — оверлей")
        self.panel = Overlay(self.root, self.cfg, self.state, self._moved)
        self.marks = ScreenMarks(self.root, self.cfg)
        self.mapping = None  # куда переносить координаты пометок (режим «по экрану»)

        self.hotkeys = None
        self.paused = hotkeys_paused()
        if not self.paused:
            self.start_hotkeys()
        self.panel.set_who(PROVIDER_SHORT[provider_of(get_active_chat(), self.cfg)])
        self._startup_check()
        self.root.after(30, self._poll_queue)
        self.root.after(400, self._poll_files)

    # --- горячие клавиши ---------------------------------------------------
    def start_hotkeys(self):
        self.stop_hotkeys()
        hk = self.cfg["hotkeys"]
        self.hotkeys = make_hotkeys(hk, self.q.put)
        self.hotkeys.start()

    def stop_hotkeys(self):
        if self.hotkeys:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
            self.hotkeys = None

    def _hk(self, action):
        return pretty_combo(self.cfg["hotkeys"].get(action, ""))

    def _hotkey(self, action):
        if action == "shot":
            self.shot(region=False)
        elif action == "region":
            self.shot(region=True)
        elif action == "toggle":
            if self.marks.has_marks and not self.panel.visible:
                self.marks.toggle()  # в режиме «по экрану» скрывает/показывает пометки
            else:
                self.panel.cancel_hide()
                self.panel.toggle()
        elif action == "scroll_up":
            self.panel.scroll(-1)
        elif action == "scroll_down":
            self.panel.scroll(1)
        elif action == "move":
            self.panel.toggle_move()
        elif action == "stop":
            if self.busy:
                self.runner.stop()
        elif action == "chat":
            self.open_manager()
        elif action == "quit":
            self.quit()

    def open_manager(self):
        if running_pid("manager"):
            send_command("focus", "manager")
            focus_window(MANAGER_TITLE)
        else:
            launch("manager.py")

    # --- запуск: проверка Claude Code и входа ------------------------------
    def _startup_check(self):
        path = self.cfg.get("claude_path", "")
        cfg = self.cfg
        prov = provider_of(get_active_chat(), cfg)

        def check():
            if prov != "claude" or access_mode(cfg, "claude") != "subscription":
                ok, msg = provider_ready(prov, cfg)
                self.q.put(("auth", "other", "", ok, msg))
                return
            exe = find_claude(path)
            if not exe:
                self.q.put(("auth", None, "", False, ""))
            else:
                self.q.put(("auth", exe, claude_version(exe)) + auth_status(exe))
        threading.Thread(target=check, daemon=True).start()

    def _on_auth(self, exe, logged, desc=""):
        how = f"Открой «Управление» ({self._hk('chat')}) → ⚙ Настройки → Нейросети → Claude."
        if exe == "other" and not logged:
            self.panel.flash(f"⚠ {desc}\n«Управление» — {self._hk('chat')}.", 15)
        elif not exe:
            self.panel.flash("⚠ Claude Code не найден. " + how, 15)
        elif not logged:
            self.panel.flash("⚠ Не выполнен вход в аккаунт Claude. " + how, 15)
        else:
            chat = get_active_chat()
            hint = " · ".join(f"{self._hk(a)} — {t}" for a, t in (
                ("shot", "скриншот"), ("region", "область"), ("toggle", "скрыть панель"))
                if self.cfg["hotkeys"].get(a))
            paused = "\n(горячие клавиши на паузе, пока открыты настройки)" if self.paused else ""
            who = PROVIDER_SHORT[provider_of(chat, self.cfg)]
            self.panel.set_who(who)
            self.panel.flash(f"Оверлей запущен · чат «{chat.get('title')}» · {who}\n{hint}{paused}", 6)

    # --- скриншот → Claude ---------------------------------------------------
    def shot(self, region):
        if self.capturing:
            return
        if self.busy:
            self.panel.show()
            self.panel.set_status(f"ещё отвечаю… ({self._hk('stop')} — стоп)")
            return
        chat = get_active_chat()
        who = acquire_chat(chat["id"], "overlay")
        if who:
            self.panel.flash(f"Чат «{chat.get('title')}» сейчас занят ({WHO_NAMES.get(who, who)}). "
                             "Дождись ответа.", 5)
            return
        prompt = (chat.get("screenshot_prompt") or "").strip() or self.cfg["screenshot_prompt"]
        self.capturing = True
        was_visible = self.panel.visible
        marks_visible = self.marks.visible
        self.panel.hide()
        self.marks.hide()  # старые пометки не должны попасть на снимок
        px, py = self.root.winfo_pointerxy()

        def cancel():
            self.capturing = False
            release_chat(chat["id"])
            if was_visible:
                self.panel.show()
            if marks_visible:
                self.marks.show()

        def send(img, mon):
            self.capturing = False
            self.marks.clear()
            shown = "📷 скриншот" + (" области" if region else "")
            if self.cfg["overlay"].get("display_mode") == "screen":
                vis, scale = fit_for_vision(img, self.cfg["max_image_side"])
                ox, oy = img.info.get("origin", (0, 0))  # в пикселях снимка
                s = screen_scale()  # Retina: пикселей на единицу экрана
                self.mapping = {"ox": mon["left"] + ox / s, "oy": mon["top"] + oy / s,
                                "scale": scale * s, "area": mon}
                text = prompt + "\n\n" + SCREEN_PROMPT.format(w=vis.width, h=vis.height)
                content = [{"type": "text", "text": text}, image_to_block(vis, max(vis.size))]
                shown += " · пометки на экране"
            else:
                self.mapping = None
                content = [{"type": "text", "text": prompt + "\n\n(" + math_note(self.cfg) + ")"},
                           image_to_block(img, self.cfg["max_image_side"])]
            self.start_request(chat, content, shown)

        def grab():
            try:
                img, mon = grab_screen_at(px, py)
            except Exception as e:
                cancel()
                self.panel.flash(f"Не удалось сделать скриншот: {e}")
                return
            if region:
                RegionSelector(self.root, img, mon, lambda crop: send(crop, mon), cancel)
            else:
                send(img, mon)
        self.root.after(200, grab)

    def start_request(self, chat, content, shown):
        fresh = load_chat(chat["id"]) or chat  # сессия могла обновиться в «Управлении»
        provider = provider_of(fresh, self.cfg)
        self.busy = True
        self.chat_id = chat["id"]
        self.session_id = None
        self.shown = shown
        self.cur_text = ""
        self.panel.cancel_hide()
        self.panel.show()
        self.panel.set_text("")
        self.panel.set_who(PROVIDER_SHORT[provider])
        self.panel.set_status(f"думаю… · {chat.get('title')}")
        self.spec = build_spec(provider, fresh, self.cfg, content)
        self.rid = self.runner.start_spec(self.spec)

    # --- события ---------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(30, self._poll_queue)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "hotkey":
            self._hotkey(ev[1])
            return
        if kind == "hotkey_fail":
            self.panel.flash("Не удалось назначить горячие клавиши: " + "; ".join(ev[1]) +
                             ". Поменяй их в «Управлении» → ⚙ Настройки.", 12)
            return
        if kind == "auth":
            self._on_auth(ev[1], ev[3], ev[4])
            return
        if ev[1] != self.rid:
            return
        if kind == "session":
            self.session_id = ev[2]
        elif kind == "status":
            self.panel.set_status(ev[2])
        elif kind == "msg_start":
            if self.cur_text and not self.cur_text.endswith("\n\n"):
                self.cur_text += "\n\n"
        elif kind == "reset":
            self.cur_text = ""
        elif kind == "delta":
            self.cur_text += ev[2]
            if not self._render_job:
                self._render_job = self.root.after(50, self._render)
        elif kind in ("done", "fail"):
            if kind == "done":
                text, sid, is_err = ev[2], ev[3], ev[4]
            else:
                text, sid, is_err = ev[2], None, True
            clear = False
            if is_err and not text.startswith("⏹"):
                text, clear = explain_error(self.spec["provider"], text)
            self._finish(text or self.cur_text, sid or self.session_id, clear)

    def _render(self):
        self._render_job = None
        if self.busy and self.mapping:
            self.panel.set_status("готовлю пометки…")
        elif self.busy:
            self.panel.set_text(self.cur_text, scroll_end=True)
            self.panel.set_status("пишу…")

    def _finish(self, text, sid, clear_session):
        if self._render_job:
            self.root.after_cancel(self._render_job)
            self._render_job = None
        self.busy = False
        parsed = parse_marks(text) if self.mapping and not text.startswith(("⚠", "⏹")) else None
        if parsed and (parsed["marks"] or parsed["summary"]):
            self.panel.hide()
            self.panel.set_text("")
            try:
                self.marks.draw(parsed, self.mapping)
            except Exception as e:
                self.panel.flash(f"Не удалось нарисовать пометки: {e}\n\n{marks_as_text(parsed)}", 20)
            text = marks_as_text(parsed)
        else:
            if self.mapping and not text.startswith(("⚠", "⏹")):
                text = "(пометки не получились — ответ текстом)\n\n" + text
            self.panel.set_text(text)
            self.panel.set_status("")
            self.panel.schedule_hide()
        self.mapping = None
        try:
            finish_chat(self.chat_id, self.spec, self.shown, text, sid, clear_session)
        finally:
            release_chat(self.chat_id)

    # --- связь с «Управлением» через файлы ------------------------------------
    def _poll_files(self):
        try:
            for cmd in take_commands("overlay"):
                if cmd in ("stop", "quit"):
                    self.quit()
                    return
                if cmd == "hello":
                    self.panel.flash("Оверлей уже запущен.", 3)
                elif cmd in dict(ACTION_NAMES):
                    self._hotkey(cmd)
            m = mtime(CONFIG_PATH)
            if m != self.cfg_mtime:
                self.cfg_mtime = m
                old_hk = dict(self.cfg["hotkeys"])
                self.cfg = load_config()
                self.panel.apply_config(self.cfg)
                self.marks.apply_config(self.cfg)
                if self.cfg["hotkeys"] != old_hk and not self.paused:
                    self.start_hotkeys()
            m = mtime(STATE_PATH)
            if m != self.state_mtime:
                self.state_mtime = m
                st = load_state()
                if st.get("overlay_pos") != self.state.get("overlay_pos") and not self.panel.move_mode:
                    self.state["overlay_pos"] = st.get("overlay_pos")
                    self.panel._schedule_fit()
            p = hotkeys_paused()
            if p and not self.paused:
                self.paused = True
                self.stop_hotkeys()
            elif not p and self.paused:
                self.paused = False
                self.start_hotkeys()
        finally:
            self.root.after(400, self._poll_files)

    def _moved(self):
        update_state(overlay_pos=self.state.get("overlay_pos"))
        self.state_mtime = mtime(STATE_PATH)

    def quit(self):
        try:
            self.runner.stop()
            self.stop_hotkeys()
            if self.busy and self.chat_id:
                release_chat(self.chat_id)
        finally:
            clear_pid("overlay")
            self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            clear_pid("overlay")


if __name__ == "__main__":
    OverlayApp().run()
