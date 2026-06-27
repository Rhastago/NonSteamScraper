"""Pytest suite for the pure / isolatable logic in find_games.py.

SAFETY NOTES
------------
Importing ``find_games`` runs module-level code that binds path constants
(SKIP_FILE, NAMES_FILE, PREFS_FILE, MANAGED_FILE, GRID_FOLDER, CACHE_FOLDER,
BACKUP_FOLDER) to *real* paths under the user's home and may even create the
Steam grid folder. This machine has Steam installed, so those constants point
at the user's real data.

To guarantee we never touch real user files, this module sets HOME to a throw-
away temp directory *before* importing find_games (see the import block below),
so every ``os.path.expanduser("~/...")`` constant is rebased into that temp dir
at import time. On top of that, every test that exercises a function reading one
of the module-level path constants ALSO monkeypatches that specific constant
onto a pytest ``tmp_path`` so each test is fully isolated and deterministic.

No test makes a real network call. The only network-touching function tested is
``_sgdb_get``, whose ``requests.get`` and ``time.sleep`` are monkeypatched.
"""

import os
import sys
import json
import tempfile

import pytest

# --- Import find_games with a sandboxed HOME so module-level path constants
# --- never resolve to the real user's ~/.steamart_* files. ----------------
_SANDBOX_HOME = tempfile.mkdtemp(prefix="steamart_test_home_")
os.environ["HOME"] = _SANDBOX_HOME
# Also neutralise Windows-style home resolution just in case.
os.environ["USERPROFILE"] = _SANDBOX_HOME

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import find_games as fg  # noqa: E402

from PIL import Image  # noqa: E402


# ---------------------------------------------------------------------------
# _release_year
# ---------------------------------------------------------------------------

def test_release_year_valid_timestamp():
    # 2021-01-01 00:00:00 UTC
    assert fg._release_year(1609459200) == "2021"


def test_release_year_valid_string_timestamp():
    # The function int()s its input, so a numeric string works too.
    assert fg._release_year("1609459200") == "2021"


@pytest.mark.parametrize("falsy", [0, None, ""])
def test_release_year_falsy_returns_empty(falsy):
    assert fg._release_year(falsy) == ""


def test_release_year_non_numeric_returns_empty():
    assert fg._release_year("not-a-number") == ""


# ---------------------------------------------------------------------------
# _pref_params
# ---------------------------------------------------------------------------

def test_pref_params_animated_never_static():
    assert fg._pref_params({"animated": "never"})["types"] == "static"


def test_pref_params_animated_must_have_animated():
    assert fg._pref_params({"animated": "must_have"})["types"] == "animated"


def test_pref_params_animated_ok_no_types_key():
    assert "types" not in fg._pref_params({"animated": "ok"})


def test_pref_params_animated_absent_no_types_key():
    # Default for animated is "never" -> the code DOES add types=static when absent.
    # Verify the documented mapping: absent -> default "never" -> types=static.
    assert fg._pref_params({})["types"] == "static"


@pytest.mark.parametrize(
    "value,expected",
    [("never", "false"), ("must_have", "true"), ("ok", "any"), ("anything_else", "any")],
)
def test_pref_params_nsfw_mapping(value, expected):
    assert fg._pref_params({"nsfw": value})["nsfw"] == expected


@pytest.mark.parametrize(
    "value,expected",
    [("never", "false"), ("must_have", "true"), ("ok", "any"), ("whatever", "any")],
)
def test_pref_params_humor_mapping(value, expected):
    assert fg._pref_params({"humor": value})["humor"] == expected


def test_pref_params_nsfw_humor_absent_default_any():
    # Default for nsfw/humor in this helper is "ok" -> "any".
    p = fg._pref_params({})
    assert p["nsfw"] == "any"
    assert p["humor"] == "any"


# ---------------------------------------------------------------------------
# is_animated_image
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mime", ["image/webp", "image/gif"])
def test_is_animated_image_true(mime):
    assert fg.is_animated_image({"mime": mime}) is True


@pytest.mark.parametrize("img", [{"mime": "image/png"}, {"mime": "image/jpeg"}, {}])
def test_is_animated_image_false(img):
    assert fg.is_animated_image(img) is False


# ---------------------------------------------------------------------------
# _get_styles / _strip_never  (consults _STYLE_MAP)
# ---------------------------------------------------------------------------

def test_get_styles_grids_selects_matching_value():
    prefs = {
        "grid_no_logo": "must_have",
        "grid_blurred": "never",
        "grid_alternate": "must_have",
        "grid_material": "ok",
    }
    got = fg._get_styles("grids", prefs, "must_have")
    # _STYLE_MAP maps grid_no_logo->no_logo, grid_alternate->alternate
    assert set(got) == {"no_logo", "alternate"}


def test_get_styles_value_never():
    prefs = {"grid_blurred": "never", "grid_material": "never", "grid_no_logo": "ok"}
    assert set(fg._get_styles("grids", prefs, "never")) == {"blurred", "material"}


def test_get_styles_unknown_art_type_returns_empty():
    assert fg._get_styles("nonexistent", {"anything": "must_have"}, "must_have") == []


def test_get_styles_heroes_mapping():
    prefs = {"hero_alternate": "must_have", "hero_blurred": "ok"}
    assert fg._get_styles("heroes", prefs, "must_have") == ["alternateArt"]


def test_strip_never_filters_never_styles():
    prefs = {"grid_blurred": "never"}  # blurred is a "never" style
    results = [
        {"style": "blurred", "url": "a"},
        {"style": "alternate", "url": "b"},
        {"style": "material", "url": "c"},
    ]
    filtered = fg._strip_never(results, "grids", prefs)
    assert [r["url"] for r in filtered] == ["b", "c"]


def test_strip_never_no_never_returns_input_unchanged():
    prefs = {"grid_blurred": "ok", "grid_material": "ok"}
    results = [{"style": "blurred", "url": "a"}, {"style": "material", "url": "b"}]
    out = fg._strip_never(results, "grids", prefs)
    # When there are no "never" styles the function returns the original list object.
    assert out is results


def test_strip_never_keeps_records_with_unknown_style():
    prefs = {"grid_blurred": "never"}
    results = [{"style": "weird_unmapped", "url": "x"}, {"style": "blurred", "url": "y"}]
    filtered = fg._strip_never(results, "grids", prefs)
    assert [r["url"] for r in filtered] == ["x"]


# ---------------------------------------------------------------------------
# clear_slot_files  (slot isolation by filename base)
# ---------------------------------------------------------------------------

def _touch(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def test_clear_slot_files_portrait_slot_isolated(tmp_path):
    # Portrait slot "123p" must not collide with wide "123", hero, or other appids.
    _touch(tmp_path / "123p.png")
    _touch(tmp_path / "123.png")
    _touch(tmp_path / "123_hero.png")
    _touch(tmp_path / "999p.png")

    fg.clear_slot_files(str(tmp_path / "123p.webp"))

    assert not (tmp_path / "123p.png").exists()       # removed: same slot, diff ext
    assert (tmp_path / "123.png").exists()             # kept: wide slot
    assert (tmp_path / "123_hero.png").exists()        # kept: hero slot
    assert (tmp_path / "999p.png").exists()            # kept: other appid


def test_clear_slot_files_wide_slot_isolated(tmp_path):
    _touch(tmp_path / "123p.png")
    _touch(tmp_path / "123.png")
    _touch(tmp_path / "123_hero.png")
    _touch(tmp_path / "999p.png")

    fg.clear_slot_files(str(tmp_path / "123.gif"))

    assert not (tmp_path / "123.png").exists()         # removed: wide slot match
    assert (tmp_path / "123p.png").exists()            # kept: portrait slot
    assert (tmp_path / "123_hero.png").exists()        # kept: hero slot
    assert (tmp_path / "999p.png").exists()            # kept: other appid


def test_clear_slot_files_missing_folder_no_error(tmp_path):
    # listdir on a nonexistent folder raises OSError, which the function swallows.
    fg.clear_slot_files(str(tmp_path / "no_such_dir" / "123p.webp"))  # must not raise


# ---------------------------------------------------------------------------
# Skip-list round-trip (with de-dup guard)
# ---------------------------------------------------------------------------

def test_skip_list_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "SKIP_FILE", str(tmp_path / "skip"))
    assert fg.load_skip_list() == set()


def test_skip_list_add_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "SKIP_FILE", str(tmp_path / "skip"))
    fg.add_to_skip_list(111)
    fg.add_to_skip_list(222)
    assert fg.load_skip_list() == {"111", "222"}


def test_skip_list_dedup_guard(tmp_path, monkeypatch):
    skip = tmp_path / "skip"
    monkeypatch.setattr(fg, "SKIP_FILE", str(skip))
    fg.add_to_skip_list(111)
    fg.add_to_skip_list(111)  # duplicate -> should not append a second line
    with open(skip, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert lines == ["111"]
    assert fg.load_skip_list() == {"111"}


# ---------------------------------------------------------------------------
# Name overrides round-trip
# ---------------------------------------------------------------------------

def test_name_overrides_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "NAMES_FILE", str(tmp_path / "names"))
    assert fg.load_name_overrides() == {}


def test_name_overrides_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "NAMES_FILE", str(tmp_path / "names"))
    fg.save_name_override(123, "Real Game Name")
    fg.save_name_override(456, "Another Title")
    assert fg.load_name_overrides() == {"123": "Real Game Name", "456": "Another Title"}


def test_name_overrides_update_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "NAMES_FILE", str(tmp_path / "names"))
    fg.save_name_override(123, "Old")
    fg.save_name_override(123, "New")
    assert fg.load_name_overrides() == {"123": "New"}


def test_name_overrides_name_with_pipe_preserved_on_load(tmp_path, monkeypatch):
    # load splits on the FIRST pipe only, so a name containing '|' survives load.
    monkeypatch.setattr(fg, "NAMES_FILE", str(tmp_path / "names"))
    with open(tmp_path / "names", "w", encoding="utf-8") as f:
        f.write("789|Game | With Pipe\n")
    assert fg.load_name_overrides() == {"789": "Game | With Pipe"}


# ---------------------------------------------------------------------------
# Prefs round-trip
# ---------------------------------------------------------------------------

def test_load_prefs_returns_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PREFS_FILE", str(tmp_path / "prefs"))
    prefs = fg.load_prefs()
    assert prefs == fg.DEFAULT_PREFS
    # Must be a fresh dict, not the module default object (mutation safety).
    assert prefs is not fg.DEFAULT_PREFS


def test_save_and_load_prefs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PREFS_FILE", str(tmp_path / "prefs"))
    custom = dict(fg.DEFAULT_PREFS)
    custom["animated"] = "must_have"
    custom["nsfw"] = "ok"
    fg.save_prefs(custom)
    assert fg.load_prefs() == custom


def test_load_prefs_filters_unknown_keys(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs"
    monkeypatch.setattr(fg, "PREFS_FILE", str(prefs_file))
    with open(prefs_file, "w", encoding="utf-8") as f:
        json.dump({"animated": "ok", "bogus_key": "zzz"}, f)
    loaded = fg.load_prefs()
    assert "bogus_key" not in loaded
    assert loaded["animated"] == "ok"
    # All default keys still present.
    assert set(loaded) == set(fg.DEFAULT_PREFS)


def test_load_prefs_corrupt_json_falls_back_to_defaults(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs"
    monkeypatch.setattr(fg, "PREFS_FILE", str(prefs_file))
    with open(prefs_file, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ]")
    assert fg.load_prefs() == fg.DEFAULT_PREFS


# ---------------------------------------------------------------------------
# Managed-file registry round-trip
# ---------------------------------------------------------------------------

def test_load_managed_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "MANAGED_FILE", str(tmp_path / "managed"))
    assert fg.load_managed_files() == set()


def test_register_and_load_managed(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "MANAGED_FILE", str(tmp_path / "managed"))
    fg.register_managed_file("/a/b/c.png")
    fg.register_managed_file("/d/e/f.png")
    assert fg.load_managed_files() == {"/a/b/c.png", "/d/e/f.png"}


def test_register_managed_appends_duplicates_but_set_dedups(tmp_path, monkeypatch):
    # register_managed_file has no de-dup guard; load returns a set so the result
    # is still de-duplicated even though the file holds two lines.
    managed = tmp_path / "managed"
    monkeypatch.setattr(fg, "MANAGED_FILE", str(managed))
    fg.register_managed_file("/a/b/c.png")
    fg.register_managed_file("/a/b/c.png")
    with open(managed, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert lines == ["/a/b/c.png", "/a/b/c.png"]
    assert fg.load_managed_files() == {"/a/b/c.png"}


# ---------------------------------------------------------------------------
# convert_animated_to_apng + _ApngFrameCounter
# ---------------------------------------------------------------------------

def _make_animated_webp(path, n=6):
    frames = [Image.new("RGBA", (8, 8), (i * 30 % 256, 10, 20, 255)) for i in range(n)]
    frames[0].save(
        str(path), format="WEBP", save_all=True,
        append_images=frames[1:], duration=70, loop=0,
    )
    return n


def test_convert_animated_to_apng_frame_count_and_callbacks(tmp_path):
    src = tmp_path / "anim.webp"
    n = _make_animated_webp(src, n=6)
    out = tmp_path / "out.png"

    progress = []
    status = []

    ok = fg.convert_animated_to_apng(
        str(src), str(out),
        progress_cb=lambda done, total: progress.append((done, total)),
        status_cb=lambda msg: status.append(msg),
    )
    assert ok is True

    im = Image.open(str(out))
    assert im.format == "PNG"
    assert getattr(im, "is_animated", False) is True
    assert im.n_frames == n

    # progress_cb fired exactly once per frame, counting up to total.
    assert progress == [(i + 1, n) for i in range(n)]

    # status_cb received both phase strings, in order.
    assert status == ["Reading frames…", "Converting to APNG…"]


def test_convert_animated_to_apng_none_callbacks_ok(tmp_path):
    # Passing no callbacks (the defaults) must still work.
    src = tmp_path / "anim2.webp"
    _make_animated_webp(src, n=4)
    out = tmp_path / "out2.png"
    assert fg.convert_animated_to_apng(str(src), str(out)) is True
    assert Image.open(str(out)).n_frames == 4


def test_apng_frame_counter_passthrough_and_counts():
    # Unit test the counter wrapper directly: only 8-byte chunks ending in b"fcTL"
    # should increment the per-frame counter.
    written = []

    class FakeFP:
        def write(self, b):
            written.append(bytes(b))
            return len(b)

    calls = []
    counter = fg._ApngFrameCounter(FakeFP(), total=3, progress_cb=lambda d, t: calls.append((d, t)))

    counter.write(b"\x00\x00\x00\x00fcTL")   # 8 bytes ending fcTL -> counts
    counter.write(b"not-eight")               # wrong length -> ignored
    counter.write(b"abcdfcTL")                # 8 bytes ending fcTL -> counts
    counter.write(b"\x00\x00\x00\x00IDAT")   # 8 bytes, not fcTL -> ignored

    assert calls == [(1, 3), (2, 3)]
    # All writes passed through to the underlying file object.
    assert written == [b"\x00\x00\x00\x00fcTL", b"not-eight", b"abcdfcTL", b"\x00\x00\x00\x00IDAT"]


def test_apng_frame_counter_getattr_delegates():
    class FakeFP:
        def write(self, b):
            return len(b)

        def tell(self):
            return 42

    counter = fg._ApngFrameCounter(FakeFP(), total=1, progress_cb=None)
    # Attribute not defined on the wrapper delegates to the wrapped fp.
    assert counter.tell() == 42


# ---------------------------------------------------------------------------
# _sgdb_get  (429 retry logic) — requests.get and time.sleep monkeypatched
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_sgdb_get_returns_immediately_on_200(monkeypatch):
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"] += 1
        return _FakeResp(200)

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    r = fg._sgdb_get("http://x", headers={})
    assert r.status_code == 200
    assert calls["get"] == 1      # no retry
    assert slept == []            # never slept


def test_sgdb_get_retries_once_on_429_then_200(monkeypatch):
    responses = [_FakeResp(429), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    r = fg._sgdb_get("http://x", headers={})
    assert r.status_code == 200
    assert calls["get"] == 2          # initial + one retry
    assert slept == [2]               # first backoff value (no Retry-After header)


def test_sgdb_get_exhausts_retries_returns_last_429(monkeypatch):
    # Always 429: initial call + `retries` retries -> 3 total gets, 2 sleeps.
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"] += 1
        return _FakeResp(429)

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    r = fg._sgdb_get("http://x", headers={}, retries=2)
    assert r.status_code == 429
    assert calls["get"] == 3          # 1 initial + 2 retries
    assert slept == [2, 4]            # the two backoff defaults


def test_sgdb_get_respects_retry_after_header_capped_at_5(monkeypatch):
    # Retry-After of 100 must be capped at 5s.
    responses = [_FakeResp(429, headers={"Retry-After": "100"}), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    r = fg._sgdb_get("http://x", headers={})
    assert r.status_code == 200
    assert slept == [5]               # capped


def test_sgdb_get_retry_after_under_cap_used_directly(monkeypatch):
    responses = [_FakeResp(429, headers={"Retry-After": "3"}), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    fg._sgdb_get("http://x", headers={})
    assert slept == [3]               # used directly, below the 5s cap


def test_sgdb_get_malformed_retry_after_falls_back_to_backoff(monkeypatch):
    # Non-integer Retry-After -> ValueError caught -> falls back to backoffs[attempt].
    responses = [_FakeResp(429, headers={"Retry-After": "soon"}), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    slept = []
    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    fg._sgdb_get("http://x", headers={})
    assert slept == [2]               # fell back to first backoff


# ---------------------------------------------------------------------------
# parse_net_workarea
# ---------------------------------------------------------------------------

def test_parse_net_workarea_single_desktop():
    # Typical single-desktop output: four numbers after the "=".
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053"
    result = fg.parse_net_workarea(line)
    assert result == (0, 0, 1920, 1053)


def test_parse_net_workarea_multi_desktop_takes_first_four():
    # Multi-desktop output: repeated sets of four numbers — only the first set
    # (desktop 0) should be returned.
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053, 0, 0, 1920, 1053"
    result = fg.parse_net_workarea(line)
    assert result == (0, 0, 1920, 1053)


def test_parse_net_workarea_not_found_sentinel():
    # xprop writes this string when the property does not exist.
    line = "_NET_WORKAREA:  not found."
    assert fg.parse_net_workarea(line) is None


def test_parse_net_workarea_empty_string():
    assert fg.parse_net_workarea("") is None


def test_parse_net_workarea_whitespace_only():
    assert fg.parse_net_workarea("   \n  ") is None


def test_parse_net_workarea_too_few_numbers():
    # Only three integers present — not enough for (x, y, w, h).
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920"
    assert fg.parse_net_workarea(line) is None


def test_parse_net_workarea_malformed_no_numbers():
    assert fg.parse_net_workarea("_NET_WORKAREA(CARDINAL) = garbage text only") is None


def test_parse_net_workarea_non_zero_origin():
    # Panel on the left/top pushes the work area's origin off (0, 0).
    line = "_NET_WORKAREA(CARDINAL) = 72, 30, 1848, 1023"
    assert fg.parse_net_workarea(line) == (72, 30, 1848, 1023)


def test_parse_net_workarea_none_input():
    # Callers may pass None when subprocess returned nothing.
    assert fg.parse_net_workarea(None) is None


# ---------------------------------------------------------------------------
# compute_window_fit
# ---------------------------------------------------------------------------

def _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y):
    """Return True if the rectangle (x, y, w, h) lies wholly inside the work area."""
    return (x >= wa_x and y >= wa_y
            and x + w <= wa_x + wa_w
            and y + h <= wa_y + wa_h)


def test_compute_window_fit_smaller_than_workarea_stays_desired():
    # A 900x750 window on a 1920x1080 work area should not be shrunk.
    w, h, x, y = fg.compute_window_fit(0, 0, 1920, 1080, 900, 750, reserve=80)
    assert w == 900
    assert h == 750


def test_compute_window_fit_smaller_than_workarea_centered():
    # On a 1920x1080 screen the window should be horizontally centred.
    w, h, x, y = fg.compute_window_fit(0, 0, 1920, 1080, 900, 750, reserve=80)
    expected_x = (1920 - 900) // 2   # 510
    assert x == expected_x


def test_compute_window_fit_smaller_than_workarea_fully_inside():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1920, 1080
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y)


def test_compute_window_fit_height_caps_to_avail_h():
    # Work area is 1280x800; with reserve=80, avail_h = 720.
    # A 900x750 desired window should be capped vertically.
    w, h, x, y = fg.compute_window_fit(0, 0, 1280, 800, 900, 750, reserve=80)
    assert h == 720   # capped to avail_h (800 - 80)
    assert w == 900   # width unchanged — 900 < 1280


def test_compute_window_fit_small_screen_fully_inside_1920x1080():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1920, 1080
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) is outside work area (1920x1080)")


def test_compute_window_fit_small_screen_fully_inside_1280x800():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1280, 800
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) is outside work area (1280x800)")


def test_compute_window_fit_bottom_not_past_workarea_bottom_1920x1080():
    # The bottom-buttons-clear-of-taskbar invariant: y + h <= wa_h.
    wa_h = 1080
    w, h, x, y = fg.compute_window_fit(0, 0, 1920, wa_h, 900, 750, reserve=80)
    assert y + h <= wa_h, f"Bottom edge {y + h} exceeds work area bottom {wa_h}"


def test_compute_window_fit_bottom_not_past_workarea_bottom_1280x800():
    wa_h = 800
    w, h, x, y = fg.compute_window_fit(0, 0, 1280, wa_h, 900, 750, reserve=80)
    assert y + h <= wa_h, f"Bottom edge {y + h} exceeds work area bottom {wa_h}"


def test_compute_window_fit_non_zero_origin_fully_inside():
    # Simulate a panel on the left (wa_x=72) and top (wa_y=30).
    wa_x, wa_y, wa_w, wa_h = 72, 30, 1848, 1023
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) outside offset work area")
    # Must not be left of the panel.
    assert x >= wa_x
    assert y >= wa_y


def test_compute_window_fit_non_zero_origin_x_not_less_than_wa_x():
    # Even if centering math would place us left of wa_x, the clamp must fix it.
    wa_x, wa_y, wa_w, wa_h = 500, 0, 200, 1080
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert x >= wa_x


def test_compute_window_fit_desired_larger_than_width_caps_width():
    # 4000-wide window on a 1920-wide work area must be clamped.
    w, h, x, y = fg.compute_window_fit(0, 0, 1920, 1080, 4000, 750, reserve=80)
    assert w == 1920


def test_compute_window_fit_very_small_workarea_height_minimum():
    # avail_h is max(wa_h - reserve, 200); even a tiny screen must give at least 200.
    w, h, x, y = fg.compute_window_fit(0, 0, 400, 300, 400, 300, reserve=80)
    # avail_h = max(300 - 80, 200) = 220; h = min(300, 220) = 220
    assert h == 220
    assert w >= 1 and h >= 1


def test_compute_window_fit_degenerate_screen_never_exceeds_workarea():
    # When the work area is smaller than the 200px reserve-floor, the window must
    # still not exceed (and overflow) the work area — the bottom-edge invariant.
    wa_x, wa_y, wa_w, wa_h = 0, 0, 300, 150
    w, h, x, y = fg.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert h <= wa_h and w <= wa_w
    assert y + h <= wa_y + wa_h
    assert x + w <= wa_x + wa_w


def test_parse_net_workarea_negative_origin_reported_honestly():
    # A negative origin must be parsed as-is (so the caller's sanity check can
    # reject it) rather than having the minus stripped into a wrong positive.
    line = "_NET_WORKAREA(CARDINAL) = -10, 0, 1920, 1053"
    assert fg.parse_net_workarea(line) == (-10, 0, 1920, 1053)


# ---------------------------------------------------------------------------
# clear_slot_files / backup_artwork — shared listdir snapshot (existing_files)
# ---------------------------------------------------------------------------

def test_clear_slot_files_uses_provided_snapshot(tmp_path):
    # When existing_files is provided, it's used verbatim instead of listdir — so a
    # name in the snapshot that no longer exists on disk is simply skipped (no error),
    # and on-disk files NOT in the snapshot are left untouched.
    _touch(tmp_path / "123p.png")
    _touch(tmp_path / "123p.jpg")  # on disk but intentionally NOT in the snapshot
    snapshot = ["123p.png", "ghost123p.removed"]  # ghost entry must not raise

    fg.clear_slot_files(str(tmp_path / "123p.webp"), existing_files=snapshot)

    assert not (tmp_path / "123p.png").exists()  # in snapshot + matches prefix -> removed
    assert (tmp_path / "123p.jpg").exists()      # not in snapshot -> untouched


def test_backup_artwork_uses_provided_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "GRID_FOLDER", str(tmp_path / "grid"))
    monkeypatch.setattr(fg, "BACKUP_FOLDER", str(tmp_path / "backup"))
    monkeypatch.setattr(fg, "MANAGED_FILE", str(tmp_path / "managed"))
    os.makedirs(fg.GRID_FOLDER)
    art = os.path.join(fg.GRID_FOLDER, "123p.png")
    _touch(art)
    fg.register_managed_file(art)  # only managed files get backed up

    # Pass the snapshot explicitly; backup must not re-listdir GRID_FOLDER.
    fg.backup_artwork(123, existing_files=["123p.png"])
    assert os.path.exists(os.path.join(fg.BACKUP_FOLDER, "123p.png"))


# ---------------------------------------------------------------------------
# set_shortcut_icons (batch) / set_shortcut_icon (single, delegates) — atomic vdf
# ---------------------------------------------------------------------------

def _write_shortcuts(path, shortcuts):
    """Helper: serialize {key: {appid, ...}} shortcuts into a binary vdf at `path`."""
    import vdf
    with open(path, "wb") as f:
        f.write(vdf.binary_dumps({"shortcuts": shortcuts}))


def _read_shortcuts(path):
    import vdf
    with open(path, "rb") as f:
        return vdf.binary_loads(f.read())["shortcuts"]


def _setup_shortcuts(tmp_path, monkeypatch, shortcuts):
    sc = tmp_path / "shortcuts.vdf"
    _write_shortcuts(str(sc), shortcuts)
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(fg, "BACKUP_FOLDER", str(tmp_path / "backup"))
    return sc


def test_set_shortcut_icons_batch_sets_all_matching_appids(tmp_path, monkeypatch):
    # Two shortcuts share appid 100 (both must get the icon); 200 gets its own; 300
    # is not in the mapping and must be left unchanged.
    sc = _setup_shortcuts(tmp_path, monkeypatch, {
        "0": {"appid": 100, "icon": ""},
        "1": {"appid": 100, "icon": ""},  # duplicate appid
        "2": {"appid": 200, "icon": ""},
        "3": {"appid": 300, "icon": "keep"},
    })
    changed = fg.set_shortcut_icons({100: "/a/icon100.png", 200: "/a/icon200.png"})
    assert changed == 3  # both 100s + the single 200

    out = _read_shortcuts(str(sc))
    assert out["0"]["icon"] == "/a/icon100.png"
    assert out["1"]["icon"] == "/a/icon100.png"
    assert out["2"]["icon"] == "/a/icon200.png"
    assert out["3"]["icon"] == "keep"  # untouched


def test_set_shortcut_icons_appid_masked_to_unsigned(tmp_path, monkeypatch):
    # appid is stored as a signed 32-bit int in the vdf; the mapping key is the
    # unsigned id, so matching must apply (appid & 0xFFFFFFFF).
    signed = -1  # 0xFFFFFFFF unsigned
    uid = signed & 0xFFFFFFFF
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": signed, "icon": ""}})
    assert fg.set_shortcut_icons({uid: "/a/i.png"}) == 1
    assert _read_shortcuts(str(sc))["0"]["icon"] == "/a/i.png"


def test_set_shortcut_icons_backs_up_once(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    fg.set_shortcut_icons({100: "/a/i.png"})
    bak = os.path.join(fg.BACKUP_FOLDER, "shortcuts.vdf.bak")
    assert os.path.exists(bak)
    import shutil
    before = open(bak, "rb").read()
    # A second write must not overwrite the original backup.
    fg.set_shortcut_icons({100: "/a/i2.png"})
    assert open(bak, "rb").read() == before


def test_set_shortcut_icons_empty_mapping_returns_zero(tmp_path, monkeypatch):
    _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    assert fg.set_shortcut_icons({}) == 0


def test_set_shortcut_icons_missing_file_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(tmp_path / "nope.vdf"))
    assert fg.set_shortcut_icons({100: "/a/i.png"}) == 0


def test_set_shortcut_icon_single_delegates_and_returns_bool(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    assert fg.set_shortcut_icon(100, "/a/i.png") is True
    assert _read_shortcuts(str(sc))["0"]["icon"] == "/a/i.png"
    # No matching appid -> bool False.
    assert fg.set_shortcut_icon(999, "/a/i.png") is False


def test_write_shortcuts_atomic_no_temp_left(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    fg.set_shortcut_icons({100: "/a/i.png"})
    # The temp file used for the atomic os.replace must not survive the write.
    assert not os.path.exists(os.path.join(str(tmp_path), "shortcuts.vdf.tmp"))


# ---------------------------------------------------------------------------
# stop_steam / start_steam / restart_steam — sequencing + factoring
# ---------------------------------------------------------------------------

def test_restart_steam_calls_stop_sleep_start_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(fg, "stop_steam", lambda: calls.append("stop"))
    monkeypatch.setattr(fg, "start_steam", lambda: calls.append("start"))
    monkeypatch.setattr(fg.time, "sleep", lambda *_: calls.append("sleep"))
    fg.restart_steam()
    assert calls == ["stop", "sleep", "start"]


def test_is_steam_running_force_bypasses_stale_true_cache(monkeypatch):
    # A recent cache says Steam is running (True) but psutil now reports no steam
    # process. Plain is_steam_running() must trust the fresh cache (stale True),
    # while force=True bypasses it, rescans, returns False, AND refreshes the cache.
    monkeypatch.setattr(fg, "PSUTIL_AVAILABLE", True)
    monkeypatch.setattr(fg, "_steam_cache", (fg.time.monotonic(), True))
    # psutil sees no processes named "steam".
    monkeypatch.setattr(fg.psutil, "process_iter", lambda *_a, **_k: iter([]))

    assert fg.is_steam_running() is True            # stale cache trusted
    assert fg.is_steam_running(force=True) is False  # cache bypassed → real scan
    # The forced scan refreshed the shared cache, so the next plain call sees False.
    assert fg.is_steam_running() is False


# ---------------------------------------------------------------------------
# _version_tuple
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("1.2.3",   (1, 2, 3)),
    ("v1.2.5",  (1, 2, 5)),
    ("2.0",     (2, 0)),
    ("1.2.3-beta", (1, 2, 3)),   # non-numeric suffix stripped per part
    ("0.0.1",   (0, 0, 1)),
    ("10.0.0",  (10, 0, 0)),
])
def test_version_tuple_parses(s, expected):
    assert fg._version_tuple(s) == expected


def test_version_tuple_newer_beats_older():
    assert fg._version_tuple("1.2.6") > fg._version_tuple("1.2.5")


def test_version_tuple_major_beats_minor():
    assert fg._version_tuple("2.0.0") > fg._version_tuple("1.99.99")


def test_version_tuple_equal_versions():
    assert fg._version_tuple("1.2.5") == fg._version_tuple("v1.2.5")


# ---------------------------------------------------------------------------
# check_for_update  (requests.get monkeypatched — no real network)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _fake_release(tag, assets=None, html_url="https://github.com/example/release"):
    return {
        "tag_name": tag,
        "html_url": html_url,
        "assets": assets or [],
    }


def test_check_for_update_available(monkeypatch):
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0")))
    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is True
    assert result["latest"] == "1.3.0"
    assert result["current"] == "1.2.5"


def test_check_for_update_up_to_date(monkeypatch):
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.2.5")))
    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False


def test_check_for_update_older_release_not_available(monkeypatch):
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.0.0")))
    result = fg.check_for_update("1.2.5")
    assert result["available"] is False


def test_check_for_update_network_error_returns_error_dict(monkeypatch):
    slept = []
    def boom(*a, **kw):
        raise fg.requests.exceptions.RequestException("no network")
    monkeypatch.setattr(fg.requests, "get", boom)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))
    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False
    assert result["error"] is not None
    assert "network" in result["error"].lower()
    assert slept == [1]  # retried once


def test_check_for_update_picks_linux_asset(monkeypatch):
    assets = [
        {"name": "NonSteamScraper-linux", "browser_download_url": "https://dl/linux"},
        {"name": "NonSteamScraper-win.zip", "browser_download_url": "https://dl/win.zip"},
    ]
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(fg.platform, "system", lambda: "Linux")
    result = fg.check_for_update("1.2.5")
    assert result["url"] == "https://dl/linux"


def test_check_for_update_picks_windows_asset(monkeypatch):
    assets = [
        {"name": "NonSteamScraper-linux", "browser_download_url": "https://dl/linux"},
        {"name": "NonSteamScraper-win.zip", "browser_download_url": "https://dl/win.zip"},
    ]
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(fg.platform, "system", lambda: "Windows")
    result = fg.check_for_update("1.2.5")
    assert result["url"] == "https://dl/win.zip"


def test_check_for_update_falls_back_to_html_url_when_no_matching_asset(monkeypatch):
    assets = [{"name": "checksums.txt", "browser_download_url": "https://dl/chk"}]
    release_page = "https://github.com/example/releases/tag/v1.3.0"
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(
            _fake_release("v1.3.0", assets=assets, html_url=release_page)))
    monkeypatch.setattr(fg.platform, "system", lambda: "Linux")
    result = fg.check_for_update("1.2.5")
    # "checksums.txt" has an extension ("txt") so bare-binary fallback doesn't
    # match; the function should fall back to html_url.
    assert result["url"] == release_page


def test_check_for_update_linux_bare_binary_fallback(monkeypatch):
    # A file with no extension is the bare binary fallback for Linux.
    assets = [
        {"name": "NonSteamScraper", "browser_download_url": "https://dl/bin"},
        {"name": "checksums.txt", "browser_download_url": "https://dl/chk"},
    ]
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(fg.platform, "system", lambda: "Linux")
    result = fg.check_for_update("1.2.5")
    assert result["url"] == "https://dl/bin"


def test_check_for_update_empty_tag_returns_error_dict(monkeypatch):
    monkeypatch.setattr(fg.requests, "get",
        lambda *a, **kw: _FakeResponse({"tag_name": "", "html_url": "x", "assets": []}))
    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False
    assert result["error"] is not None


def test_check_for_update_retry_on_request_exception_then_success(monkeypatch):
    """First attempt raises RequestException; second attempt succeeds — retry works."""
    calls = {"n": 0}
    slept = []

    def fake_get(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fg.requests.exceptions.RequestException("blip")
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is True
    assert result["latest"] == "1.3.0"
    assert calls["n"] == 2      # initial failure + one retry
    assert slept == [1]         # 1 s inter-retry pause


def test_check_for_update_exhausts_retries_returns_error_dict(monkeypatch):
    """Both attempts raise RequestException — returns error dict (network reason)."""
    slept = []

    def fake_get(*a, **kw):
        raise fg.requests.exceptions.RequestException("always down")

    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False
    assert result["error"] is not None
    assert "network" in result["error"].lower()
    assert slept == [1]     # slept once between the two attempts


def test_check_for_update_non200_returns_error_immediately(monkeypatch):
    """Non-200 (non-403) → error dict returned immediately; NOT retried."""
    calls = {"n": 0}
    slept = []

    class _Non200:
        status_code = 500

    def fake_get(*a, **kw):
        calls["n"] += 1
        return _Non200()

    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False
    assert "500" in result["error"]
    assert calls["n"] == 1   # called exactly once — no retry
    assert slept == []        # no sleep because no retry


def test_check_for_update_403_returns_rate_limit_error_no_retry(monkeypatch):
    """403 → rate-limit error dict; NOT retried (conserves the 60/hr budget)."""
    calls = {"n": 0}
    slept = []

    class _Forbidden:
        status_code = 403

    def fake_get(*a, **kw):
        calls["n"] += 1
        return _Forbidden()

    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    result = fg.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False
    assert "rate limit" in result["error"].lower()
    assert calls["n"] == 1   # called exactly once — no retry
    assert slept == []        # no sleep because no retry


def test_check_for_update_403_reports_minutes_until_reset(monkeypatch):
    """A rate-limited 403 with X-RateLimit-Reset reports the minutes remaining."""
    import re
    import time as _time
    calls = {"n": 0}
    slept = []

    class _Forbidden:
        status_code = 403
        # ~30 minutes from now (1799s rounds up to 30 via ceil)
        headers = {"X-RateLimit-Reset": str(int(_time.time()) + 1799)}

    def fake_get(*a, **kw):
        calls["n"] += 1
        return _Forbidden()

    monkeypatch.setattr(fg.requests, "get", fake_get)
    monkeypatch.setattr(fg.time, "sleep", lambda s: slept.append(s))

    result = fg.check_for_update("1.2.5")
    assert "rate limit" in result["error"].lower()
    assert "try again in" in result["error"].lower()
    # Parse the "~N min" figure out of the message and check it's ~30.
    mins = int(re.search(r"~(\d+)\s*min", result["error"]).group(1))
    assert 29 <= mins <= 31
    assert calls["n"] == 1   # not retried
    assert slept == []


def test_check_for_update_403_without_reset_header_is_generic(monkeypatch):
    """A 403 with no rate-limit headers falls back to the generic message."""
    class _Forbidden:
        status_code = 403
        headers = {}

    monkeypatch.setattr(fg.requests, "get", lambda *a, **kw: _Forbidden())
    result = fg.check_for_update("1.2.5")
    assert "rate limit" in result["error"].lower()
    assert "try again later" in result["error"].lower()


def test_check_for_update_sends_user_agent_header(monkeypatch):
    """Verify the User-Agent header is present in every request."""
    seen_headers = []

    def fake_get(url, headers=None, **kw):
        seen_headers.append(headers or {})
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(fg.requests, "get", fake_get)
    fg.check_for_update("1.2.5")
    assert seen_headers, "requests.get was never called"
    assert "User-Agent" in seen_headers[0]
    assert seen_headers[0]["User-Agent"]   # non-empty


def test_check_for_update_uses_10s_timeout(monkeypatch):
    """Verify timeout=10 is passed to requests.get."""
    seen_timeouts = []

    def fake_get(url, timeout=None, **kw):
        seen_timeouts.append(timeout)
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(fg.requests, "get", fake_get)
    fg.check_for_update("1.2.5")
    assert seen_timeouts == [10]


# ---------------------------------------------------------------------------
# Consuming download_all_artwork results — applied_paths_from_results /
# icon_write_from_results.
#
# REGRESSION GUARD: results mixes art-slot dicts with the icon_to_set TUPLE.
# The old consumer did `for v in results.values(): v.get(...)`, which raised
# AttributeError ('tuple' object has no attribute 'get') the moment a game had
# an icon — and because run_fetch ran in a daemon thread with no try/except,
# the UI hung on "Fetching…" forever. These tests pin the helper contracts AND
# drive the real defer_icon=True assembly to prove the consumer survives it.
# ---------------------------------------------------------------------------

def _slot(applied_index, applied_path):
    """Minimal art-slot dict shaped like download_all_artwork emits."""
    return {
        "applied_url": None, "applied_path": applied_path,
        "applied_index": applied_index, "option_urls": [],
        "option_meta": [], "thumb_paths": [], "current_index": 0,
        "filename_base": "x",
    }


def test_applied_paths_from_results_collects_only_applied_slots():
    results = {
        "grids":      _slot(0, "/g/123p.png"),       # applied
        "grids_wide": _slot(None, "/g/123.png"),     # not applied (animated)
        "heroes":     None,                          # missing slot
        "logos":      _slot(2, "/g/123_logo.png"),   # applied
        "icons":      _slot(0, "/g/123_icon.png"),   # applied
        "icon_to_set": (12345, "/g/12345_icon.png"), # the TUPLE hazard
    }
    paths = fg.applied_paths_from_results(results)
    assert paths == ["/g/123p.png", "/g/123_logo.png", "/g/123_icon.png"]


def test_applied_paths_from_results_does_not_raise_on_icon_tuple():
    # The exact shape that crashed the old inline loop: an icon_to_set tuple
    # alongside slots. Must NOT raise AttributeError.
    results = {
        "grids": _slot(0, "/g/p.png"),
        "icon_to_set": (999, "/g/999_icon.png"),
    }
    assert fg.applied_paths_from_results(results) == ["/g/p.png"]


def test_applied_paths_from_results_skips_applied_index_none_and_no_path():
    results = {
        "a": _slot(None, "/g/a.png"),  # index None -> skipped
        "b": _slot(0, ""),             # no path    -> skipped
        "icon_to_set": None,
    }
    assert fg.applied_paths_from_results(results) == []


def test_icon_write_from_results_returns_tuple():
    results = {"grids": _slot(0, "/g/p.png"),
               "icon_to_set": (12345, "/g/12345_icon.png")}
    assert fg.icon_write_from_results(results) == (12345, "/g/12345_icon.png")


def test_icon_write_from_results_none_when_no_icon():
    assert fg.icon_write_from_results({"grids": _slot(0, "/g/p.png"),
                                       "icon_to_set": None}) is None


def test_download_all_artwork_defer_icon_shape_consumable(tmp_path, monkeypatch):
    """Drive the REAL defer_icon=True assembly with network mocked at the lowest
    seam (get_artwork / download_artwork), then feed its return into the consumer
    helpers. This is the regression test that would have caught the hang: a game
    with an icon yields results['icon_to_set'] as a tuple, and the consumer must
    not choke on it."""
    monkeypatch.setattr(fg, "GRID_FOLDER", str(tmp_path / "grid"))
    monkeypatch.setattr(fg, "CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(fg, "BACKUP_FOLDER", str(tmp_path / "backup"))
    os.makedirs(fg.GRID_FOLDER, exist_ok=True)

    # Every slot returns one static PNG option so each slot auto-applies (incl.
    # icons -> exercises the deferred icon_to_set tuple path).
    def fake_get_artwork(sgdb_id, art_type, prefs=None, dimensions=None):
        return [{"url": f"https://sgdb/{art_type}.png", "mime": "image/png"}]

    written = []

    def fake_download_artwork(url, save_path, register=True):
        # Pretend the download succeeded; record the target so we can assert paths.
        with open(save_path, "wb") as f:
            f.write(b"x")
        written.append(save_path)
        return True

    monkeypatch.setattr(fg, "get_artwork", fake_get_artwork)
    monkeypatch.setattr(fg, "download_artwork", fake_download_artwork)
    # set_shortcut_icon must NOT be called in defer mode; fail loudly if it is.
    monkeypatch.setattr(fg, "set_shortcut_icon",
                        lambda *a, **k: pytest.fail("icon write not deferred"))

    results = fg.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    # 1) icon_to_set is present and is the deferred TUPLE, not a slot dict.
    assert "icon_to_set" in results
    assert results["icon_to_set"][0] == 12345
    assert isinstance(results["icon_to_set"], tuple)

    # 2) Feeding the real return into the consumer helpers must NOT raise.
    paths = fg.applied_paths_from_results(results)         # would AttributeError pre-fix
    assert all(isinstance(p, str) for p in paths)
    # The auto-applied icon path is collected as an applied slot path.
    icon_uid, icon_path = fg.icon_write_from_results(results)
    assert icon_uid == 12345
    assert icon_path in paths

    # 3) Shape sanity: every art-slot key maps to a dict, icon_to_set to a tuple.
    for slot in ("grids", "grids_wide", "heroes", "logos", "icons"):
        assert isinstance(results[slot], dict)


# ---------------------------------------------------------------------------
# Pending (deferred) icon writes — defer & auto-apply
# ---------------------------------------------------------------------------

def test_save_pending_icons_merges_with_existing(tmp_path, monkeypatch):
    pf = tmp_path / "pending"
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(pf))
    fg.save_pending_icons({100: "/a/i100.png"})
    fg.save_pending_icons({200: "/a/i200.png", 100: "/a/i100b.png"})  # 100 overwritten
    loaded = fg.load_pending_icons()
    assert loaded == {100: "/a/i100b.png", 200: "/a/i200.png"}


def test_load_pending_icons_round_trips_int_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    fg.save_pending_icons({12345: "/a/i.png"})
    loaded = fg.load_pending_icons()
    assert loaded == {12345: "/a/i.png"}
    assert all(isinstance(k, int) for k in loaded)  # keys back to int, not str


def test_load_pending_icons_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "nope"))
    assert fg.load_pending_icons() == {}


def test_has_and_clear_pending_icons_reflect_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    assert fg.has_pending_icons() is False
    fg.save_pending_icons({100: "/a/i.png"})
    assert fg.has_pending_icons() is True
    fg.clear_pending_icons()
    assert fg.has_pending_icons() is False
    fg.clear_pending_icons()  # idempotent — must not raise when already gone


def test_apply_pending_icons_skips_and_keeps_file_when_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(tmp_path / "shortcuts.vdf"))
    monkeypatch.setattr(fg, "is_steam_running", lambda: True)
    called = []
    monkeypatch.setattr(fg, "set_shortcut_icons", lambda m: called.append(m) or len(m))
    fg.save_pending_icons({100: "/a/i.png"})
    assert fg.apply_pending_icons() == 0
    assert called == []                    # never wrote while Steam open
    assert fg.has_pending_icons() is True  # pending file left intact for next time


def test_apply_pending_icons_writes_and_clears_when_steam_closed(tmp_path, monkeypatch):
    icon = tmp_path / "i.png"
    _touch(icon)  # path must exist so it isn't filtered out
    sc = tmp_path / "shortcuts.vdf"
    sc.write_bytes(b"x")  # SHORTCUTS_PATH just needs to exist for the guard
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(fg, "is_steam_running", lambda: False)
    captured = {}
    monkeypatch.setattr(fg, "set_shortcut_icons", lambda m: captured.update(m) or len(m))
    fg.save_pending_icons({100: str(icon)})
    assert fg.apply_pending_icons() == 1
    assert captured == {100: str(icon)}    # right mapping passed through
    assert fg.has_pending_icons() is False  # cleared after a real write attempt


def test_apply_pending_icons_drops_missing_icon_paths(tmp_path, monkeypatch):
    sc = tmp_path / "shortcuts.vdf"; sc.write_bytes(b"x")
    monkeypatch.setattr(fg, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(fg, "is_steam_running", lambda: False)
    captured = []
    monkeypatch.setattr(fg, "set_shortcut_icons", lambda m: captured.append(m) or len(m))
    fg.save_pending_icons({100: str(tmp_path / "gone.png")})  # path does not exist
    # Nothing valid to write -> set_shortcut_icons not called, but pending still cleared
    # (those icon files are gone; nothing to retry).
    assert fg.apply_pending_icons() == 0
    assert captured == []
    assert fg.has_pending_icons() is False
