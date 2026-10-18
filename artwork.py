"""Artwork into Steam's grid folder: download one image (.part + length check +
os.replace), install_into_slot — the ONE way a new image lands in a slot,
static or animated (APNG encoded in the cache, then moved in) — the results
window's apply (install_option), a whole game's fetch (download_all_artwork,
returning a FetchedArt), the row thumbnail pick, and Clear All Artwork.
tkinter-free.

The grid folder is the active account's, read live from steam_env."""

from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import tempfile
import threading
from appcommon import debug as _debug
import appfiles
import restore
import sgdb
import steam_env
import steam_files


def clear_managed_artwork():
    managed = appfiles.load_managed_files(); deleted = 0
    for p in managed:
        if os.path.exists(p): os.remove(p); deleted += 1
    if os.path.exists(appfiles.MANAGED_FILE): os.remove(appfiles.MANAGED_FILE)
    # The restore point snapshots the very art being cleared; leaving it behind
    # would let a later Undo silently re-copy and re-register everything the
    # operator just removed ("Clear All Artwork" promises to remove it all).
    restore.clear_restore_point()
    return deleted

def download_artwork(url, save_path, register=True):
    # Download to a temp file and only swap it into place once it's complete, so a
    # dropped connection can never leave a truncated image that gets applied/registered.
    tmp = save_path + ".part"
    try:
        r = sgdb.http_get(url, stream=True, timeout=10)
        if r.status_code != 200:
            print(f"    ❌ Failed ({r.status_code}): {url}"); return False
        written = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk); written += len(chunk)
        expected = r.headers.get("Content-Length")
        # A compressed response (Steam's icon CDN gzips .ico files) states the
        # COMPRESSED length while iter_content yields decoded bytes, so the byte
        # count only means something for an unencoded body. A short compressed body
        # is still caught: urllib3 enforces Content-Length on the raw stream and
        # raises IncompleteRead (enforce_content_length defaults to True).
        encoded = r.headers.get("Content-Encoding", "identity").lower() not in ("", "identity")
        if not encoded and expected and expected.isdigit() and written != int(expected):
            raise IOError(f"incomplete download: {written}/{expected} bytes")
        os.replace(tmp, save_path)  # atomic within the same folder
        if register:
            appfiles.register_managed_file(save_path)
        print(f"    ✅ Saved: {os.path.basename(save_path)}"); return True
    except Exception as e:
        _debug("download_artwork")
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass
        print(f"    ❌ Network error: {e}"); return False

def clear_slot_files(save_path, existing_files=None, keep=None):
    """Delete any existing artwork sharing `save_path`'s grid slot but using a
    different extension. Steam keys art off the filename base (e.g. '12345p'), so a
    leftover '12345p.png' would keep displaying instead of a new '12345p.webp' —
    this guarantees the freshly downloaded file is the only one Steam sees.
    The previous file is already preserved by snapshot_for_restore() before this runs.

    existing_files: optional pre-listed folder snapshot to avoid a redundant listdir
    (download_all_artwork shares ONE snapshot across this game's slots — safe because
    each slot has a distinct filename base). Standalone callers leave it None.
    keep: a filename to leave alone — install_into_slot passes the file it has just
    installed, which an older snapshot lists under the same name as the old art."""
    folder = os.path.dirname(save_path)
    prefix = os.path.basename(save_path).rsplit(".", 1)[0] + "."
    if existing_files is None:
        try:
            existing_files = os.listdir(folder)
        except OSError:
            return
    for f in existing_files:
        if f.startswith(prefix) and f != keep:
            try: os.remove(os.path.join(folder, f))
            except OSError: pass

def remove_partial_downloads(grid_folder, unsigned_ids):
    """Delete the .part files a stopped fetch left behind: in `grid_folder`, the
    ones whose leading digit run is one of `unsigned_ids` (never another game's),
    and the matching preview downloads in CACHE_FOLDER. A .part is only ever this
    app's own unfinished download, never the user's art. Returns the number
    removed. Never raises."""
    ids = {str(i) for i in unsigned_ids}
    removed = 0
    for folder in (grid_folder, appfiles.CACHE_FOLDER):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for f in names:
            if not f.endswith(".part"):
                continue
            m = re.match(r"(\d+)", f)
            if m and m.group(1) in ids:
                try:
                    os.remove(os.path.join(folder, f))
                    removed += 1
                except OSError:
                    _debug("remove_partial_downloads")
    return removed

def install_into_slot(save_path, url, existing_files=None, animated=False,
                      progress_cb=None, status_cb=None):
    """The ONE way a new image lands in a grid slot — used by the fetch, by the
    results window's "Apply this one" and by the animated (APNG) apply. First get the
    new file into place atomically: a static image through download_artwork (a .part
    file, a length check, then os.replace onto save_path); an animated one through
    _apng_into_place (download + encode in the cache, then .part + os.replace). Only
    when that succeeded clear the slot's other extensions and register the file. A
    failure changes nothing in the grid folder: the old art stays (B5 — clearing
    first used to delete it even when the download then failed).
    Order note: the replace comes BEFORE the clear, so a crash between the two leaves
    the new file plus an old one of another extension, never an empty slot.
    progress_cb / status_cb are the APNG encode callbacks. Returns True when the
    new file is in place."""
    if animated:
        if not _apng_into_place(url, save_path, progress_cb, status_cb):
            return False
    elif not download_artwork(url, save_path, register=False):
        return False
    clear_slot_files(save_path, existing_files=existing_files,
                     keep=os.path.basename(save_path))
    appfiles.register_managed_file(save_path)
    return True

def install_option(save_path, url, unsigned_id, is_icon=False, animated=False,
                   progress_cb=None, status_cb=None):
    """The results window's "Apply this one", static or animated: install the
    option into its slot (install_into_slot), then, for the icon slot, point
    Steam's shortcut at it (apply_icon). An animated icon is an APNG served as
    image/png; it goes through the same icon write as a static one (C4: the
    animated apply used to place the file and never touch shortcuts.vdf).
    Returns (ok, queued): ok when the file is in place, queued when the icon
    write waits for Steam to close."""
    ok = install_into_slot(save_path, url, animated=animated,
                           progress_cb=progress_cb, status_cb=status_cb)
    queued = False
    if ok and is_icon:
        queued = steam_files.apply_icon(unsigned_id, save_path)
    return ok, queued

def row_thumbnail_path(grid_folder, app_id, names=None):
    """Return the best representative art file for `app_id` to show as a list-row
    thumbnail, or None if the game has no cover art on disk.

    Slot preference (Steam keys art off the filename base):
      1. portrait cover  "{app_id}p.<ext>"   (preferred — rows are portrait-shaped)
      2. wide grid       "{app_id}.<ext>"     (fallback)
    Hero ("{app_id}_hero.*"), logo ("{app_id}_logo.*") and icon ("{app_id}_icon.*")
    slots are intentionally ignored — they don't read well at row size.

    Pure / tkinter-free so it can be unit-tested headless. Returns an absolute path
    using whichever extension is present (png/jpg/webp/…); if several extensions
    share a slot, the lexicographically-first is returned for determinism.

    names: the folder's listing, when the caller already has one — a list render
    lists the grid folder ONCE and passes it to every row (B11: one listdir per
    row was 300+ listings per keystroke on a big library)."""
    if names is None:
        try:
            names = os.listdir(grid_folder)
        except OSError:
            return None

    portrait_prefix = f"{app_id}p."
    wide_prefix = f"{app_id}."

    portrait, wide = [], []
    for name in names:
        if name.startswith(portrait_prefix):
            portrait.append(name)
        elif name.startswith(wide_prefix):
            # Exclude hero/logo/icon slots — their base is "{app_id}_…", which does
            # NOT start with "{app_id}." so they're already excluded here. This branch
            # only catches the true wide slot "{app_id}.<ext>".
            wide.append(name)

    chosen = sorted(portrait)[0] if portrait else (sorted(wide)[0] if wide else None)
    if chosen is None:
        return None
    return os.path.join(grid_folder, chosen)

def is_animated_image(img):
    """True if an SGDB image record refers to animated artwork. SGDB serves animated
    grids/heroes as webp/gif; static art is png/jpg."""
    return img.get("mime", "") in ("image/webp", "image/gif")

class _ApngFrameCounter:
    """Wraps the output file so we can report real per-frame progress during Pillow's
    otherwise-opaque APNG encode. Pillow writes each frame's 8-byte 'fcTL' chunk header
    as a discrete write() right before compressing that frame's (slow) data, so counting
    those headers tracks encode progress linearly in real time."""
    def __init__(self, fp, total, progress_cb):
        self._fp = fp
        self._total = total
        self._cb = progress_cb
        self._count = 0
    def write(self, b):
        if self._cb is not None and len(b) == 8 and bytes(b[-4:]) == b"fcTL":
            self._count += 1
            self._cb(self._count, self._total)
        return self._fp.write(b)
    def __getattr__(self, name):
        return getattr(self._fp, name)

def convert_animated_to_apng(src_path, out_path, progress_cb=None, status_cb=None):
    """Convert an animated webp/gif at src_path to APNG at out_path (.png) — the only
    animated format Steam's library renders. (Steam ignores .webp/.gif in the grid
    folder, so animated covers must be APNG.) progress_cb(done, total) fires per frame
    during the slow encode; status_cb(message) reports the current phase. Returns True
    on success."""
    from PIL import Image
    im = Image.open(src_path)
    n = getattr(im, "n_frames", 1)
    if status_cb: status_cb("Reading frames…")
    frames, durations = [], []
    for i in range(n):
        im.seek(i)
        durations.append(im.info.get("duration", 60))
        frames.append(im.convert("RGBA"))
    if status_cb: status_cb("Converting to APNG…")
    with open(out_path, "wb") as raw:
        fp = _ApngFrameCounter(raw, n, progress_cb)
        frames[0].save(fp, format="PNG", save_all=True,
                       append_images=frames[1:], duration=durations, loop=0, disposal=2)
    return True

def _apng_into_place(url, out_path, progress_cb=None, status_cb=None):
    """Download an animated webp/gif from `url`, convert it to APNG and put it at
    out_path. The source download and the slow encode (seconds to minutes) both
    happen in CACHE_FOLDER, never in Steam's grid folder (B7: encoding straight into
    the live file left a half-written PNG for Steam to read if the app closed). The
    finished APNG is copied next to out_path as a .part and os.replace'd into place —
    a copy, because the cache and the grid folder may be on different drives.
    progress_cb(done, total) reports per-frame encode progress; status_cb(message)
    reports the current phase. Leaves no temp file behind. Returns True on success."""
    if status_cb:
        status_cb("Downloading…")
    os.makedirs(appfiles.CACHE_FOLDER, exist_ok=True)
    fd, work = tempfile.mkstemp(prefix="apng_", dir=appfiles.CACHE_FOLDER)
    os.close(fd)
    src = f"{work}.src.{url.split('.')[-1].split('?')[0]}"
    encoded = f"{work}.png"
    part = out_path + ".part"
    try:
        if not download_artwork(url, src, register=False):
            return False
        convert_animated_to_apng(src, encoded, progress_cb, status_cb)
        shutil.copyfile(encoded, part)
        os.replace(part, out_path)
        return True
    except Exception as e:
        _debug("_apng_into_place")
        print(f"    ❌ APNG conversion failed: {e}"); return False
    finally:
        for p in (work, src, encoded, part):
            try:
                if os.path.exists(p): os.remove(p)
            except OSError:
                pass

class FetchedArt:
    """What download_all_artwork did for one game, in two separate fields so no
    consumer can mistake one kind of value for the other (T-A6: one dict used to
    hold the slot dicts AND the deferred icon tuple, and a loop over its values
    crashed on the tuple and left the UI on "Fetching…").
      slots       {slot key: slot dict, or None when the slot found nothing}, in
                  slot order: grids, grids_wide, heroes, logos, icons.
      icon_write  (unsigned_id, icon_path) of an icon installed with
                  defer_icon=True, for the caller's batched shortcuts.vdf write;
                  None otherwise."""

    def __init__(self):
        self.slots = {}
        self.icon_write = None

    def applied_paths(self):
        """The applied_path of every slot that auto-applied a static image."""
        return [d["applied_path"] for d in self.slots.values()
                if d and d.get("applied_index") is not None and d.get("applied_path")]

    def failed_labels(self):
        """The label ("Cover", "Icon", …) of every slot whose download failed."""
        return [d.get("label") or key for key, d in self.slots.items()
                if d and d.get("failed")]

def download_all_artwork(sgdb_id, unsigned_id, prefs=None, progress_cb=None, defer_icon=False):
    """Fetch and install one game's artwork from SGDB id `sgdb_id`; returns a
    FetchedArt. With defer_icon=True the icon's shortcuts.vdf write is left to the
    caller (FetchedArt.icon_write) so a whole run writes once."""
    if prefs is None:
        prefs = appfiles.load_prefs()
    # slot key -> (label, SGDB endpoint, filename base, dimensions filter)
    # Portrait and wide covers share the "grids" endpoint, split by dimensions.
    slots = {
        "grids":      ("Cover",           "grids",  f"{unsigned_id}p",     "600x900,660x930"),
        "grids_wide": ("Wide Cover",      "grids",  f"{unsigned_id}",      "460x215,920x430"),
        "heroes":     ("Hero/Background", "heroes", f"{unsigned_id}_hero", None),
        "logos":      ("Logo",            "logos",  f"{unsigned_id}_logo", None),
        "icons":      ("Icon",            "icons",  f"{unsigned_id}_icon", None),
    }
    # One GRID_FOLDER snapshot reused by snapshot_for_restore + every clear_slot_files
    # below, instead of ~6 listdir calls per game. Safe to share within ONE game: each
    # slot has a distinct filename base, so a slot's freshly written file never affects
    # another slot's clear. (Taken fresh per call — never shared across games.)
    try:
        existing_files = os.listdir(steam_env.GRID_FOLDER)
    except OSError:
        existing_files = []
    restore.snapshot_for_restore(unsigned_id, existing_files=existing_files)
    os.makedirs(appfiles.CACHE_FOLDER, exist_ok=True)
    results = FetchedArt()
    # N slots × (1 main + 3 previews) steps max
    total_steps = len(slots) * 4
    step = 0
    # Progress is bumped from concurrent preview threads, so guard the counter.
    step_lock = threading.Lock()

    def bump(label):
        nonlocal step
        with step_lock:
            step += 1
            cur = step
        if progress_cb:
            progress_cb(label, cur, total_steps)

    # --- Phase 1: fetch all slots' metadata concurrently (bounded) -------------
    # Each get_artwork is an independent, network-bound round-trip, so running the
    # five together collapses ~5 serial round-trips into ~1.
    def fetch_slot(item):
        slot, (art_label, endpoint, base, dimensions) = item
        return slot, sgdb.get_artwork(sgdb_id, endpoint, prefs, dimensions=dimensions)

    # The Steam-link lookup is one more independent round-trip, so it rides in the
    # same pool as the five slot fetches.
    with ThreadPoolExecutor(max_workers=sgdb.SGDB_MAX_WORKERS) as ex:
        steam_future = ex.submit(sgdb.get_steam_platform_data, sgdb_id)
        slot_images = dict(ex.map(fetch_slot, slots.items()))
        steam_link = steam_future.result()
    steam_images = sgdb.steam_official_images(*steam_link) if steam_link else {}

    # --- Phase 2: per slot (declared order, so results/progress stay deterministic),
    # download the applied art then the previews (previews run concurrently) -----
    for slot, (art_label, endpoint, base, dimensions) in slots.items():
        images = list(slot_images.get(slot) or [])
        # Steam's own store asset always goes FIRST (so it is the auto-applied pick),
        # even when SGDB has nothing for this slot. The only thing that drops it is a
        # Logo/Icon "Official = Exclude" pref (_strip_never on its "official" style).
        official = sgdb._strip_never([steam_images[slot]], endpoint, prefs) if slot in steam_images else []
        images = official + images
        if not images:
            # A missing slot still consumes its 1 main + 3 preview steps so the bar
            # reaches 100% exactly (same accounting as the serial version).
            for _ in range(4):
                bump(f"{art_label} — none found")
            results.slots[slot] = None
            continue
        # Keep SGDB's original ranking so animated options stay visible in the results
        # (don't resort static-first — that can push animated past the 5-option cap).
        # Animated art is NOT auto-applied (Steam needs a slow APNG conversion), so we
        # auto-apply the first STATIC option and leave animated ones as alternatives
        # the user commits on demand.
        options = images[:5]
        applied_index = next((i for i, im in enumerate(options)
                              if not is_animated_image(im)), None)

        # Target path: the auto-applied static file keeps its own extension; if there
        # is nothing to auto-apply (all options animated), the slot stays empty until
        # the user applies one, which will be written as APNG (.png).
        failed = False
        if applied_index is not None:
            top = options[applied_index]
            ext = top["url"].split(".")[-1].split("?")[0]
            save_path = os.path.join(steam_env.GRID_FOLDER, f"{base}.{ext}")
            bump(f"Downloading {art_label}")
            if not install_into_slot(save_path, top["url"], existing_files=existing_files):
                # The download failed and the old art is untouched. Nothing was
                # applied, so applied_index is None (FetchedArt.applied_paths skips
                # the slot) and no icon write may point Steam at a missing file.
                failed = True
                applied_index = None
            elif slot == "icons":
                if defer_icon:
                    # Defer the shortcuts.vdf write so the caller can batch + write all
                    # icons in ONE atomic pass (and while Steam is closed).
                    results.icon_write = (unsigned_id, save_path)
                else:
                    steam_files.set_shortcut_icon(unsigned_id, save_path)
        else:
            save_path = os.path.join(steam_env.GRID_FOLDER, f"{base}.png")
            bump(f"{art_label} — animated, apply to commit")

        # Previews: first 3 options, downloaded at FULL resolution (img["url"]) using
        # the original {base}_{i} cache names — unchanged from the serial version, so
        # animated previews still animate and app.py's on-demand cycling path (which
        # reconstructs {base}_{i} names) stays valid. They write to DISTINCT paths, so
        # downloading them concurrently is safe.
        preview_imgs = list(enumerate(options[:3]))

        def fetch_preview(arg):
            i, img = arg
            u = img["url"]; e = u.split(".")[-1].split("?")[0]
            tp = os.path.join(appfiles.CACHE_FOLDER, f"{base}_{i}.{e}")
            bump(f"Caching {art_label} preview {i+1}/3")
            download_artwork(u, tp, register=False)
            return i, tp

        if preview_imgs:
            with ThreadPoolExecutor(max_workers=sgdb.SGDB_MAX_WORKERS) as ex:
                pairs = list(ex.map(fetch_preview, preview_imgs))
            # Re-sort by index so thumb_paths still lines up with options 0..2.
            thumbs = [tp for _i, tp in sorted(pairs, key=lambda p: p[0])]
        else:
            thumbs = []

        results.slots[slot] = {"applied_path": save_path,
                               "applied_index": applied_index,
                               "failed": failed,
                               "label": art_label,
                               "option_urls": [img["url"] for img in options],
                               "option_meta": [
                                   {
                                       "animated": is_animated_image(img),
                                       "nsfw":     bool(img.get("nsfw", False)),
                                       "humor":    bool(img.get("humor", False)),
                                       "steam":    bool(img.get("steam_official", False)),
                                   }
                                   for img in options
                               ],
                               "thumb_paths": thumbs, "filename_base": base}
    return results
