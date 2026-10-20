"""Off-thread image helpers for NonSteamScraper's results/list views.

Decoding (Image.open, every animation frame's seek + thumbnail) runs on a small
BOUNDED pool of daemon workers, never one thread per image (B12). A worker
touches nothing in Tk: it only fills a Future. The Tk thread polls that Future
with widget.after (_when_done) and builds the ImageTk.PhotoImage and changes
the widget there. Staleness tokens stamped on the target label let a slow
decode self-discard if a newer image has since been requested, and PhotoImage
references are kept (on the widget and/or a cache) so they aren't
garbage-collected to blank.
"""

import queue
import threading
import tkinter as tk
from concurrent.futures import Future

import artwork
from theming import FONT_UI
from PIL import Image, ImageTk


class _DaemonPool:
    """At most `workers` daemon threads draining a job queue; submit() returns a
    concurrent.futures.Future. Daemon, so a stalled job never holds the app open
    at exit (a ThreadPoolExecutor's workers are joined at interpreter exit)."""

    def __init__(self, workers):
        self._workers = workers
        self._jobs = queue.Queue()
        self._threads = []
        self._lock = threading.Lock()

    def submit(self, fn, *args):
        future = Future()
        self._jobs.put((future, fn, args))
        with self._lock:
            if len(self._threads) < self._workers:
                t = threading.Thread(target=self._run, daemon=True)
                self._threads.append(t)
                t.start()
        return future

    def _run(self):
        while True:
            future, fn, args = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as e:
                future.set_exception(e)


# Previews decode two at a time; on-demand preview downloads run two at a time.
_DECODE_POOL = _DaemonPool(2)
_FETCH_POOL = _DaemonPool(2)
# Animated previews the results window downloads on demand (Q19): two at a time
# across all cards, so a results window full of animated options cannot flood the
# connection.
_ANIM_POOL = _DaemonPool(2)

# How often the Tk thread looks at a pending Future (ms).
POLL_MS = 20


def _when_done(widget, future, on_done):
    """Tk thread: call on_done(future) on the Tk thread once `future` has finished
    — now if it already has, else by polling with widget.after. Stops quietly if
    the widget is destroyed first. This is how worker results reach Tk: the
    worker never calls into Tk itself."""
    def poll():
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        if future.done():
            on_done(future)
        else:
            try:
                widget.after(POLL_MS, poll)
            except Exception:
                pass  # widget gone
    poll()


def _decode_preview(path):
    """Worker: open `path` and pre-decode it for a 300x200 preview. Returns
    ("static", image, None) or ("animated", [frame, ...], [delay_ms, ...]) —
    every frame seeked and thumbnailed HERE, once, so the Tk thread only shows
    them (B12: each tick used to re-seek and re-thumbnail on the Tk thread)."""
    img = Image.open(path)
    n = getattr(img, "n_frames", 1)
    if n > 1:
        frames, delays = [], []
        for i in range(n):
            img.seek(i)
            frame = img.copy()
            frame.thumbnail((300, 200))
            frames.append(frame)
            delays.append(img.info.get("duration", 100))
        return "animated", frames, delays
    img.thumbnail((300, 200))
    # Copy so the original can be GC'd; thumbnail mutates in-place.
    return "static", img.copy(), None


def _play_frames(lbl, frames, delays, i=0):
    """Tk thread: show pre-decoded frame i on lbl, then schedule the next. Builds
    one PhotoImage per tick from the ready thumbnail (no decode, no resize) and
    keeps only the current one, so Tk holds one image per label. Silently stops
    if the widget is destroyed."""
    try:
        i %= len(frames)
        photo = ImageTk.PhotoImage(frames[i])
        lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
        lbl.image = photo
        lbl._anim_id = lbl.after(delays[i], lambda: _play_frames(lbl, frames, delays, i + 1))
    except Exception:
        pass


def _show_text_only(lbl, text):
    """Tk thread: the label shows `text` and NO picture. The image is cleared, any
    animation stopped and any decode in flight made stale — a Tk label with an image
    shows the image, not its text, so the previous option's picture used to stay
    under a counter that had moved on, and Apply installed art never seen (N8)."""
    if getattr(lbl, "_anim_id", None):
        try:
            lbl.after_cancel(lbl._anim_id)
        except Exception:
            pass
        lbl._anim_id = None
    lbl._load_token = object()
    lbl.config(image="", text=text, width=0, height=0)
    lbl.image = None


# The spinner shown while an animated preview is decoded again on a revisit (Q21b):
# a quarter turn per tick.
_SPINNER = "◐◓◑◒"
SPIN_MS = 120


def _spin(lbl, text, token, i=0):
    """Tk thread: a big turning glyph (its own label, about three times the text
    size — ruling Q21b) above `text` on lbl until the picture is up. Stops, and
    hides the glyph, when the label's load token changes (another option was asked
    for, or the decode failed) or an image has been set."""
    try:
        sp = getattr(lbl, "_spinner", None)
        if sp is None:
            sp = lbl._spinner = tk.Label(lbl.master, bg=lbl.cget("bg"), fg=lbl.cget("fg"),
                                         font=(FONT_UI, 30))
        if getattr(lbl, "_load_token", None) is not token or str(lbl.cget("image")):
            sp.place_forget()
            return
        if i == 0:
            lbl.config(text=f"\n\n{text}")   # below the glyph
            sp.place(relx=0.5, rely=0.36, anchor="center")
        sp.config(text=_SPINNER[i % len(_SPINNER)])
        lbl.after(SPIN_MS, lambda: _spin(lbl, text, token, i + 1))
    except Exception:
        pass   # the window went away


def _show_unavailable(lbl):
    """Tk thread: no picture for this option (N8)."""
    _show_text_only(lbl, "preview\nunavailable")


def _update_badge(badge_label, meta):
    """Show or hide the top-left badge overlay based on image metadata."""
    parts = []
    if meta.get("steam"):    parts.append("STEAM")
    if meta.get("animated"): parts.append("▶")
    if meta.get("nsfw"):     parts.append("18+")
    if meta.get("humor"):    parts.append("MEME")
    text = " ".join(parts)
    badge_label.config(text=text)
    if text:
        badge_label.place(x=4, y=4)
    else:
        badge_label.place_forget()


def _display_image_on_label(lbl, path, on_animated=None, cache=None, cache_key=None,
                            on_shown=None):
    """Display the image at path on lbl. Plays frame-by-frame if the image is animated.
    on_animated(bool) is called with the actual animation status detected from the file.

    The decode runs on the bounded _DECODE_POOL; ImageTk.PhotoImage and every
    widget change happen back on the Tk thread (_when_done).

    Staleness guard: a token is stamped on the label before the decode starts; if the
    token has been replaced by the time it finishes (user moved to a different
    image), the result is silently discarded.

    cache/cache_key: when both are given, a successfully-built STATIC PhotoImage is
    stored as cache[cache_key] so a later revisit can assign it synchronously and skip
    the off-thread decode. Animated images are never cached.

    on_shown() runs on the Tk thread once the picture (or its first frame) is up.
    """
    # Cancel any in-flight animation — this runs on the UI thread (safe).
    if hasattr(lbl, "_anim_id") and lbl._anim_id:
        try:
            lbl.after_cancel(lbl._anim_id)
        except Exception:
            pass
        lbl._anim_id = None

    # Stamp a new load token so stale decode results self-discard.
    token = object()
    lbl._load_token = token

    def show(future):
        # Tk thread.
        if lbl._load_token is not token:
            return  # stale — a newer image won the race
        try:
            kind, image, delays = future.result()
        except Exception:
            _show_unavailable(lbl)
            return
        if on_animated:
            on_animated(kind == "animated")
        if on_shown:
            on_shown()
        if kind == "animated":
            _play_frames(lbl, image, delays)
            return
        photo = ImageTk.PhotoImage(image)
        lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
        lbl.image = photo  # keep a reference so GC doesn't collect it
        # Static-only cache for the results cycle: store the built
        # PhotoImage so a revisit to this index assigns it synchronously.
        if cache is not None and cache_key is not None:
            cache[cache_key] = photo

    _when_done(lbl, _DECODE_POOL.submit(_decode_preview, path), show)


def _decode_row_thumb(executor, lbl, path, box, cache, cache_key, on_done=None):
    """Decode a small row-cover thumbnail OFF the UI thread on a bounded `executor`,
    then create the ImageTk.PhotoImage and assign it ON the UI thread (_when_done
    polls the executor's Future; the worker never calls into Tk).

    Correctness guards (a past project bug came from off-thread image handling):
      * Staleness token — a fresh token is stamped on `lbl` before the work is queued;
        if the label is rebuilt/re-targeted (e.g. the list re-renders mid-decode) the
        token changes and the stale result self-discards.
      * winfo_exists() — the label may be destroyed by a re-render before the worker
        finishes; _when_done stops polling a dead widget.
      * `box` is (w, h); thumbnail() keeps aspect within it. PhotoImage is created on
        the UI thread (Tk requirement) and stored in `cache[cache_key]` AND on the
        widget so neither is garbage-collected (a dropped reference renders blank).

    `on_done(photo)` (optional) runs on the UI thread after a successful assign."""
    token = object()
    lbl._thumb_token = token

    def decode():
        img = Image.open(path)
        # Animated covers: just show the first frame as a static thumbnail — rows
        # don't animate. convert() flattens any palette/alpha for a clean resize.
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        img = img.convert("RGBA")
        img.thumbnail(box)
        return img.copy()

    def show(future):
        if getattr(lbl, "_thumb_token", None) is not token:
            return  # stale — a newer render won the race
        try:
            copy = future.result()
        except Exception:
            return  # unreadable file: leave whatever placeholder is already shown
        photo = ImageTk.PhotoImage(copy)
        lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
        lbl.image = photo            # widget reference: GC guard
        cache[cache_key] = photo     # cache reference: reuse + GC guard
        if on_done:
            on_done(photo)

    try:
        future = executor.submit(decode)
    except Exception:
        return  # executor shut down (window closing)
    _when_done(lbl, future, show)


# update_view is a standalone helper rather than a class method because it
# operates only on the state dict and widget references passed to it,
# with no need to access the broader application state.
def update_view(state, img_label, counter_label):
    """Load and display the thumbnail at the current index in the results screen."""
    index = state["index"]
    counter_label.config(text=f"{index + 1} / {len(state['option_urls'])}")

    badge_label = state.get("badge_label")
    meta = state["option_meta"][index] if index < len(state.get("option_meta", [])) else {}
    if badge_label:
        _update_badge(badge_label, meta)

    # Correct the badge if the actual file's animation status differs from SGDB metadata
    def on_animated(is_anim, _meta=meta, _bl=badge_label):
        if _bl and is_anim != _meta.get("animated"):
            _meta["animated"] = is_anim
            _update_badge(_bl, _meta)

    # A card's animated-preview download panel belongs to the option it was shown
    # for: hidden on every move; the animated paths below bring it back (Q19).
    loader = state.get("animated_loader")
    if loader is not None:
        loader.hide()
    animated = bool(meta.get("animated") or meta.get("animated_preview"))
    plain_counter = f"{index + 1} / {len(state['option_urls'])}"

    def load_animated(path, cache=None, cache_key=None):
        # An animated preview is decoded on every show (its frames are not kept,
        # to save memory); a big one takes seconds. Say so with the turning glyph,
        # and never leave the previous option's picture up meanwhile (Q21/Q21b).
        counter_label.config(text=f"{plain_counter} (loading...)")
        _show_text_only(img_label, "Loading animated preview…")

        def shown(_expected=index):
            if state["index"] == _expected:
                counter_label.config(text=plain_counter)
        _display_image_on_label(img_label, path, on_animated,
                                cache=cache, cache_key=cache_key, on_shown=shown)
        _spin(img_label, "Loading animated preview…", img_label._load_token)

    if index in state["paths"]:
        # Per-index decoded-image cache: revisiting an index via ◀/▶ should be
        # instant instead of re-opening + re-decoding the file off-thread every time
        # (the row-thumb path already caches; the results cycle didn't). We cache the
        # built PhotoImage keyed by index for STATIC images ONLY — on a cache hit we
        # assign it synchronously (keeping a reference on the label so it isn't GC'd to
        # blank) and skip the worker entirely. Animated images are NOT cached: they
        # must keep the existing _display_image_on_label animation path, so they always
        # fall through to it. The cache is populated from _display_image_on_label's
        # static apply via state["_img_cache"], so the staleness-token / winfo_exists
        # guards there are untouched and preserved.
        cache = state.get("_img_cache")
        if cache is None:
            cache = state["_img_cache"] = {}
        cached = cache.get(index)
        if cached is not None:
            # Cancel any in-flight animation before swapping in the static cache hit,
            # mirroring _display_image_on_label so a prior animated frame can't keep
            # firing on this label.
            if getattr(img_label, "_anim_id", None):
                try:
                    img_label.after_cancel(img_label._anim_id)
                except Exception:
                    pass
                img_label._anim_id = None
            # Bump the load token so any in-flight off-thread decode self-discards
            # rather than overwriting this synchronous cache hit.
            img_label._load_token = object()
            img_label.config(image=cached, text="",
                             width=cached.width(), height=cached.height())
            img_label.image = cached
            if badge_label:
                _update_badge(badge_label, meta)
        elif animated:
            load_animated(state["paths"][index], cache=cache, cache_key=index)
        else:
            _display_image_on_label(img_label, state["paths"][index], on_animated,
                                    cache=cache, cache_key=index)
    else:
        # Fetch on demand. The previous option's picture must not stay up while this
        # one downloads (N8); the label is placed at the image box's full size, so
        # clearing it does not change the layout.
        counter_label.config(text=f"{index + 1} / {len(state['option_urls'])} (loading...)")
        _show_text_only(img_label, "loading...")
        loading = state.setdefault("_loading", set())
        if index in loading:
            return  # already downloading this option; it shows when it lands
        loading.add(index)
        # The option's preview source (a thumbnail, or the full file — artwork.preview_url),
        # never the full image a thumbnail stands for.
        url = state["preview_urls"][index]
        ext = url.split(".")[-1].split("?")[0]
        # The {base}_{i} cache name the fetch's own previews use — never renamed.
        cache_path = f"{state['cache_base']}_{index}.{ext}"

        def finish(ok, expected=index, _on_anim=on_animated):
            # Tk thread: file the preview under ITS index (B9: appending in
            # finish order showed option 5's image under option 4).
            loading.discard(expected)
            if ok:
                state["paths"][expected] = cache_path
            if state["index"] != expected:
                return
            counter_label.config(text=f"{expected + 1} / {len(state['option_urls'])}")
            if ok and animated:
                load_animated(cache_path)      # the first show after its download, too
            elif ok:
                _display_image_on_label(img_label, cache_path, _on_anim)
            else:
                _show_unavailable(img_label)

        if animated and loader is not None:
            # A big animated file: the card shows its size, progress and Cancel
            # (Q19); finish(ok) runs on the Tk thread when it lands. A cancelled
            # download is not "loading" any more, so a revisit can start it again.
            _show_text_only(img_label, "")
            counter_label.config(text=plain_counter)   # the panel says what is happening
            loading.discard(index)
            loader.start(index, url, cache_path, finish)
            return

        def land(future):
            try:
                ok = future.result()
            except Exception:
                ok = False
            finish(ok)

        # The download (worker) goes through the HTTP seam with a status check (a
        # non-200 writes no file) and lands atomically (.part + os.replace).
        _when_done(img_label, _FETCH_POOL.submit(artwork.download_artwork, url,
                                                 cache_path, False), land)
