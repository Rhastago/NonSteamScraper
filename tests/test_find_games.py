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
