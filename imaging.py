"""Off-thread image helpers for NonSteamScraper's results/list views.

All PIL decoding happens on a worker thread; ImageTk.PhotoImage creation and every
widget mutation are marshaled back to the Tk thread via ``widget.after(0, ...)``
(Tk requires that). Staleness tokens stamped on the target label let a slow decode
self-discard if a newer image has since been requested, and PhotoImage references
are kept (on the widget and/or a cache) so they aren't garbage-collected to blank.
"""

import threading
import requests
from PIL import Image, ImageTk


def _run_animation(lbl, img, frame):
    """Advance one frame of an animated image on a label. Silently stops if the widget is destroyed."""
    try:
        img.seek(frame % img.n_frames)
        copy = img.copy()
        copy.thumbnail((300, 200))
        photo = ImageTk.PhotoImage(copy)
        lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
        lbl.image = photo
        delay = img.info.get("duration", 100)
        lbl._anim_id = lbl.after(delay, lambda: _run_animation(lbl, img, frame + 1))
    except Exception:
        pass


def _update_badge(badge_label, meta):
    """Show or hide the top-left badge overlay based on image metadata."""
    parts = []
    if meta.get("animated"): parts.append("▶")
    if meta.get("nsfw"):     parts.append("18+")
    if meta.get("humor"):    parts.append("MEME")
    text = " ".join(parts)
    badge_label.config(text=text)
    if text:
        badge_label.place(x=4, y=4)
    else:
        badge_label.place_forget()


def _display_image_on_label(lbl, path, on_animated=None, cache=None, cache_key=None):
    """Display the image at path on lbl. Plays frame-by-frame if the image is animated.
    on_animated(bool) is called with the actual animation status detected from the file.

    PIL work (Image.open + thumbnail) happens on a worker thread; ImageTk.PhotoImage
    and all widget mutations happen back on the UI thread (Tk requirement).

    Staleness guard: a token is stamped on the label before the worker starts; if the
    token has been replaced by the time the worker finishes (user moved to a different
    image), the result is silently discarded.

    cache/cache_key (#6): when both are given, a successfully-built STATIC PhotoImage is
    stored as cache[cache_key] so a later revisit can assign it synchronously and skip
    this off-thread decode. Animated images are never cached.
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

    def worker():
        try:
            img = Image.open(path)
            is_animated = getattr(img, "n_frames", 1) > 1

            if is_animated:
                # Image object is opened in the worker; frame iteration starts on the
                # UI thread (via after) because _run_animation calls lbl.after internally.
                def apply_animated():
                    if lbl._load_token is not token:
                        return  # stale — a newer image won the race
                    if on_animated:
                        on_animated(True)
                    _run_animation(lbl, img, 0)
                lbl.after(0, apply_animated)
            else:
                img.thumbnail((300, 200))
                # Copy so the original can be GC'd; thumbnail mutates in-place.
                copy = img.copy()

                def apply_static():
                    if lbl._load_token is not token:
                        return  # stale
                    if on_animated:
                        on_animated(False)
                    photo = ImageTk.PhotoImage(copy)
                    lbl.config(image=photo, text="",
                               width=photo.width(), height=photo.height())
                    lbl.image = photo  # keep a reference so GC doesn't collect it
                    # Static-only cache for the results cycle (#6): store the built
                    # PhotoImage so a revisit to this index assigns it synchronously.
                    if cache is not None and cache_key is not None:
                        cache[cache_key] = photo
                lbl.after(0, apply_static)
        except Exception:
            def apply_error():
                if lbl._load_token is not token:
                    return
                lbl.config(text="preview\nunavailable")
            lbl.after(0, apply_error)

    threading.Thread(target=worker, daemon=True).start()


def _decode_row_thumb(executor, lbl, path, box, cache, cache_key, on_done=None):
    """Decode a small row-cover thumbnail OFF the UI thread on a bounded `executor`,
    then create the ImageTk.PhotoImage and assign it ON the UI thread via lbl.after().

    Correctness guards (a past project bug came from off-thread image handling):
      * Staleness token — a fresh token is stamped on `lbl` before the work is queued;
        if the label is rebuilt/re-targeted (e.g. the list re-renders mid-decode) the
        token changes and the stale result self-discards.
      * winfo_exists() — the label may be destroyed by a re-render before the worker
        finishes; assigning to a dead widget would raise, so we check first.
      * `box` is (w, h); thumbnail() keeps aspect within it. PhotoImage is created on
        the UI thread (Tk requirement) and stored in `cache[cache_key]` AND on the
        widget so neither is garbage-collected (a dropped reference renders blank).

    `on_done(photo)` (optional) runs on the UI thread after a successful assign."""
    token = object()
    lbl._thumb_token = token

    def worker():
        try:
            img = Image.open(path)
            # Animated covers: just show the first frame as a static thumbnail — rows
            # don't animate. convert() flattens any palette/alpha for a clean resize.
            if getattr(img, "n_frames", 1) > 1:
                img.seek(0)
            img = img.convert("RGBA")
            img.thumbnail(box)
            copy = img.copy()
        except Exception:
            return  # unreadable file: leave whatever placeholder is already shown

        def apply_static():
            if getattr(lbl, "_thumb_token", None) is not token:
                return  # stale — a newer render won the race
            try:
                if not lbl.winfo_exists():
                    return  # widget destroyed by a re-render
            except Exception:
                return
            photo = ImageTk.PhotoImage(copy)
            lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
            lbl.image = photo            # widget reference: GC guard
            cache[cache_key] = photo     # cache reference: reuse + GC guard
            if on_done:
                on_done(photo)

        try:
            lbl.after(0, apply_static)
        except Exception:
            pass  # window/label gone

    try:
        executor.submit(worker)
    except Exception:
        pass  # executor shut down (window closing)


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

    if index < len(state["paths"]):
        # Per-index decoded-image cache (#6): revisiting an index via ◀/▶ should be
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
        else:
            _display_image_on_label(img_label, state["paths"][index], on_animated,
                                    cache=cache, cache_key=index)
    else:
        # Fetch on demand — keep the current image visible while downloading to
        # avoid the label reverting to character-unit dimensions mid-load.
        counter_label.config(text=f"{index + 1} / {len(state['option_urls'])} (loading...)")
        url = state["option_urls"][index]

        def fetch(expected=index, _on_anim=on_animated):
            try:
                r = requests.get(url, stream=True, timeout=10)
                ext = url.split(".")[-1].split("?")[0]
                cache_path = state["paths"][0].rsplit("_", 1)[0] + f"_{expected}.{ext}"
                with open(cache_path, "wb") as f:
                    f.write(r.content)
                state["paths"].append(cache_path)
                if state["index"] == expected:
                    img_label.after(0, lambda: _display_image_on_label(img_label, cache_path, _on_anim))
                    counter_label.after(0, lambda: counter_label.config(
                        text=f"{expected + 1} / {len(state['option_urls'])}"))
            except Exception:
                if state["index"] == expected:
                    img_label.after(0, lambda: img_label.config(text="preview\nunavailable"))

        threading.Thread(target=fetch, daemon=True).start()
