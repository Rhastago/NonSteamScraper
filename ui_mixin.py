"""Reusable, window-agnostic UI helpers for NonSteamScraper: themed widget
factories, the bundled-icon loader, the status/apply-button setters, the tooltip,
and the theme/accent switching (which relaunches the app). Mixed into SteamArtApp.

These read the live theme via ``self.theme`` and the icon cache/constants that
SteamArtApp.__init__ sets up, so they only make sense as a mixin on that class."""

import os
import sys
import tkinter as tk
from PIL import Image, ImageTk

from appcommon import resource_path
from theming import ACCENTS, FONT_UI, save_theme, save_accent

class UIMixin:

    def _icon(self, name, size=18):
        """Return a cached PhotoImage for assets/icons/{name}.png scaled to `size` px,
        or None if it can't be loaded — callers fall back to their emoji/text so the UI
        never breaks if an icon is missing or unbundled. Tk can't render color emoji,
        so these bundled images are how the app shows colorful icons consistently."""
        key = (name, size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        photo = None
        try:
            img = Image.open(resource_path(os.path.join("assets", "icons", f"{name}.png")))
            img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            photo = None
        self._icon_cache[key] = photo
        return photo


    def _iconify(self, button, name, size=18, compound=None):
        """Swap a button/label's emoji text for a bundled color icon. With `compound`
        set (e.g. 'left') the icon sits beside the existing text; otherwise it replaces
        the text. No-op if the icon can't load, leaving the original glyph in place."""
        ic = self._icon(name, size)
        if not ic:
            return
        if compound:
            button.config(image=ic, compound=compound)
        else:
            button.config(image=ic, text="")


    def _set_status(self, text, fg=None, icon=None):
        """Set the status bar text with an optional leading color icon. Always sets the
        image (icon or empty) so a previous icon never lingers when the text changes."""
        ic = self._icon(icon, self.ICON_INLINE) if icon else None
        self.status_bar.config(text=text, image=ic or "",
                               compound="left" if ic else "none",
                               fg=fg or self.theme["fg"])


    def _set_apply_btn(self, btn, applied, queued=False):
        """Set an Apply/Applied button's label + color icon by state, clearing any
        stale image when reverting so the icon and text stay in sync. `queued` is the
        icon-deferred case: the art downloaded but the shortcuts.vdf write is pending a
        Steam-close, so we show a distinct "Queued" label instead of "Applied!"."""
        if queued:
            # Icon write deferred while Steam is running (applies on Steam close).
            ic = self._icon("warning", self.ICON_ACTION)
            btn.config(text="Queued (close Steam)", image=ic or "",
                       compound="left" if ic else "none")
            return
        ic = self._icon("applied", self.ICON_ACTION) if applied else None
        if applied:
            btn.config(text="Applied!", image=ic or "",
                       compound="left" if ic else "none")
        else:
            btn.config(text="Apply this one", image="", compound="none")


    # --- Themed widget helpers ---

    def _btn(self, parent, text, command, primary=False, **kw):
        """Create a button styled for the current theme. With primary=True it uses
        the accent color as a prominent call-to-action (accent bg, on-accent text,
        accent-hover highlight); otherwise it's a normal neutral button."""
        if primary:
            return tk.Button(parent, text=text, command=command,
                             bg=self.theme["accent"], fg=self.theme["accent_fg"],
                             activebackground=self.theme["accent_hover"],
                             activeforeground=self.theme["accent_fg"],
                             relief="flat", cursor="hand2", **kw)
        return tk.Button(parent, text=text, command=command,
                         bg=self.theme["button_bg"], fg=self.theme["button_fg"],
                         activebackground=self.theme["select_bg"],
                         activeforeground=self.theme["fg"], **kw)


    def _flat_btn(self, parent, text, command, **kw):
        """Create a borderless button that blends into the background."""
        return tk.Button(parent, text=text, command=command, relief="flat",
                         bg=self.theme["bg"], fg=self.theme["fg"],
                         activebackground=self.theme["bg"],
                         activeforeground=self.theme["fg"], **kw)


    def _frame(self, parent, **kw):
        return tk.Frame(parent, bg=self.theme["bg"], **kw)


    def _label(self, parent, text, **kw):
        kw.setdefault("bg", self.theme["bg"])
        kw.setdefault("fg", self.theme["fg"])
        return tk.Label(parent, text=text, **kw)


    def _make_scrollable_frame(self, parent, padx=10, pady=0):
        """Return a (canvas, body_frame) pair inside a scrollable container."""
        t = self.theme
        container = tk.Frame(parent, bg=t["bg"])
        container.pack(fill="both", expand=True, padx=padx, pady=pady)
        canvas = tk.Canvas(container, bg=t["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=t["bg"])
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return canvas, body


    def _add_tooltip(self, widget, text):
        """Show a small tooltip near the cursor when hovering over widget."""
        tip = None

        def show(event):
            nonlocal tip
            txt = text() if callable(text) else text
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 6}")
            tk.Label(tip, text=txt, font=(FONT_UI, 9),
                     bg="#ffffe0", fg="#1a1a1a", relief="solid", borderwidth=1,
                     padx=4, pady=2).pack()

        def hide(event):
            nonlocal tip
            if tip:
                tip.destroy()
                tip = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)


    def relaunch_app(self):
        """Restart the application so a theme change takes effect.
        Works for both the script and the packaged executable."""
        try:
            self.window.destroy()
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            # A PyInstaller onefile bundle unpacks to a temp dir tracked via _MEI*/_PYI*
            # env vars. If the relaunched process inherits them it reuses the OLD temp
            # dir, which the exiting process then deletes — breaking compiled imports
            # like PIL._imaging on restart. Strip them so the new process re-extracts.
            env = {k: v for k, v in os.environ.items()
                   if not (k.startswith("_MEI") or k.startswith("_PYI"))}
            os.execve(sys.executable, [sys.executable], env)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)


    def toggle_theme(self):
        """Switch between light and dark mode and relaunch."""
        save_theme("dark" if self.theme_name == "light" else "light")
        self.relaunch_app()


    def set_accent(self, name):
        """Persist a new accent color and relaunch so it takes effect everywhere
        (same mechanism as the light/dark toggle). No-op if unchanged."""
        if name == self.accent_name or name not in ACCENTS:
            return
        save_accent(name)
        self.relaunch_app()


    def _bind_wheel_to_canvas(self, widget, canvas):
        """Bind mouse-wheel scrolling on a widget and all of its descendants so they
        scroll the given canvas. Uses direct per-widget bindings (never bind_all),
        so opening or closing other windows can no longer disturb scrolling."""
        def _on_wheel(event):
            num = getattr(event, "num", None)
            if num == 4:
                canvas.yview_scroll(-1, "units")
            elif num == 5:
                canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, _on_wheel)
        for child in widget.winfo_children():
            self._bind_wheel_to_canvas(child, canvas)
