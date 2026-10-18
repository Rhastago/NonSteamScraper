"""Pytest suite for the pure / isolatable backend logic: appfiles, steam_env,
sgdb, steam_files, restore, artwork, winplace and updates (all of it lived in
find_games.py until T-A5 split it; the file keeps its name).

SAFETY NOTES
------------
Importing ``appfiles`` runs module-level code that binds path constants
(SKIP_FILE, NAMES_FILE, PREFS_FILE, MANAGED_FILE, GRID_FOLDER, CACHE_FOLDER,
BACKUP_FOLDER) to *real* paths under the user's home and may even create the
Steam grid folder. This machine has Steam installed, so those constants point
at the user's real data.

To guarantee we never touch real user files, tests/conftest.py sets HOME to a
throw-away temp directory *before* this module imports the backend, so every
``os.path.expanduser("~/...")`` constant is rebased into that temp dir at import
time; conftest removes it when the session ends, and its G-HOME guard fails any
test that writes into it. On top of that, every test that exercises a function reading one
of the module-level path constants ALSO monkeypatches that specific constant
onto a pytest ``tmp_path`` so each test is fully isolated and deterministic.

No test makes a real network call. The only network-touching function tested is
``_sgdb_get``, whose ``requests.get`` and ``time.sleep`` are monkeypatched.
"""

import os
import sys
import json
import platform
import time
import types

import pytest
import requests

# --- The backend is imported under the sandbox HOME that conftest.py creates
# --- (and removes), so module-level path constants never resolve to the real
# --- user's ~/.steamart_* files. Refuse to run if that ever stops being true.
assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before the backend is imported")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updates
import winplace
import artwork
import restore
import steam_files
import sgdb
import steam_env
import appfiles

from PIL import Image  # noqa: E402

# find_games was split into modules (T-A5). A patch must land on the module
# that OWNS a name, or the code that reads it never sees the patch (and a path
# patch then falls back to the sandbox HOME, which G-HOME fails). Helpers that
# patch a name held in a variable go through _owner().
_OWNER_MODULES = (appfiles, steam_env, sgdb, steam_files, restore, artwork, winplace, updates)


def _owner(name):
    for mod in _OWNER_MODULES:
        if name in vars(mod):
            return mod
    raise AssertionError(f"no module owns {name!r}")


# ---------------------------------------------------------------------------
# _release_year
# ---------------------------------------------------------------------------

def test_release_year_valid_timestamp():
    # 2021-01-01 00:00:00 UTC
    assert sgdb._release_year(1609459200) == "2021"


def test_release_year_valid_string_timestamp():
    # The function int()s its input, so a numeric string works too.
    assert sgdb._release_year("1609459200") == "2021"


@pytest.mark.parametrize("falsy", [0, None, ""])
def test_release_year_falsy_returns_empty(falsy):
    assert sgdb._release_year(falsy) == ""


def test_release_year_non_numeric_returns_empty():
    assert sgdb._release_year("not-a-number") == ""


# ---------------------------------------------------------------------------
# _pref_params
# ---------------------------------------------------------------------------

def test_pref_params_animated_never_static():
    assert sgdb._pref_params({"animated": "never"})["types"] == "static"


def test_pref_params_animated_must_have_animated():
    assert sgdb._pref_params({"animated": "must_have"})["types"] == "animated"


def test_pref_params_animated_ok_no_types_key():
    assert "types" not in sgdb._pref_params({"animated": "ok"})


def test_pref_params_animated_absent_no_types_key():
    # Default for animated is "never" -> the code DOES add types=static when absent.
    # Verify the documented mapping: absent -> default "never" -> types=static.
    assert sgdb._pref_params({})["types"] == "static"


@pytest.mark.parametrize(
    "value,expected",
    [("never", "false"), ("must_have", "true"), ("ok", "any"), ("anything_else", "any")],
)
def test_pref_params_nsfw_mapping(value, expected):
    assert sgdb._pref_params({"nsfw": value})["nsfw"] == expected


@pytest.mark.parametrize(
    "value,expected",
    [("never", "false"), ("must_have", "true"), ("ok", "any"), ("whatever", "any")],
)
def test_pref_params_humor_mapping(value, expected):
    assert sgdb._pref_params({"humor": value})["humor"] == expected


def test_pref_params_nsfw_humor_absent_default_any():
    # Default for nsfw/humor in this helper is "ok" -> "any".
    p = sgdb._pref_params({})
    assert p["nsfw"] == "any"
    assert p["humor"] == "any"


# ---------------------------------------------------------------------------
# is_animated_image
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mime", ["image/webp", "image/gif"])
def test_is_animated_image_true(mime):
    assert artwork.is_animated_image({"mime": mime}) is True


@pytest.mark.parametrize("img", [{"mime": "image/png"}, {"mime": "image/jpeg"}, {}])
def test_is_animated_image_false(img):
    assert artwork.is_animated_image(img) is False


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
    got = sgdb._get_styles("grids", prefs, "must_have")
    # _STYLE_MAP maps grid_no_logo->no_logo, grid_alternate->alternate
    assert set(got) == {"no_logo", "alternate"}


def test_get_styles_value_never():
    prefs = {"grid_blurred": "never", "grid_material": "never", "grid_no_logo": "ok"}
    assert set(sgdb._get_styles("grids", prefs, "never")) == {"blurred", "material"}


def test_get_styles_unknown_art_type_returns_empty():
    assert sgdb._get_styles("nonexistent", {"anything": "must_have"}, "must_have") == []


def test_get_styles_heroes_mapping():
    prefs = {"hero_alternate": "must_have", "hero_blurred": "ok"}
    assert sgdb._get_styles("heroes", prefs, "must_have") == ["alternateArt"]


def test_strip_never_filters_never_styles():
    prefs = {"grid_blurred": "never"}  # blurred is a "never" style
    results = [
        {"style": "blurred", "url": "a"},
        {"style": "alternate", "url": "b"},
        {"style": "material", "url": "c"},
    ]
    filtered = sgdb._strip_never(results, "grids", prefs)
    assert [r["url"] for r in filtered] == ["b", "c"]


def test_strip_never_no_never_returns_input_unchanged():
    prefs = {"grid_blurred": "ok", "grid_material": "ok"}
    results = [{"style": "blurred", "url": "a"}, {"style": "material", "url": "b"}]
    out = sgdb._strip_never(results, "grids", prefs)
    # When there are no "never" styles the function returns the original list object.
    assert out is results


def test_strip_never_keeps_records_with_unknown_style():
    prefs = {"grid_blurred": "never"}
    results = [{"style": "weird_unmapped", "url": "x"}, {"style": "blurred", "url": "y"}]
    filtered = sgdb._strip_never(results, "grids", prefs)
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

    artwork.clear_slot_files(str(tmp_path / "123p.webp"))

    assert not (tmp_path / "123p.png").exists()       # removed: same slot, diff ext
    assert (tmp_path / "123.png").exists()             # kept: wide slot
    assert (tmp_path / "123_hero.png").exists()        # kept: hero slot
    assert (tmp_path / "999p.png").exists()            # kept: other appid


def test_clear_slot_files_wide_slot_isolated(tmp_path):
    _touch(tmp_path / "123p.png")
    _touch(tmp_path / "123.png")
    _touch(tmp_path / "123_hero.png")
    _touch(tmp_path / "999p.png")

    artwork.clear_slot_files(str(tmp_path / "123.gif"))

    assert not (tmp_path / "123.png").exists()         # removed: wide slot match
    assert (tmp_path / "123p.png").exists()            # kept: portrait slot
    assert (tmp_path / "123_hero.png").exists()        # kept: hero slot
    assert (tmp_path / "999p.png").exists()            # kept: other appid


def test_clear_slot_files_missing_folder_no_error(tmp_path):
    # listdir on a nonexistent folder raises OSError, which the function swallows.
    artwork.clear_slot_files(str(tmp_path / "no_such_dir" / "123p.webp"))  # must not raise


# ---------------------------------------------------------------------------
# row_thumbnail_path  (best row-cover selection by slot)
# ---------------------------------------------------------------------------

def test_row_thumbnail_prefers_portrait_over_wide(tmp_path):
    _touch(tmp_path / "123p.png")   # portrait cover (preferred)
    _touch(tmp_path / "123.jpg")    # wide grid
    _touch(tmp_path / "123_hero.png")
    assert artwork.row_thumbnail_path(str(tmp_path), 123) == str(tmp_path / "123p.png")


def test_row_thumbnail_falls_back_to_wide(tmp_path):
    _touch(tmp_path / "123.webp")   # only the wide slot exists
    _touch(tmp_path / "123_hero.png")
    _touch(tmp_path / "123_logo.png")
    assert artwork.row_thumbnail_path(str(tmp_path), 123) == str(tmp_path / "123.webp")


def test_row_thumbnail_ignores_hero_logo_icon_only(tmp_path):
    # No portrait or wide cover — hero/logo/icon must NOT be chosen.
    _touch(tmp_path / "123_hero.png")
    _touch(tmp_path / "123_logo.png")
    _touch(tmp_path / "123_icon.png")
    assert artwork.row_thumbnail_path(str(tmp_path), 123) is None


def test_row_thumbnail_none_when_no_art(tmp_path):
    _touch(tmp_path / "999p.png")   # different appid
    assert artwork.row_thumbnail_path(str(tmp_path), 123) is None


def test_row_thumbnail_slot_isolation_no_prefix_bleed(tmp_path):
    # "1234.png" must not be mistaken for app_id 123's wide slot.
    _touch(tmp_path / "1234.png")
    _touch(tmp_path / "1230.png")
    assert artwork.row_thumbnail_path(str(tmp_path), 123) is None


def test_row_thumbnail_missing_folder_returns_none(tmp_path):
    assert artwork.row_thumbnail_path(str(tmp_path / "no_such_dir"), 123) is None


# ---------------------------------------------------------------------------
# Skip-list round-trip (with de-dup guard)
# ---------------------------------------------------------------------------

def test_skip_list_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(tmp_path / "skip"))
    assert appfiles.load_skip_list() == set()


def test_skip_list_add_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(tmp_path / "skip"))
    appfiles.add_to_skip_list(111)
    appfiles.add_to_skip_list(222)
    assert appfiles.load_skip_list() == {"111", "222"}


def test_skip_list_dedup_guard(tmp_path, monkeypatch):
    skip = tmp_path / "skip"
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(skip))
    appfiles.add_to_skip_list(111)
    appfiles.add_to_skip_list(111)  # duplicate -> should not append a second line
    with open(skip, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert lines == ["111"]
    assert appfiles.load_skip_list() == {"111"}


# ---------------------------------------------------------------------------
# Name overrides round-trip
# ---------------------------------------------------------------------------

def test_name_overrides_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    assert appfiles.load_name_overrides() == {}


def test_name_overrides_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    appfiles.save_name_override(123, "Real Game Name")
    appfiles.save_name_override(456, "Another Title")
    assert appfiles.load_name_overrides() == {"123": "Real Game Name", "456": "Another Title"}


def test_name_overrides_update_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    appfiles.save_name_override(123, "Old")
    appfiles.save_name_override(123, "New")
    assert appfiles.load_name_overrides() == {"123": "New"}


def test_name_overrides_name_with_pipe_preserved_on_load(tmp_path, monkeypatch):
    # load splits on the FIRST pipe only, so a name containing '|' survives load.
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    with open(tmp_path / "names", "w", encoding="utf-8") as f:
        f.write("789|Game | With Pipe\n")
    assert appfiles.load_name_overrides() == {"789": "Game | With Pipe"}


# ---------------------------------------------------------------------------
# Prefs round-trip
# ---------------------------------------------------------------------------

def test_load_prefs_returns_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PREFS_FILE", str(tmp_path / "prefs"))
    prefs = appfiles.load_prefs()
    assert prefs == appfiles.DEFAULT_PREFS
    # Must be a fresh dict, not the module default object (mutation safety).
    assert prefs is not appfiles.DEFAULT_PREFS


def test_save_and_load_prefs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PREFS_FILE", str(tmp_path / "prefs"))
    custom = dict(appfiles.DEFAULT_PREFS)
    custom["animated"] = "must_have"
    custom["nsfw"] = "ok"
    appfiles.save_prefs(custom)
    assert appfiles.load_prefs() == custom


def test_load_prefs_filters_unknown_keys(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs"
    monkeypatch.setattr(appfiles, "PREFS_FILE", str(prefs_file))
    with open(prefs_file, "w", encoding="utf-8") as f:
        json.dump({"animated": "ok", "bogus_key": "zzz"}, f)
    loaded = appfiles.load_prefs()
    assert "bogus_key" not in loaded
    assert loaded["animated"] == "ok"
    # All default keys still present.
    assert set(loaded) == set(appfiles.DEFAULT_PREFS)


def test_load_prefs_corrupt_json_falls_back_to_defaults(tmp_path, monkeypatch):
    prefs_file = tmp_path / "prefs"
    monkeypatch.setattr(appfiles, "PREFS_FILE", str(prefs_file))
    with open(prefs_file, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ]")
    assert appfiles.load_prefs() == appfiles.DEFAULT_PREFS


# ---------------------------------------------------------------------------
# Managed-file registry round-trip
# ---------------------------------------------------------------------------

def test_load_managed_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(tmp_path / "managed"))
    assert appfiles.load_managed_files() == set()


def test_register_and_load_managed(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(tmp_path / "managed"))
    appfiles.register_managed_file("/a/b/c.png")
    appfiles.register_managed_file("/d/e/f.png")
    assert appfiles.load_managed_files() == {"/a/b/c.png", "/d/e/f.png"}


def test_register_managed_does_not_append_a_path_twice(tmp_path, monkeypatch):
    # B13: re-registering the same path (every re-apply) used to append another
    # line, so the registry only ever grew.
    managed = tmp_path / "managed"
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(managed))
    appfiles.register_managed_file("/a/b/c.png")
    appfiles.register_managed_file("/a/b/c.png")
    with open(managed, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert lines == ["/a/b/c.png"]
    assert appfiles.load_managed_files() == {"/a/b/c.png"}


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

    ok = artwork.convert_animated_to_apng(
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
    assert artwork.convert_animated_to_apng(str(src), str(out)) is True
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
    counter = artwork._ApngFrameCounter(FakeFP(), total=3, progress_cb=lambda d, t: calls.append((d, t)))

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

    counter = artwork._ApngFrameCounter(FakeFP(), total=1, progress_cb=None)
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    r = sgdb._sgdb_get("http://x", headers={})
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    r = sgdb._sgdb_get("http://x", headers={})
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    r = sgdb._sgdb_get("http://x", headers={}, retries=2)
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    r = sgdb._sgdb_get("http://x", headers={})
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    sgdb._sgdb_get("http://x", headers={})
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
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    sgdb._sgdb_get("http://x", headers={})
    assert slept == [2]               # fell back to first backoff


# ---------------------------------------------------------------------------
# check_api_key / verify_api_key / search_game — explicit outcomes (B3, Q2)
# The 401 body below is the one SGDB really returned for a bad key on
# 2026-10-17 (TRIAGE evidence run).
# ---------------------------------------------------------------------------

class _JsonResp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


_SGDB_401 = {"success": False, "errors": ["Invalid key format"]}


def _serve(monkeypatch, *responses):
    """Patch the HTTP seam to hand out `responses` in order (an Exception
    instance is raised instead). Returns the list of URLs requested."""
    queue = list(responses)
    urls = []

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        urls.append(url)
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return urls


def test_check_api_key_401_is_rejected(monkeypatch):
    _serve(monkeypatch, _JsonResp(401, _SGDB_401))
    assert sgdb.check_api_key("bad-key") == sgdb.KEY_REJECTED


def test_check_api_key_403_is_rejected(monkeypatch):
    _serve(monkeypatch, _JsonResp(403, {"success": False}))
    assert sgdb.check_api_key("bad-key") == sgdb.KEY_REJECTED


def test_check_api_key_connection_error_is_unreachable(monkeypatch):
    _serve(monkeypatch, requests.exceptions.ConnectionError("no route"))
    assert sgdb.check_api_key("good-key") == sgdb.KEY_UNREACHABLE


@pytest.mark.parametrize("resp", [
    _JsonResp(500, {"success": False}),
    _JsonResp(429, {"success": False}),
    _JsonResp(200, ValueError("not json")),
])
def test_check_api_key_server_trouble_is_unreachable_not_rejected(monkeypatch, resp):
    """A 5xx, a 429 that outlasts the retries or a garbled body says nothing
    about the key, so it must never be reported as a rejection."""
    _serve(monkeypatch, resp)
    assert sgdb.check_api_key("good-key") == sgdb.KEY_UNREACHABLE


def test_check_api_key_200_is_accepted(monkeypatch):
    _serve(monkeypatch, _JsonResp(200, {"success": True, "data": []}))
    assert sgdb.check_api_key("good-key") == sgdb.KEY_ACCEPTED


def test_check_api_key_defaults_to_the_saved_key(monkeypatch):
    seen = []

    def fake_get(url, headers=None, params=None, timeout=None, stream=False):
        seen.append(headers["Authorization"])
        return _JsonResp(200, {"success": True})
    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(appfiles, "load_api_key", lambda: "SAVED")
    assert sgdb.check_api_key() == sgdb.KEY_ACCEPTED
    assert seen == ["Bearer SAVED"]


def test_verify_api_key_keeps_its_bool_contract(monkeypatch):
    _serve(monkeypatch, _JsonResp(200, {"success": True}))
    assert sgdb.verify_api_key("k") is True
    _serve(monkeypatch, _JsonResp(401, _SGDB_401))
    assert sgdb.verify_api_key("k") is False
    _serve(monkeypatch, requests.exceptions.ConnectionError("x"))
    assert sgdb.verify_api_key("k") is False
    assert sgdb.verify_api_key("   ") is False


def _search_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    monkeypatch.setattr(appfiles, "load_api_key", lambda: "KEY")


def test_search_game_401_is_refused(tmp_path, monkeypatch):
    """B3: a refused key used to come back as None — 'not found' — and the
    fetch hid the game for good."""
    _search_setup(tmp_path, monkeypatch)
    _serve(monkeypatch, _JsonResp(401, _SGDB_401))
    assert sgdb.search_game("Doom Eternal", 1) == (sgdb.SEARCH_REFUSED, None)


def test_search_game_403_is_refused(tmp_path, monkeypatch):
    _search_setup(tmp_path, monkeypatch)
    _serve(monkeypatch, _JsonResp(403, {"success": False}))
    assert sgdb.search_game("Doom", 1) == (sgdb.SEARCH_REFUSED, None)


def test_search_game_empty_data_is_not_found_after_every_query(tmp_path, monkeypatch):
    _search_setup(tmp_path, monkeypatch)
    urls = _serve(monkeypatch, _JsonResp(200, {"success": True, "data": []}))
    assert sgdb.search_game("Some Long Game Title", 1) == (sgdb.SEARCH_NOT_FOUND, None)
    assert len(urls) == 2, "both the full and the 3-word query must come back empty"


def test_search_game_network_error_is_error(tmp_path, monkeypatch):
    _search_setup(tmp_path, monkeypatch)
    _serve(monkeypatch, requests.exceptions.ConnectionError("down"))
    assert sgdb.search_game("Doom", 1) == (sgdb.SEARCH_ERROR, None)


@pytest.mark.parametrize("resp", [
    _JsonResp(200, {"success": False, "errors": ["x"]}),
    _JsonResp(500, {"success": False}),
    _JsonResp(429, {"success": False}),
    _JsonResp(200, ValueError("not json")),
    _JsonResp(200, {"success": True}),
])
def test_search_game_anything_but_genuine_empty_is_error(tmp_path, monkeypatch, resp):
    """Only {"success": true, "data": []} may count as not-found."""
    _search_setup(tmp_path, monkeypatch)
    _serve(monkeypatch, resp)
    assert sgdb.search_game("Doom", 1) == (sgdb.SEARCH_ERROR, None)


def test_search_game_found_returns_first_hit(tmp_path, monkeypatch):
    _search_setup(tmp_path, monkeypatch)
    _serve(monkeypatch, _JsonResp(200, {"success": True, "data": [
        {"id": 77, "name": "Doom"}, {"id": 78, "name": "Doom II"}]}))
    assert sgdb.search_game("Doom", 1) == (sgdb.SEARCH_FOUND, 77)


# ---------------------------------------------------------------------------
# parse_net_workarea
# ---------------------------------------------------------------------------

def test_parse_net_workarea_single_desktop():
    # Typical single-desktop output: four numbers after the "=".
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053"
    result = winplace.parse_net_workarea(line)
    assert result == (0, 0, 1920, 1053)


def test_parse_net_workarea_multi_desktop_takes_first_four():
    # Multi-desktop output: repeated sets of four numbers — only the first set
    # (desktop 0) should be returned.
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053, 0, 0, 1920, 1053"
    result = winplace.parse_net_workarea(line)
    assert result == (0, 0, 1920, 1053)


def test_parse_net_workarea_not_found_sentinel():
    # xprop writes this string when the property does not exist.
    line = "_NET_WORKAREA:  not found."
    assert winplace.parse_net_workarea(line) is None


def test_parse_net_workarea_empty_string():
    assert winplace.parse_net_workarea("") is None


def test_parse_net_workarea_whitespace_only():
    assert winplace.parse_net_workarea("   \n  ") is None


def test_parse_net_workarea_too_few_numbers():
    # Only three integers present — not enough for (x, y, w, h).
    line = "_NET_WORKAREA(CARDINAL) = 0, 0, 1920"
    assert winplace.parse_net_workarea(line) is None


def test_parse_net_workarea_malformed_no_numbers():
    assert winplace.parse_net_workarea("_NET_WORKAREA(CARDINAL) = garbage text only") is None


def test_parse_net_workarea_non_zero_origin():
    # Panel on the left/top pushes the work area's origin off (0, 0).
    line = "_NET_WORKAREA(CARDINAL) = 72, 30, 1848, 1023"
    assert winplace.parse_net_workarea(line) == (72, 30, 1848, 1023)


def test_parse_net_workarea_none_input():
    # Callers may pass None when subprocess returned nothing.
    assert winplace.parse_net_workarea(None) is None


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
    w, h, x, y = winplace.compute_window_fit(0, 0, 1920, 1080, 900, 750, reserve=80)
    assert w == 900
    assert h == 750


def test_compute_window_fit_smaller_than_workarea_centered():
    # On a 1920x1080 screen the window should be horizontally centred.
    w, h, x, y = winplace.compute_window_fit(0, 0, 1920, 1080, 900, 750, reserve=80)
    expected_x = (1920 - 900) // 2   # 510
    assert x == expected_x


def test_compute_window_fit_smaller_than_workarea_fully_inside():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1920, 1080
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y)


def test_compute_window_fit_height_caps_to_avail_h():
    # Work area is 1280x800; with reserve=80, avail_h = 720.
    # A 900x750 desired window should be capped vertically.
    w, h, x, y = winplace.compute_window_fit(0, 0, 1280, 800, 900, 750, reserve=80)
    assert h == 720   # capped to avail_h (800 - 80)
    assert w == 900   # width unchanged — 900 < 1280


def test_compute_window_fit_small_screen_fully_inside_1920x1080():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1920, 1080
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) is outside work area (1920x1080)")


def test_compute_window_fit_small_screen_fully_inside_1280x800():
    wa_x, wa_y, wa_w, wa_h = 0, 0, 1280, 800
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) is outside work area (1280x800)")


def test_compute_window_fit_bottom_not_past_workarea_bottom_1920x1080():
    # The bottom-buttons-clear-of-taskbar invariant: y + h <= wa_h.
    wa_h = 1080
    w, h, x, y = winplace.compute_window_fit(0, 0, 1920, wa_h, 900, 750, reserve=80)
    assert y + h <= wa_h, f"Bottom edge {y + h} exceeds work area bottom {wa_h}"


def test_compute_window_fit_bottom_not_past_workarea_bottom_1280x800():
    wa_h = 800
    w, h, x, y = winplace.compute_window_fit(0, 0, 1280, wa_h, 900, 750, reserve=80)
    assert y + h <= wa_h, f"Bottom edge {y + h} exceeds work area bottom {wa_h}"


def test_compute_window_fit_non_zero_origin_fully_inside():
    # Simulate a panel on the left (wa_x=72) and top (wa_y=30).
    wa_x, wa_y, wa_w, wa_h = 72, 30, 1848, 1023
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert _fits_inside(wa_x, wa_y, wa_w, wa_h, w, h, x, y), (
        f"Window ({w}x{h} at +{x}+{y}) outside offset work area")
    # Must not be left of the panel.
    assert x >= wa_x
    assert y >= wa_y


def test_compute_window_fit_non_zero_origin_x_not_less_than_wa_x():
    # Even if centering math would place us left of wa_x, the clamp must fix it.
    wa_x, wa_y, wa_w, wa_h = 500, 0, 200, 1080
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert x >= wa_x


def test_compute_window_fit_desired_larger_than_width_caps_width():
    # 4000-wide window on a 1920-wide work area must be clamped.
    w, h, x, y = winplace.compute_window_fit(0, 0, 1920, 1080, 4000, 750, reserve=80)
    assert w == 1920


def test_compute_window_fit_very_small_workarea_height_minimum():
    # avail_h is max(wa_h - reserve, 200); even a tiny screen must give at least 200.
    w, h, x, y = winplace.compute_window_fit(0, 0, 400, 300, 400, 300, reserve=80)
    # avail_h = max(300 - 80, 200) = 220; h = min(300, 220) = 220
    assert h == 220
    assert w >= 1 and h >= 1


def test_compute_window_fit_degenerate_screen_never_exceeds_workarea():
    # When the work area is smaller than the 200px reserve-floor, the window must
    # still not exceed (and overflow) the work area — the bottom-edge invariant.
    wa_x, wa_y, wa_w, wa_h = 0, 0, 300, 150
    w, h, x, y = winplace.compute_window_fit(wa_x, wa_y, wa_w, wa_h, 900, 750, reserve=80)
    assert h <= wa_h and w <= wa_w
    assert y + h <= wa_y + wa_h
    assert x + w <= wa_x + wa_w


def test_parse_net_workarea_negative_origin_reported_honestly():
    # A negative origin must be parsed as-is (so the caller's sanity check can
    # reject it) rather than having the minus stripped into a wrong positive.
    line = "_NET_WORKAREA(CARDINAL) = -10, 0, 1920, 1053"
    assert winplace.parse_net_workarea(line) == (-10, 0, 1920, 1053)


# ---------------------------------------------------------------------------
# clear_slot_files / snapshot_for_restore — shared listdir snapshot (existing_files)
# ---------------------------------------------------------------------------

def test_clear_slot_files_uses_provided_snapshot(tmp_path):
    # When existing_files is provided, it's used verbatim instead of listdir — so a
    # name in the snapshot that no longer exists on disk is simply skipped (no error),
    # and on-disk files NOT in the snapshot are left untouched.
    _touch(tmp_path / "123p.png")
    _touch(tmp_path / "123p.jpg")  # on disk but intentionally NOT in the snapshot
    snapshot = ["123p.png", "ghost123p.removed"]  # ghost entry must not raise

    artwork.clear_slot_files(str(tmp_path / "123p.webp"), existing_files=snapshot)

    assert not (tmp_path / "123p.png").exists()  # in snapshot + matches prefix -> removed
    assert (tmp_path / "123p.jpg").exists()      # not in snapshot -> untouched


def test_snapshot_for_restore_uses_provided_snapshot(tmp_path, monkeypatch):
    """When existing_files is provided, snapshot_for_restore uses it verbatim
    instead of re-listdir'ing GRID_FOLDER — an on-disk file NOT in the snapshot
    must not be copied into the store."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "123p.png"))
    _touch(os.path.join(p["GRID_FOLDER"], "123p.jpg"))  # on disk but NOT in the snapshot

    restore.snapshot_for_restore(123, existing_files=["123p.png"])

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["123"]
    assert entry["files"] == ["123p.png"]
    assert not os.path.exists(os.path.join(p["RESTORE_FOLDER"], "123", "123p.jpg"))


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
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(appfiles, "BACKUP_FOLDER", str(tmp_path / "backup"))
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
    changed = steam_files.set_shortcut_icons({100: "/a/icon100.png", 200: "/a/icon200.png"})
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
    assert steam_files.set_shortcut_icons({uid: "/a/i.png"}) == 1
    assert _read_shortcuts(str(sc))["0"]["icon"] == "/a/i.png"


def test_set_shortcut_icons_backs_up_once(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "111")
    steam_files.set_shortcut_icons({100: "/a/i.png"})
    bak = os.path.join(appfiles.BACKUP_FOLDER, "shortcuts.vdf.111.bak")
    assert os.path.exists(bak)
    import shutil
    before = open(bak, "rb").read()
    # A second write must not overwrite the original backup.
    steam_files.set_shortcut_icons({100: "/a/i2.png"})
    assert open(bak, "rb").read() == before


def _two_accounts(tmp_path, monkeypatch):
    """Two account folders, each with its own shortcuts.vdf, and a backup folder
    holding a legacy shortcuts.vdf.bak. Returns {uid: vdf path} and the backup dir."""
    paths = {}
    for uid, appid in (("111", 100), ("222", 200)):
        cfg = tmp_path / "userdata" / uid / "config"
        cfg.mkdir(parents=True)
        _write_shortcuts(str(cfg / "shortcuts.vdf"), {"0": {"appid": appid, "icon": uid}})
        paths[uid] = str(cfg / "shortcuts.vdf")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "shortcuts.vdf.bak").write_bytes(b"LEGACY")
    monkeypatch.setattr(appfiles, "BACKUP_FOLDER", str(backup))
    return paths, backup


def _use_account(monkeypatch, uid, paths):
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", uid)
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", paths[uid])


def test_set_shortcut_icons_backs_up_each_account_separately(tmp_path, monkeypatch):
    """B10: the backup was ONE file taken "if not exists", so after account A's
    first write, account B's first write backed up nothing."""
    paths, backup = _two_accounts(tmp_path, monkeypatch)
    originals = {uid: open(p, "rb").read() for uid, p in paths.items()}

    _use_account(monkeypatch, "111", paths)
    steam_files.set_shortcut_icons({100: "/a/one.png"})
    _use_account(monkeypatch, "222", paths)
    steam_files.set_shortcut_icons({200: "/a/two.png"})
    steam_files.set_shortcut_icons({200: "/a/two-again.png"})

    for uid in ("111", "222"):
        bak = backup / f"shortcuts.vdf.{uid}.bak"
        assert bak.exists(), f"account {uid} must get its own backup"
        assert bak.read_bytes() == originals[uid], "the backup is the pre-write file"
    assert (backup / "shortcuts.vdf.bak").read_bytes() == b"LEGACY", (
        "the legacy backup must be left untouched")


def test_clear_backup_keeps_every_shortcuts_backup(tmp_path, monkeypatch):
    paths, backup = _two_accounts(tmp_path, monkeypatch)
    for uid in ("111", "222"):
        _use_account(monkeypatch, uid, paths)
        steam_files.set_shortcut_icons({100: "/x.png", 200: "/y.png"})
    (backup / "12345p.png").write_bytes(b"x" * (300 * 1024))
    assert steam_files.legacy_backup_size() > 0.0

    steam_files.clear_backup()

    assert sorted(os.listdir(backup)) == [
        "shortcuts.vdf.111.bak", "shortcuts.vdf.222.bak", "shortcuts.vdf.bak"]
    assert steam_files.legacy_backup_size() == 0.0, (
        "shortcuts.vdf backups are never cleared, so they must not count")


def test_set_shortcut_icons_empty_mapping_returns_zero(tmp_path, monkeypatch):
    _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    assert steam_files.set_shortcut_icons({}) == 0


def test_set_shortcut_icons_missing_file_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(tmp_path / "nope.vdf"))
    assert steam_files.set_shortcut_icons({100: "/a/i.png"}) == 0


def test_set_shortcut_icon_single_delegates_and_returns_bool(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    assert steam_files.set_shortcut_icon(100, "/a/i.png") is True
    assert _read_shortcuts(str(sc))["0"]["icon"] == "/a/i.png"
    # No matching appid -> bool False.
    assert steam_files.set_shortcut_icon(999, "/a/i.png") is False


def test_write_shortcuts_atomic_no_temp_left(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    steam_files.set_shortcut_icons({100: "/a/i.png"})
    # The temp file used for the atomic os.replace must not survive the write.
    assert not os.path.exists(os.path.join(str(tmp_path), "shortcuts.vdf.tmp"))


def test_set_shortcut_icons_concurrent_writers_both_land(tmp_path, monkeypatch):
    """B4: the fetch worker and the pending-icon poll can write at the same time.
    Each reads, changes and writes the whole file; without one lock around that,
    the second writer works from the original and drops the first one's icon.
    The patched write holds thread A inside its write long enough for thread B
    to read — the overlap that loses an icon when nothing serialises them."""
    import threading
    import time
    sc = _setup_shortcuts(tmp_path, monkeypatch, {
        "0": {"appid": 100, "icon": ""}, "1": {"appid": 200, "icon": ""}})
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "111")
    real_write = steam_files._write_shortcuts_atomic
    a_writing = threading.Event()

    def slow_write(data):
        if threading.current_thread().name == "A":
            a_writing.set()
            time.sleep(0.3)   # B reads now, unless a lock holds it back
        real_write(data)
    monkeypatch.setattr(steam_files, "_write_shortcuts_atomic", slow_write)

    a = threading.Thread(target=steam_files.set_shortcut_icons, args=({100: "/a/100.png"},), name="A")
    b = threading.Thread(target=steam_files.set_shortcut_icons, args=({200: "/a/200.png"},), name="B")
    a.start()
    assert a_writing.wait(2)
    b.start()
    a.join(5); b.join(5)

    out = _read_shortcuts(str(sc))
    assert out["0"]["icon"] == "/a/100.png", "thread A's icon was lost"
    assert out["1"]["icon"] == "/a/200.png", "thread B's icon was lost"


def test_write_shortcuts_atomic_unique_temp_fsync_then_replace(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": ""}})
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "111")
    os.chmod(sc, 0o644)
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))

    def spy_replace(src, dst):
        events.append(("replace", src, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy_replace)

    assert steam_files.set_shortcut_icons({100: "/a/i.png"}) == 1

    replaces = [e for e in events if isinstance(e, tuple) and e[2] == str(sc)]
    assert len(replaces) == 1
    tmp = replaces[0][1]
    assert os.path.dirname(tmp) == str(tmp_path), "the temp file must sit next to shortcuts.vdf"
    assert os.path.basename(tmp) != "shortcuts.vdf.tmp", "no fixed temp name"
    assert events.index("fsync") < events.index(replaces[0]), "fsync before replace"
    assert sorted(os.listdir(tmp_path)) == ["backup", "shortcuts.vdf"]
    if os.name == "posix":   # Windows has no Unix mode bits to keep
        assert (os.stat(sc).st_mode & 0o777) == 0o644, "the file keeps its mode"


def test_write_shortcuts_atomic_failure_leaves_original_and_no_temp(tmp_path, monkeypatch):
    sc = _setup_shortcuts(tmp_path, monkeypatch, {"0": {"appid": 100, "icon": "old"}})
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "111")
    before = sc.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)

    assert steam_files.set_shortcut_icons({100: "/a/i.png"}) == 0
    assert sc.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == ["backup", "shortcuts.vdf"]


# ---------------------------------------------------------------------------
# stop_steam / start_steam / restart_steam — sequencing + factoring
# ---------------------------------------------------------------------------

def test_restart_steam_calls_stop_sleep_start_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(steam_env, "stop_steam", lambda: calls.append("stop"))
    monkeypatch.setattr(steam_env, "start_steam", lambda: calls.append("start"))
    monkeypatch.setattr(time, "sleep", lambda *_: calls.append("sleep"))
    steam_env.restart_steam()
    assert calls == ["stop", "sleep", "start"]


def test_is_steam_running_force_bypasses_stale_true_cache(monkeypatch):
    # A recent cache says Steam is running (True) but psutil now reports no steam
    # process. Plain is_steam_running() must trust the fresh cache (stale True),
    # while force=True bypasses it, rescans, returns False, AND refreshes the cache.
    monkeypatch.setattr(steam_env, "PSUTIL_AVAILABLE", True)
    monkeypatch.setattr(steam_env, "_steam_cache", (time.monotonic(), True))
    # psutil sees no processes named "steam".
    monkeypatch.setattr(steam_env.psutil, "process_iter", lambda *_a, **_k: iter([]))

    assert steam_env.is_steam_running() is True            # stale cache trusted
    assert steam_env.is_steam_running(force=True) is False  # cache bypassed → real scan
    # The forced scan refreshed the shared cache, so the next plain call sees False.
    assert steam_env.is_steam_running() is False


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
    assert updates._version_tuple(s) == expected


def test_version_tuple_newer_beats_older():
    assert updates._version_tuple("1.2.6") > updates._version_tuple("1.2.5")


def test_version_tuple_major_beats_minor():
    assert updates._version_tuple("2.0.0") > updates._version_tuple("1.99.99")


def test_version_tuple_equal_versions():
    assert updates._version_tuple("1.2.5") == updates._version_tuple("v1.2.5")


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
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0")))
    result = updates.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is True
    assert result["latest"] == "1.3.0"
    assert result["current"] == "1.2.5"


def test_check_for_update_up_to_date(monkeypatch):
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.2.5")))
    result = updates.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is False


def test_check_for_update_older_release_not_available(monkeypatch):
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.0.0")))
    result = updates.check_for_update("1.2.5")
    assert result["available"] is False


def test_check_for_update_network_error_returns_error_dict(monkeypatch):
    slept = []
    def boom(*a, **kw):
        raise requests.exceptions.RequestException("no network")
    monkeypatch.setattr(sgdb, "http_get", boom)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    result = updates.check_for_update("1.2.5")
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
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = updates.check_for_update("1.2.5")
    assert result["url"] == "https://dl/linux"


def test_check_for_update_picks_windows_asset(monkeypatch):
    assets = [
        {"name": "NonSteamScraper-linux", "browser_download_url": "https://dl/linux"},
        {"name": "NonSteamScraper-win.zip", "browser_download_url": "https://dl/win.zip"},
    ]
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    result = updates.check_for_update("1.2.5")
    assert result["url"] == "https://dl/win.zip"


def test_check_for_update_falls_back_to_html_url_when_no_matching_asset(monkeypatch):
    assets = [{"name": "checksums.txt", "browser_download_url": "https://dl/chk"}]
    release_page = "https://github.com/example/releases/tag/v1.3.0"
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(
            _fake_release("v1.3.0", assets=assets, html_url=release_page)))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = updates.check_for_update("1.2.5")
    # "checksums.txt" has an extension ("txt") so bare-binary fallback doesn't
    # match; the function should fall back to html_url.
    assert result["url"] == release_page


def test_check_for_update_linux_bare_binary_fallback(monkeypatch):
    # A file with no extension is the bare binary fallback for Linux.
    assets = [
        {"name": "NonSteamScraper", "browser_download_url": "https://dl/bin"},
        {"name": "checksums.txt", "browser_download_url": "https://dl/chk"},
    ]
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse(_fake_release("v1.3.0", assets=assets)))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = updates.check_for_update("1.2.5")
    assert result["url"] == "https://dl/bin"


def test_check_for_update_empty_tag_returns_error_dict(monkeypatch):
    monkeypatch.setattr(sgdb, "http_get",
        lambda *a, **kw: _FakeResponse({"tag_name": "", "html_url": "x", "assets": []}))
    result = updates.check_for_update("1.2.5")
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
            raise requests.exceptions.RequestException("blip")
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    result = updates.check_for_update("1.2.5")
    assert result is not None
    assert result["available"] is True
    assert result["latest"] == "1.3.0"
    assert calls["n"] == 2      # initial failure + one retry
    assert slept == [1]         # 1 s inter-retry pause


def test_check_for_update_exhausts_retries_returns_error_dict(monkeypatch):
    """Both attempts raise RequestException — returns error dict (network reason)."""
    slept = []

    def fake_get(*a, **kw):
        raise requests.exceptions.RequestException("always down")

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    result = updates.check_for_update("1.2.5")
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

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    result = updates.check_for_update("1.2.5")
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

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    result = updates.check_for_update("1.2.5")
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

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    result = updates.check_for_update("1.2.5")
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

    monkeypatch.setattr(sgdb, "http_get", lambda *a, **kw: _Forbidden())
    result = updates.check_for_update("1.2.5")
    assert "rate limit" in result["error"].lower()
    assert "try again later" in result["error"].lower()


def test_check_for_update_sends_user_agent_header(monkeypatch):
    """Verify the User-Agent header is present in every request."""
    seen_headers = []

    def fake_get(url, headers=None, **kw):
        seen_headers.append(headers or {})
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    updates.check_for_update("1.2.5")
    assert seen_headers, "requests.get was never called"
    assert "User-Agent" in seen_headers[0]
    assert seen_headers[0]["User-Agent"]   # non-empty


def test_check_for_update_uses_10s_timeout(monkeypatch):
    """Verify timeout=10 is passed to requests.get."""
    seen_timeouts = []

    def fake_get(url, timeout=None, **kw):
        seen_timeouts.append(timeout)
        return _FakeResponse(_fake_release("v1.3.0"))

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    updates.check_for_update("1.2.5")
    assert seen_timeouts == [10]


# ---------------------------------------------------------------------------
# FetchedArt — what download_all_artwork returns (T-A6).
#
# REGRESSION HISTORY: the return value used to be ONE dict mixing art-slot
# dicts with an "icon_to_set" tuple. A consumer doing
# `for v in results.values(): v.get(...)` raised AttributeError the moment a
# game had an icon, in a daemon thread, and the UI hung on "Fetching…". The
# slots and the icon write are now separate fields, so that mistake cannot be
# written; these tests pin the two accessors and drive the real assembly.
# ---------------------------------------------------------------------------

def _slot(applied_index, applied_path, failed=False, label="Cover"):
    """Minimal art-slot dict shaped like download_all_artwork emits."""
    return {
        "applied_path": applied_path, "failed": failed, "label": label,
        "applied_index": applied_index, "option_urls": [],
        "option_meta": [], "thumb_paths": [],
        "filename_base": "x",
    }


def _fetched(slots, icon_write=None):
    art = artwork.FetchedArt()
    art.slots.update(slots)
    art.icon_write = icon_write
    return art


def test_fetched_art_applied_paths_collects_only_applied_slots():
    art = _fetched({
        "grids":      _slot(0, "/g/123p.png"),       # applied
        "grids_wide": _slot(None, "/g/123.png"),     # not applied (animated)
        "heroes":     None,                          # missing slot
        "logos":      _slot(2, "/g/123_logo.png"),   # applied
        "icons":      _slot(0, "/g/123_icon.png"),   # applied
    }, icon_write=(12345, "/g/12345_icon.png"))
    assert art.applied_paths() == ["/g/123p.png", "/g/123_logo.png", "/g/123_icon.png"]


def test_fetched_art_applied_paths_skips_applied_index_none_and_no_path():
    art = _fetched({"a": _slot(None, "/g/a.png"), "b": _slot(0, "")})
    assert art.applied_paths() == []


def test_fetched_art_failed_labels_in_slot_order():
    art = _fetched({"grids": _slot(None, "/g/p.png", failed=True, label="Cover"),
                    "heroes": _slot(0, "/g/h.png"),
                    "logos": None,
                    "icons": _slot(None, "/g/i.png", failed=True, label="Icon")})
    assert art.failed_labels() == ["Cover", "Icon"]


def test_fetched_art_keeps_the_icon_write_out_of_the_slots():
    art = _fetched({"grids": _slot(0, "/g/p.png")}, icon_write=(12345, "/g/12345_icon.png"))
    assert art.icon_write == (12345, "/g/12345_icon.png")
    assert all(v is None or isinstance(v, dict) for v in art.slots.values())


def test_download_all_artwork_defer_icon_shape_consumable(tmp_path, monkeypatch):
    """Drive the REAL defer_icon=True assembly with network mocked at the lowest
    seam (get_artwork / download_artwork), then feed its return into the consumer
    helpers. This is the regression test that would have caught the hang: a game
    with an icon yields FetchedArt.icon_write as a tuple, and the consumer must
    not choke on it."""
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(tmp_path / "grid"))
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(tmp_path / "cache"))
    monkeypatch.setattr(appfiles, "BACKUP_FOLDER", str(tmp_path / "backup"))
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(tmp_path / "managed"))
    monkeypatch.setattr(appfiles, "RESTORE_FOLDER", str(tmp_path / "restore"))
    os.makedirs(steam_env.GRID_FOLDER, exist_ok=True)

    # Every slot returns one static PNG option so each slot auto-applies (incl.
    # icons -> exercises the deferred icon write).
    def fake_get_artwork(sgdb_id, art_type, prefs=None, dimensions=None):
        return [{"url": f"https://sgdb/{art_type}.png", "mime": "image/png"}]

    written = []

    def fake_download_artwork(url, save_path, register=True):
        # Pretend the download succeeded; record the target so we can assert paths.
        with open(save_path, "wb") as f:
            f.write(b"x")
        written.append(save_path)
        return True

    monkeypatch.setattr(sgdb, "get_artwork", fake_get_artwork)
    monkeypatch.setattr(artwork, "download_artwork", fake_download_artwork)
    # set_shortcut_icon must NOT be called in defer mode; fail loudly if it is.
    monkeypatch.setattr(steam_files, "set_shortcut_icon",
                        lambda *a, **k: pytest.fail("icon write not deferred"))

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    # 1) the deferred icon write is present, apart from the slots.
    assert results.icon_write is not None
    assert results.icon_write[0] == 12345
    assert isinstance(results.icon_write, tuple)

    # 2) Feeding the real return into the consumer helpers must NOT raise.
    paths = results.applied_paths()         # would AttributeError pre-fix
    assert all(isinstance(p, str) for p in paths)
    # The auto-applied icon path is collected as an applied slot path.
    icon_uid, icon_path = results.icon_write
    assert icon_uid == 12345
    assert icon_path in paths

    # 3) Shape sanity: every art slot maps to a dict.
    for slot in ("grids", "grids_wide", "heroes", "logos", "icons"):
        assert isinstance(results.slots[slot], dict)


# ---------------------------------------------------------------------------
# Pending (deferred) icon writes — defer & auto-apply
# ---------------------------------------------------------------------------

def test_save_pending_icons_merges_with_existing(tmp_path, monkeypatch):
    pf = tmp_path / "pending"
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(pf))
    steam_files.save_pending_icons({100: "/a/i100.png"})
    steam_files.save_pending_icons({200: "/a/i200.png", 100: "/a/i100b.png"})  # 100 overwritten
    loaded = steam_files.load_pending_icons()
    assert loaded == {100: "/a/i100b.png", 200: "/a/i200.png"}


def test_load_pending_icons_round_trips_int_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    steam_files.save_pending_icons({12345: "/a/i.png"})
    loaded = steam_files.load_pending_icons()
    assert loaded == {12345: "/a/i.png"}
    assert all(isinstance(k, int) for k in loaded)  # keys back to int, not str


def test_load_pending_icons_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "nope"))
    assert steam_files.load_pending_icons() == {}


def test_has_and_clear_pending_icons_reflect_state(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    assert steam_files.has_pending_icons() is False
    steam_files.save_pending_icons({100: "/a/i.png"})
    assert steam_files.has_pending_icons() is True
    steam_files.clear_pending_icons()
    assert steam_files.has_pending_icons() is False
    steam_files.clear_pending_icons()  # idempotent — must not raise when already gone


def test_apply_pending_icons_skips_and_keeps_file_when_steam_running(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(tmp_path / "shortcuts.vdf"))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: True)
    called = []
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: called.append(m) or len(m))
    steam_files.save_pending_icons({100: "/a/i.png"})
    assert steam_files.apply_pending_icons() == 0
    assert called == []                    # never wrote while Steam open
    assert steam_files.has_pending_icons() is True  # pending file left intact for next time


def test_apply_pending_icons_writes_and_clears_when_steam_closed(tmp_path, monkeypatch):
    icon = tmp_path / "i.png"
    _touch(icon)  # path must exist so it isn't filtered out
    sc = tmp_path / "shortcuts.vdf"
    sc.write_bytes(b"x")  # SHORTCUTS_PATH just needs to exist for the guard
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: False)
    captured = {}
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: captured.update(m) or len(m))
    steam_files.save_pending_icons({100: str(icon)})
    assert steam_files.apply_pending_icons() == 1
    assert captured == {100: str(icon)}    # right mapping passed through
    assert steam_files.has_pending_icons() is False  # cleared after a real write attempt


def test_apply_pending_icons_drops_missing_icon_paths(tmp_path, monkeypatch):
    sc = tmp_path / "shortcuts.vdf"; sc.write_bytes(b"x")
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: False)
    captured = []
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: captured.append(m) or len(m))
    steam_files.save_pending_icons({100: str(tmp_path / "gone.png")})  # path does not exist
    # Nothing valid to write -> set_shortcut_icons not called, but pending still cleared
    # (those icon files are gone; nothing to retry).
    assert steam_files.apply_pending_icons() == 0
    assert captured == []
    assert steam_files.has_pending_icons() is False


def test_pending_icons_are_namespaced_per_account(tmp_path, monkeypatch):
    """Queues under different accounts must not see or overwrite each other — the
    file path is derived from the live STEAM_USER_ID."""
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))

    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    steam_files.save_pending_icons({100: "/a/iA.png"})

    # Switch to account B: B's queue is independent and starts empty.
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "BBB")
    assert steam_files.load_pending_icons() == {}
    assert steam_files.has_pending_icons() is False
    steam_files.save_pending_icons({200: "/b/iB.png"})

    # Back to A: its original queue is untouched by anything B did.
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    assert steam_files.load_pending_icons() == {100: "/a/iA.png"}
    # And the two queues live in distinct files.
    assert (tmp_path / "pending_AAA").exists()
    assert (tmp_path / "pending_BBB").exists()


def test_apply_pending_icons_does_not_clear_other_accounts_queue(tmp_path, monkeypatch):
    """THE regression: applying while a DIFFERENT account is active must never touch
    (let alone clear) another account's queued icons. Previously the queue was
    account-agnostic, so applying under B wiped A's icons forever."""
    sc = tmp_path / "shortcuts.vdf"; sc.write_bytes(b"x")
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: False)
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: len(m))

    # Account A queues an icon.
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    steam_files.save_pending_icons({100: "/a/iA.png"})

    # Account B is active when the poll fires with Steam closed.
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "BBB")
    assert steam_files.apply_pending_icons() == 0      # B has nothing to write

    # A's queue must still be intact (the old code cleared it here).
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    assert steam_files.load_pending_icons() == {100: "/a/iA.png"}


def test_pending_icons_legacy_file_migrates_to_active_account(tmp_path, monkeypatch):
    """A v1.3.0 un-namespaced queue is adopted by the account active at upgrade time,
    not orphaned."""
    base = tmp_path / "pending"
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(base))
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    # Simulate a leftover legacy (un-namespaced) queue from v1.3.0.
    base.write_text('{"100": "/a/legacy.png"}', encoding="utf-8")

    assert steam_files.load_pending_icons() == {100: "/a/legacy.png"}
    assert (tmp_path / "pending_AAA").exists()  # moved under the active account
    assert not base.exists()                    # legacy file consumed


def test_full_reset_preserves_art_but_clears_settings(tmp_path, monkeypatch):
    """Factory reset clears SETTINGS but must NEVER delete the user's fetched art:
    clear_managed_artwork/clear_backup are not called, the managed registry survives,
    and only the regenerable thumbnail cache + settings/pending files are removed."""
    calls = []
    monkeypatch.setattr(appfiles, "clear_cache", lambda: calls.append("cache"))
    monkeypatch.setattr(artwork, "clear_managed_artwork", lambda: calls.append("art"))
    monkeypatch.setattr(steam_files, "clear_backup", lambda: calls.append("backup"))

    # Settings/state files — these SHOULD be removed.
    settings = {}
    for name in ("APIKEY_FILE", "PREFS_FILE", "SKIP_FILE", "NAMES_FILE", "IDS_FILE",
                 "ACCOUNT_FILE", "FIRSTRUN_FILE", "THEME_FILE", "ACCENT_FILE",
                 "GEOMETRY_FILE"):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        monkeypatch.setattr(_owner(name), name, str(p))
        settings[name] = p

    # Managed-file registry — must be PRESERVED so the art (and 'Clear All Artwork') survive.
    managed = tmp_path / "managed"
    managed.write_text("123p.png\n", encoding="utf-8")
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(managed))

    # A pending (un-applied) icon queue — should be swept.
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    monkeypatch.setattr(steam_env, "STEAM_USER_ID", "AAA")
    steam_files.save_pending_icons({100: "/a/i.png"})

    appfiles.full_reset()

    assert "art" not in calls               # never deletes applied artwork
    assert "backup" not in calls            # backup (art history) preserved
    assert "cache" in calls                 # regenerable cache cleared
    assert managed.exists()                 # managed registry preserved
    for name, p in settings.items():
        assert not p.exists(), f"{name} should have been cleared"
    assert steam_files.has_pending_icons() is False  # pending queue swept


# ---------------------------------------------------------------------------
# download_all_artwork — full fetch-pipeline INTEGRATION tests.
#
# These drive the REAL download_all_artwork end-to-end, mocking the network at
# the LOWEST seam — sgdb._sgdb_get — so the genuine get_artwork tiered-fallback,
# is_animated_image classification, applied_index selection, option_meta vote
# plumbing, and the deferred icon write all execute for real. Only the
# byte-level downloads (artwork.download_artwork) and the OS/Steam side effects are
# stubbed. This is the seam whose worst historical bug — a tuple reaching a
# `.get()` in a values()-loop — silently hung the UI on "Fetching…" forever.
# ---------------------------------------------------------------------------

def _sgdb_payload(assets):
    """Wrap a list of SGDB asset dicts in the {success, data} envelope that
    get_artwork's `.json()` consumer expects, returned via a tiny object that
    only needs a .json() method — exactly what _sgdb_get yields in production."""
    payload = {"success": True, "data": list(assets)}
    return types.SimpleNamespace(json=lambda p=payload: p)


def _install_fetch_mocks(monkeypatch, tmp_path, slot_assets,
                         record_icon=True):
    """Wire up every external dependency download_all_artwork touches so it runs
    purely in-memory under tmp_path. `slot_assets` maps an endpoint substring
    ("grids"/"heroes"/"logos"/"icons") -> the list of assets _sgdb_get returns
    for any URL hitting that endpoint. Returns the recorder list passed to a fake
    set_shortcut_icon so callers can assert it was / wasn't invoked.

    NOTE: get_artwork calls _sgdb_get multiple times per slot (tiered fallback);
    branching purely on the endpoint substring makes the fake deterministic and,
    because every endpoint always returns >=1 asset here, the Tier-3 fill loop
    stops immediately."""
    grid = tmp_path / "grid"
    cache = tmp_path / "cache"
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))
    monkeypatch.setattr(appfiles, "BACKUP_FOLDER", str(tmp_path / "backup"))
    os.makedirs(grid, exist_ok=True)

    def fake_sgdb_get(url, headers=None, params=None, timeout=None, retries=3):
        # grids_wide also hits the "grids" endpoint (split only by dimensions),
        # so the "grids" branch deliberately serves both portrait + wide covers.
        for key, assets in slot_assets.items():
            if f"/{key}/" in url:
                return _sgdb_payload(assets)
        return _sgdb_payload([])

    def fake_download_artwork(url, save_path, register=True):
        # Create an empty file so applied_path / thumb_paths actually exist on
        # disk, then report success — no network, no real image bytes.
        with open(save_path, "wb") as f:
            f.write(b"")
        return True

    icon_calls = []
    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get)
    monkeypatch.setattr(artwork, "download_artwork", fake_download_artwork)
    monkeypatch.setattr(appfiles, "register_managed_file", lambda *a, **k: None)
    monkeypatch.setattr(restore, "snapshot_for_restore", lambda *a, **k: None)
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(appfiles, "load_api_key", lambda *a, **k: "FAKEKEY")
    if record_icon:
        monkeypatch.setattr(
            steam_files, "set_shortcut_icon",
            lambda uid, path, *a, **k: icon_calls.append((uid, path)) or True)
    else:
        monkeypatch.setattr(
            steam_files, "set_shortcut_icon",
            lambda *a, **k: pytest.fail("set_shortcut_icon must not be called"))
    return icon_calls


def _static(url, **extra):
    """A static SGDB asset (PNG). Extra keys (nsfw/humor/style/...) override."""
    a = {"url": url, "thumb": url, "mime": "image/png",
         "nsfw": False, "humor": False, "style": "alternate"}
    a.update(extra)
    return a


def _animated(url, **extra):
    """An animated SGDB asset. is_animated_image keys off mime image/webp|gif."""
    a = {"url": url, "thumb": url, "mime": "image/webp",
         "nsfw": False, "humor": False, "style": "alternate"}
    a.update(extra)
    return a


def test_download_all_artwork_happy_path_all_slots_populated(tmp_path, monkeypatch):
    """All 5 slots return a single static asset -> every slot dict is populated,
    applied_index points at the first (only) static option, option_urls echo the
    inputs, and option_meta carries animated/nsfw/humor."""
    slot_assets = {
        "grids":  [_static("https://sgdb/grids.png", nsfw=True, humor=False)],
        "heroes": [_static("https://sgdb/heroes.png", nsfw=False, humor=True)],
        "logos":  [_static("https://sgdb/logos.png")],
        "icons":  [_static("https://sgdb/icons.png")],
    }
    icon_calls = _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=False)

    for slot in ("grids", "grids_wide", "heroes", "logos", "icons"):
        sd = results.slots[slot]
        assert isinstance(sd, dict), f"{slot} should be a populated dict"
        # The only option is static -> auto-applied at index 0.
        assert sd["applied_index"] == 0
        assert sd["applied_path"]  # a target path was assigned
        assert sd["option_meta"][0]["animated"] is False

    # grids and grids_wide both come from the "grids" endpoint assets.
    assert results.slots["grids"]["option_urls"] == ["https://sgdb/grids.png"]
    assert results.slots["grids_wide"]["option_urls"] == ["https://sgdb/grids.png"]
    assert results.slots["heroes"]["option_urls"] == ["https://sgdb/heroes.png"]

    # Per-slot metadata flows through verbatim.
    gm = results.slots["grids"]["option_meta"][0]
    assert gm["nsfw"] is True and gm["humor"] is False
    hm = results.slots["heroes"]["option_meta"][0]
    assert hm["nsfw"] is False and hm["humor"] is True

    # Non-defer mode: the icon was written immediately, icon_write stays None.
    assert results.icon_write is None
    assert len(icon_calls) == 1 and icon_calls[0][0] == 12345


def test_download_all_artwork_deferred_icon_write_contract(tmp_path, monkeypatch):
    """THE regression guard. defer_icon=True with a static icon -> icon_write is
    the (unsigned_id, path) TUPLE; both consumers handle the results dict without
    choking on that tuple, and the deferred icon is NOT written eagerly."""
    slot_assets = {
        "grids":  [_static("https://sgdb/grids.png")],
        "heroes": [_static("https://sgdb/heroes.png")],
        "logos":  [_static("https://sgdb/logos.png")],
        "icons":  [_static("https://sgdb/icons.png")],
    }
    # record_icon=False -> fail loudly if set_shortcut_icon fires in defer mode.
    _install_fetch_mocks(monkeypatch, tmp_path, slot_assets, record_icon=False)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    # icon_write is the deferred (uid, path) pair, kept apart from the slots.
    assert isinstance(results.icon_write, tuple)
    uid, path = results.icon_write
    assert uid == 12345
    assert path == results.slots["icons"]["applied_path"]

    # ...and it is exactly that pair.
    assert results.icon_write == (12345, path)

    # applied_paths() returns a list of str paths, the icon's among them.
    paths = results.applied_paths()
    assert isinstance(paths, list)
    assert all(isinstance(p, str) for p in paths)
    assert path in paths  # the icon's applied path is collected as a slot path


def test_download_all_artwork_non_defer_writes_icon(tmp_path, monkeypatch):
    """defer_icon=False with a static icon -> icon_write is None and
    set_shortcut_icon WAS called with (unsigned_id, icon_path)."""
    slot_assets = {
        "grids":  [_static("https://sgdb/grids.png")],
        "heroes": [_static("https://sgdb/heroes.png")],
        "logos":  [_static("https://sgdb/logos.png")],
        "icons":  [_static("https://sgdb/icons.png")],
    }
    icon_calls = _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=False)

    assert results.icon_write is None
    assert icon_calls == [(12345, results.slots["icons"]["applied_path"])]


def _failing_downloads(monkeypatch, bad_urls):
    """Replace the fetch mocks' download with one that fails for `bad_urls` and
    writes b"NEW" for every other URL. Returns the list of attempted save paths."""
    attempted = []

    def fake_download_artwork(url, save_path, register=True):
        attempted.append(save_path)
        if url in bad_urls:
            return False
        with open(save_path, "wb") as f:
            f.write(b"NEW")
        return True
    monkeypatch.setattr(artwork, "download_artwork", fake_download_artwork)
    return attempted


_ALL_STATIC = {
    "grids":  [_static("https://sgdb/grids.png")],
    "heroes": [_static("https://sgdb/heroes.png")],
    "logos":  [_static("https://sgdb/logos.png")],
    "icons":  [_static("https://sgdb/icons.png")],
}


def test_download_all_artwork_failed_download_keeps_the_old_file(tmp_path, monkeypatch):
    """B5: the old cover was deleted BEFORE the download, so a CDN timeout left
    the slot empty while the results said "Applied!". The old file must survive
    a failed download, whatever its extension, and the slot must report failure."""
    _install_fetch_mocks(monkeypatch, tmp_path, _ALL_STATIC)
    grid = tmp_path / "grid"
    (grid / "12345p.jpg").write_bytes(b"OLD-JPG")
    (grid / "12345p.png").write_bytes(b"OLD-PNG")
    _failing_downloads(monkeypatch, {"https://sgdb/grids.png"})

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    assert (grid / "12345p.jpg").read_bytes() == b"OLD-JPG"
    assert (grid / "12345p.png").read_bytes() == b"OLD-PNG"
    assert results.slots["grids"]["failed"] is True
    assert results.slots["grids"]["applied_index"] is None
    assert results.slots["grids"]["applied_path"] not in results.applied_paths()
    # The mocks serve grids.png to the wide cover too, so it fails the same way.
    assert results.failed_labels() == ["Cover", "Wide Cover"]


def test_download_all_artwork_failed_icon_makes_no_icon_write(tmp_path, monkeypatch):
    _install_fetch_mocks(monkeypatch, tmp_path, _ALL_STATIC, record_icon=False)
    _failing_downloads(monkeypatch, {"https://sgdb/icons.png"})

    deferred = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)
    assert deferred.icon_write is None
    assert deferred.icon_write is None
    # Non-defer mode: record_icon=False fails the test if set_shortcut_icon runs.
    direct = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=False)
    assert direct.slots["icons"]["failed"] is True


def test_download_all_artwork_success_replaces_a_different_extension(tmp_path, monkeypatch):
    _install_fetch_mocks(monkeypatch, tmp_path, _ALL_STATIC)
    grid = tmp_path / "grid"
    (grid / "12345p.jpg").write_bytes(b"OLD-JPG")
    (grid / "123456p.jpg").write_bytes(b"NEIGHBOUR")
    _failing_downloads(monkeypatch, set())

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    assert not (grid / "12345p.jpg").exists(), "the old extension must be cleared"
    assert (grid / "12345p.png").read_bytes() == b"NEW"
    assert (grid / "123456p.jpg").read_bytes() == b"NEIGHBOUR"
    assert results.slots["grids"]["failed"] is False
    assert results.failed_labels() == []


def test_remove_partial_downloads_only_the_runs_part_files(tmp_path, monkeypatch):
    grid, cache = tmp_path / "grid", tmp_path / "cache"
    grid.mkdir(); cache.mkdir()
    keep = ["12345p.png", "12345.png.bak", "123456p.png.part", "999_hero.png.part", "notes.part.txt"]
    gone = ["12345p.png.part", "12345_icon.ico.part"]
    for f in keep + gone:
        (grid / f).write_bytes(b"x")
    (cache / "12345p_0.png.part").write_bytes(b"x")
    (cache / "12345p_1.png").write_bytes(b"x")
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))

    assert artwork.remove_partial_downloads(str(grid), [12345]) == 3

    assert sorted(os.listdir(grid)) == sorted(keep)
    assert os.listdir(cache) == ["12345p_1.png"]


def _grid_and_cache(tmp_path, monkeypatch):
    grid, cache = tmp_path / "grid", tmp_path / "cache"
    grid.mkdir(); cache.mkdir()
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))
    monkeypatch.setattr(appfiles, "register_managed_file", lambda p: None)
    return grid, cache


def test_install_into_slot_failed_static_download_keeps_old_file_no_partial(tmp_path, monkeypatch):
    """The real download path: the CDN dies mid-body. The old art stays and no
    .part is left in Steam's grid folder."""
    grid, _cache = _grid_and_cache(tmp_path, monkeypatch)
    (grid / "12345p.jpg").write_bytes(b"OLD")

    class _Dying:
        status_code = 200
        headers = {}

        def iter_content(self, n):
            yield b"half"
            raise requests.exceptions.ConnectionError("reset")
    monkeypatch.setattr(sgdb, "http_get", lambda *a, **k: _Dying())

    assert artwork.install_into_slot(str(grid / "12345p.png"), "https://x/new.png") is False
    assert sorted(os.listdir(grid)) == ["12345p.jpg"]
    assert (grid / "12345p.jpg").read_bytes() == b"OLD"


def test_install_into_slot_failed_apng_encode_keeps_old_file_no_partial(tmp_path, monkeypatch):
    grid, cache = _grid_and_cache(tmp_path, monkeypatch)
    (grid / "12345p.png").write_bytes(b"OLD")
    (grid / "12345p.jpg").write_bytes(b"OLD-JPG")

    def fake_download(url, save_path, register=True):
        with open(save_path, "wb") as f:
            f.write(b"webp")
        return True

    def failing_encode(src, out, progress_cb=None, status_cb=None):
        with open(out, "wb") as f:
            f.write(b"half an apng")
        raise RuntimeError("encoder crashed")
    monkeypatch.setattr(artwork, "download_artwork", fake_download)
    monkeypatch.setattr(artwork, "convert_animated_to_apng", failing_encode)

    assert artwork.install_into_slot(str(grid / "12345p.png"), "https://x/a.webp",
                                animated=True) is False
    assert sorted(os.listdir(grid)) == ["12345p.jpg", "12345p.png"]
    assert (grid / "12345p.png").read_bytes() == b"OLD"
    assert os.listdir(cache) == [], "no source download or encode output may be left"


def test_install_into_slot_apng_appears_only_after_the_encode(tmp_path, monkeypatch):
    """B7: the encode wrote straight into the live grid file, and the source
    download sat in the grid folder too. Both must happen outside it; the APNG
    appears in the slot only once the encode has finished."""
    grid, cache = _grid_and_cache(tmp_path, monkeypatch)
    (grid / "12345p.jpg").write_bytes(b"OLD-JPG")
    out_path = grid / "12345p.png"
    seen = {}

    def fake_download(url, save_path, register=True):
        seen["src_dir"] = os.path.dirname(save_path)
        with open(save_path, "wb") as f:
            f.write(b"webp")
        return True

    def watching_encode(src, out, progress_cb=None, status_cb=None):
        seen["out_dir"] = os.path.dirname(out)
        seen["grid_during_encode"] = sorted(os.listdir(grid))
        with open(out, "wb") as f:
            f.write(b"APNG")
        return True
    monkeypatch.setattr(artwork, "download_artwork", fake_download)
    monkeypatch.setattr(artwork, "convert_animated_to_apng", watching_encode)

    assert artwork.install_into_slot(str(out_path), "https://x/a.webp", animated=True) is True

    assert seen["src_dir"] == str(cache), "the source download must not sit in the grid folder"
    assert seen["out_dir"] == str(cache), "the encode must not write in the grid folder"
    assert seen["grid_during_encode"] == ["12345p.jpg"], "nothing new in the slot mid-encode"
    assert sorted(os.listdir(grid)) == ["12345p.png"], "old extension cleared after the install"
    assert out_path.read_bytes() == b"APNG"
    assert os.listdir(cache) == []


def test_install_into_slot_same_extension_success_keeps_the_new_file(tmp_path, monkeypatch):
    """The listing taken before the download names the OLD 12345p.png; after a
    successful download that name is the NEW file and must not be cleared."""
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "12345p.png").write_bytes(b"OLD")
    listing = os.listdir(grid)
    monkeypatch.setattr(appfiles, "register_managed_file", lambda p: None)
    _failing_downloads(monkeypatch, set())

    assert artwork.install_into_slot(str(grid / "12345p.png"), "https://x/a.png",
                                existing_files=listing) is True
    assert (grid / "12345p.png").read_bytes() == b"NEW"


def test_download_all_artwork_all_animated_slot_not_auto_applied(tmp_path, monkeypatch):
    """A slot whose every option is animated -> applied_index is None,
    applied_path falls back to the .png target (committed on demand later), and
    for the icons slot no icon write happens at all."""
    slot_assets = {
        "grids":  [_static("https://sgdb/grids.png")],
        "heroes": [_static("https://sgdb/heroes.png")],
        "logos":  [_static("https://sgdb/logos.png")],
        # Every icon option is animated -> nothing to auto-apply.
        "icons":  [_animated("https://sgdb/icon0.webp"),
                   _animated("https://sgdb/icon1.gif")],
    }
    icon_calls = _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=False)

    icons = results.slots["icons"]
    assert icons is not None  # options exist, just none auto-applied
    assert icons["applied_index"] is None
    assert icons["applied_path"].endswith(".png")
    assert all(m["animated"] is True for m in icons["option_meta"])

    # No static icon was applied -> no eager write, no deferred tuple.
    assert icon_calls == []
    assert results.icon_write is None
    # A static slot still auto-applies normally alongside the animated one.
    assert results.slots["grids"]["applied_index"] == 0


def test_download_all_artwork_missing_slot_is_none_and_consumable(tmp_path, monkeypatch):
    """A slot whose _sgdb_get returns empty data -> results.slots[slot] is None, and
    both consumer helpers tolerate the None slot without crashing."""
    slot_assets = {
        "grids":  [_static("https://sgdb/grids.png")],
        "heroes": [],  # nothing found for the hero slot
        "logos":  [_static("https://sgdb/logos.png")],
        "icons":  [_static("https://sgdb/icons.png")],
    }
    _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    assert results.slots["heroes"] is None
    # Consumers must skip the None slot (and the icon tuple) without raising.
    paths = results.applied_paths()
    assert isinstance(paths, list)
    assert results.icon_write[0] == 12345


# ---------------------------------------------------------------------------
# S2 — the http_get seam (T2): one GET path, one shared pooled session
# ---------------------------------------------------------------------------

class _FakeStreamResp:
    """Minimal requests.Response stand-in with iter_content, for the seam gate
    on download_artwork."""
    def __init__(self, status_code, chunks=(), headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {}

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def _forbidden_get(*args, **kwargs):
    raise AssertionError("bare requests.get called")


def test_http_seam_get_exists_and_is_callable():
    assert hasattr(sgdb, "http_get"), "the http_get seam does not exist yet"
    assert callable(sgdb.http_get)


def test_http_seam_session_accessor_returns_single_shared_session():
    assert hasattr(sgdb, "_get_session"), "the _get_session accessor does not exist yet"
    s1 = sgdb._get_session()
    s2 = sgdb._get_session()
    assert s1 is s2


def test_http_seam_sgdb_get_does_not_bypass_the_seam(monkeypatch):
    """_sgdb_get must route through http_get; a bare requests.get call is the
    failure being guarded against."""
    assert hasattr(sgdb, "http_get"), "the http_get seam does not exist yet"
    used = []

    def fake_http_get(url, **kwargs):
        used.append((url, kwargs))
        return _FakeResp(200)

    monkeypatch.setattr(sgdb, "http_get", fake_http_get)
    monkeypatch.setattr(requests, "get", _forbidden_get)

    r = sgdb._sgdb_get("http://x", headers={})

    assert r.status_code == 200
    assert len(used) == 1
    assert used[0][0] == "http://x"


def test_http_seam_download_artwork_uses_seam_with_stream_and_timeout(monkeypatch, tmp_path):
    """download_artwork must use the seam with stream=True and timeout=10,
    matching the download call."""
    assert hasattr(sgdb, "http_get"), "the http_get seam does not exist yet"
    seen = {}

    def fake_http_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _FakeStreamResp(200, [b"abc"], {"Content-Length": "3"})

    monkeypatch.setattr(sgdb, "http_get", fake_http_get)
    monkeypatch.setattr(requests, "get", _forbidden_get)
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(tmp_path / "managed"))

    save_path = str(tmp_path / "12345p.png")
    assert artwork.download_artwork("http://x/img.png", save_path) is True

    assert seen["url"] == "http://x/img.png"
    assert seen.get("stream") is True
    assert seen.get("timeout") == 10
    assert os.path.exists(save_path)


# ---------------------------------------------------------------------------
# S2 — rate-limit visibility hook (T3)
# ---------------------------------------------------------------------------

def test_rate_limit_hook_reports_retry_after_wait(monkeypatch):
    """A 429 with Retry-After: 3 must call RATE_LIMIT_HOOK once with a message
    mentioning the 3-second wait, so the wait shows up in the activity log."""
    assert hasattr(sgdb, "RATE_LIMIT_HOOK"), "the RATE_LIMIT_HOOK module hook does not exist yet"
    responses = [_FakeResp(429, headers={"Retry-After": "3"}), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    messages = []
    monkeypatch.setattr(sgdb, "RATE_LIMIT_HOOK", messages.append)

    r = sgdb._sgdb_get("http://x", headers={})

    assert r.status_code == 200
    assert len(messages) == 1
    assert "3" in messages[0]


def test_rate_limit_hook_raising_never_breaks_sgdb_get(monkeypatch):
    """A hook that raises must be swallowed — sgdb stays UI-free and a
    broken hook must never take a fetch down."""
    assert hasattr(sgdb, "RATE_LIMIT_HOOK"), "the RATE_LIMIT_HOOK module hook does not exist yet"
    responses = [_FakeResp(429, headers={"Retry-After": "1"}), _FakeResp(200)]
    calls = {"get": 0}

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        r = responses[calls["get"]]
        calls["get"] += 1
        return r

    monkeypatch.setattr(sgdb, "http_get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def bad_hook(msg):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr(sgdb, "RATE_LIMIT_HOOK", bad_hook)

    r = sgdb._sgdb_get("http://x", headers={})

    assert r.status_code == 200


# ---------------------------------------------------------------------------
# S3 — restore point: snapshot / run lifecycle / undo (T4)
# ---------------------------------------------------------------------------
# Store shape (SPEC.md S3): ~/.steamart_restore/manifest.json + <id>/<file>
# copies. Every gate below points GRID_FOLDER, RESTORE_FOLDER, MANAGED_FILE,
# BACKUP_FOLDER and SHORTCUTS_PATH at tmp_path and leads with a presence
# assertion, so the missing product code fails by ASSERTION, never by
# AttributeError.

def _restore_setup(tmp_path, monkeypatch):
    """Point every path constant a restore-point test touches at tmp_path and
    create the grid folder. Returns the patched locations as a dict."""
    grid = tmp_path / "grid"
    grid.mkdir()
    paths = {
        "GRID_FOLDER": str(grid),
        "RESTORE_FOLDER": str(tmp_path / "restore"),
        "MANAGED_FILE": str(tmp_path / "managed"),
        "BACKUP_FOLDER": str(tmp_path / "backup"),
        "SHORTCUTS_PATH": str(tmp_path / "shortcuts.vdf"),
    }
    for name, value in paths.items():
        monkeypatch.setattr(_owner(name), name, value)
    return paths


def _read_manifest(restore_folder):
    """Load the restore store's manifest.json (SPEC.md S3 shape)."""
    with open(os.path.join(restore_folder, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def test_restore_snapshot_copies_files_and_writes_manifest(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    _touch(os.path.join(p["GRID_FOLDER"], "12345_hero.jpg"))
    _touch(os.path.join(p["GRID_FOLDER"], "99999p.png"))  # a different game

    restore.snapshot_for_restore(12345)

    manifest = _read_manifest(p["RESTORE_FOLDER"])
    entry = manifest["entries"]["12345"]
    assert sorted(entry["files"]) == ["12345_hero.jpg", "12345p.png"]
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "12345p.png"))
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "12345_hero.jpg"))
    assert not os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "99999p.png"))


def test_restore_snapshot_records_unmanaged_files(tmp_path, monkeypatch):
    """clear_slot_files deletes art this app never registered, so a managed-only
    snapshot cannot restore what those paths destroy."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    managed_art = os.path.join(p["GRID_FOLDER"], "12345p.png")
    unmanaged_art = os.path.join(p["GRID_FOLDER"], "12345_hero.jpg")
    _touch(managed_art)
    _touch(unmanaged_art)
    appfiles.register_managed_file(managed_art)

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert "12345_hero.jpg" in entry["files"]
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "12345_hero.jpg"))


def test_restore_snapshot_records_managed_subset(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    managed_art = os.path.join(p["GRID_FOLDER"], "12345p.png")
    unmanaged_art = os.path.join(p["GRID_FOLDER"], "12345_hero.jpg")
    _touch(managed_art)
    _touch(unmanaged_art)
    appfiles.register_managed_file(managed_art)

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert set(entry["managed"]) == {"12345p.png"}


def test_restore_snapshot_first_wins_while_unsealed(tmp_path, monkeypatch):
    """A second snapshot for the same id while the first is UNSEALED leaves the
    first snapshot's files unchanged — this is the double Re-fetch case."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    os.remove(os.path.join(p["GRID_FOLDER"], "12345p.png"))  # folder emptied by Re-fetch

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert entry["files"] == ["12345p.png"]


def test_restore_sealed_snapshot_is_superseded(tmp_path, monkeypatch):
    """A SEALED entry is superseded, not kept: Re-fetch → fetch → keep →
    Re-fetch → fetch → Undo must restore the art the operator chose to keep."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "seal_restore_point"), "restore.seal_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    restore.seal_restore_point()
    os.remove(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.webp"))

    restore.snapshot_for_restore(12345, pending=True)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert entry["files"] == ["12345p.webp"]
    # The superseded snapshot's copies are gone from the store.
    assert not os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "12345p.png"))
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "12345", "12345p.webp"))


def test_restore_snapshot_never_matches_a_neighbouring_game(tmp_path, monkeypatch):
    """Leading-digit-run matching: uid 12345 must not capture 123456p.png."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    _touch(os.path.join(p["GRID_FOLDER"], "123456p.png"))

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert entry["files"] == ["12345p.png"]


def test_restore_snapshot_records_icon_from_shortcuts_vdf(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _write_shortcuts(p["SHORTCUTS_PATH"], {"0": {"appid": 12345, "icon": "/a/b.png"}})

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert entry["icon"] == "/a/b.png"


def test_restore_snapshot_records_entry_for_game_without_art(tmp_path, monkeypatch):
    """The common first-fetch case: no art, no icon — an entry must still be
    recorded, or Undo silently stops working for new games."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "has_restore_point"), "restore.has_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)

    restore.snapshot_for_restore(12345)

    entry = _read_manifest(p["RESTORE_FOLDER"])["entries"]["12345"]
    assert entry["files"] == []
    assert restore.has_restore_point() is True


def test_clear_game_art_never_touches_a_prefix_neighbour(tmp_path, monkeypatch):
    """B1: clearing game 12345 must not delete game 123456's art (the old
    Search → Select glob(f"{id}*") did), and every 12345 file it deletes must
    first be copied into the restore point so Undo can bring it back."""
    p = _restore_setup(tmp_path, monkeypatch)
    mine = ["12345p.png", "12345.jpg", "12345_hero.png", "12345_logo.png", "12345_icon.ico"]
    neighbour = ["123456p.png", "123456.jpg", "123456_hero.png", "123456_icon.png"]
    for f in mine + neighbour:
        with open(os.path.join(p["GRID_FOLDER"], f), "wb") as fh:
            fh.write(f.encode())

    removed = restore.clear_game_art(12345)

    left = sorted(os.listdir(p["GRID_FOLDER"]))
    assert left == sorted(neighbour), "every 123456 file must survive, every 12345 file must go"
    assert removed == len(mine)
    m = _read_manifest(p["RESTORE_FOLDER"])
    assert sorted(m["entries"]["12345"]["files"]) == sorted(mine)
    assert m["entries"]["12345"]["pending"] is True
    assert "123456" not in m["entries"]
    for f in mine:
        with open(os.path.join(p["RESTORE_FOLDER"], "12345", f), "rb") as fh:
            assert fh.read() == f.encode(), "the restore copy must be the pre-delete bytes"


def test_restore_snapshot_wipes_store_on_grid_folder_change(tmp_path, monkeypatch):
    """A manifest whose grid_folder differs from the live GRID_FOLDER (account
    switch) wipes the store FIRST, before the first-wins check."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)  # unsealed entry under the first account
    other_grid = tmp_path / "grid2"
    other_grid.mkdir()
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(other_grid))
    _touch(os.path.join(str(other_grid), "12345p.webp"))

    restore.snapshot_for_restore(12345)

    manifest = _read_manifest(p["RESTORE_FOLDER"])
    assert manifest["grid_folder"] == str(other_grid)
    # Without the wipe, first-wins would have kept the OLD unsealed entry.
    assert manifest["entries"]["12345"]["files"] == ["12345p.webp"]


def test_restore_manifest_written_atomically(tmp_path, monkeypatch):
    """The manifest write must be temp-file + os.replace, proved behaviourally
    (not by grepping for os.replace in the source)."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    replaced = []
    real_replace = os.replace

    def recorder(src, dst):
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recorder)

    restore.snapshot_for_restore(12345)

    manifest_path = os.path.join(p["RESTORE_FOLDER"], "manifest.json")
    assert replaced, "snapshot_for_restore must write the manifest via os.replace"
    src, dst = replaced[0]
    assert dst == manifest_path
    assert src != manifest_path  # a temp path, not a direct write


def test_restore_begin_run_drops_sealed_keeps_unsealed_clears_pending(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "seal_restore_point"), "restore.seal_restore_point does not exist yet"
    assert hasattr(restore, "begin_restore_run"), "restore.begin_restore_run does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "11111p.png"))
    restore.snapshot_for_restore(11111)
    _touch(os.path.join(p["GRID_FOLDER"], "22222p.png"))
    restore.snapshot_for_restore(22222)
    restore.seal_restore_point()          # both sealed
    _touch(os.path.join(p["GRID_FOLDER"], "11111p.webp"))
    restore.snapshot_for_restore(11111, pending=True)  # Re-fetch: supersedes, becomes unsealed

    restore.begin_restore_run()

    manifest = _read_manifest(p["RESTORE_FOLDER"])
    assert "22222" not in manifest["entries"]          # sealed → dropped
    assert "11111" in manifest["entries"]              # unsealed → kept
    assert manifest["entries"]["11111"]["pending"] is False  # adopted into the run


def test_restore_begin_run_deletes_dropped_copies_from_disk(tmp_path, monkeypatch):
    """begin_restore_run deletes the dropped entries' copies from the store, not
    just their manifest rows — otherwise the store leaks like ~/.steamart_backup."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "seal_restore_point"), "restore.seal_restore_point does not exist yet"
    assert hasattr(restore, "begin_restore_run"), "restore.begin_restore_run does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "11111p.png"))
    restore.snapshot_for_restore(11111)
    _touch(os.path.join(p["GRID_FOLDER"], "22222p.png"))
    restore.snapshot_for_restore(22222)
    restore.seal_restore_point()
    restore.snapshot_for_restore(11111, pending=True)
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "22222"))  # copies exist pre-run

    restore.begin_restore_run()

    assert not os.path.exists(os.path.join(p["RESTORE_FOLDER"], "22222"))


def test_restore_seal_marks_every_entry_sealed(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "seal_restore_point"), "restore.seal_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    restore.snapshot_for_restore(11111)
    restore.snapshot_for_restore(22222)

    restore.seal_restore_point()

    manifest = _read_manifest(p["RESTORE_FOLDER"])
    assert len(manifest["entries"]) == 2
    assert all(e["sealed"] is True for e in manifest["entries"].values())


def test_restore_has_restore_point_states(tmp_path, monkeypatch):
    """False with no manifest, true with one entry, false when the manifest's
    grid_folder differs from the live GRID_FOLDER (account switch)."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "has_restore_point"), "restore.has_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    assert restore.has_restore_point() is False          # no manifest
    restore.snapshot_for_restore(12345)
    assert restore.has_restore_point() is True           # one entry
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(tmp_path / "other_grid"))  # account switch
    assert restore.has_restore_point() is False          # grid-folder mismatch


def test_restore_undo_refuses_account_switch(tmp_path, monkeypatch):
    """A grid-folder mismatch must make undo_restore_point return a zero summary
    and write nothing into the live grid folder."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    os.remove(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    other_grid = tmp_path / "other_grid"
    other_grid.mkdir()
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(other_grid))

    summary = restore.undo_restore_point()

    assert isinstance(summary, dict)
    assert summary.get("games", 0) == 0
    assert summary.get("files", 0) == 0
    assert os.listdir(steam_env.GRID_FOLDER) == []          # nothing written across accounts
    assert os.path.exists(os.path.join(p["RESTORE_FOLDER"], "manifest.json"))  # store untouched


def test_restore_undo_restores_byte_identical_files(tmp_path, monkeypatch):
    """A game whose art was replaced ends up byte-identical to the snapshot."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    original = os.path.join(p["GRID_FOLDER"], "12345p.png")
    with open(original, "wb") as f:
        f.write(b"ORIGINAL-ART-BYTES")
    restore.snapshot_for_restore(12345)
    os.remove(original)
    with open(original, "wb") as f:  # the fetch wrote something new
        f.write(b"NEW-FETCHED-ART")
    appfiles.register_managed_file(original)

    restore.undo_restore_point()

    with open(original, "rb") as f:
        assert f.read() == b"ORIGINAL-ART-BYTES"
    # A successful undo wipes the store, which is what makes a second press a
    # no-op and greys the button (has_restore_point drives it). Untested, an
    # implementation that skips the wipe re-restores on every press.
    assert restore.has_restore_point() is False
    # ...and the restored file is registered again, so Clear All Artwork still
    # covers it.
    assert original in appfiles.load_managed_files()


def test_restore_undo_removes_fetched_file_with_different_extension(tmp_path, monkeypatch):
    """The results-window 'apply an alternative' case: 1234p.webp written over a
    snapshot of 1234p.png — after undo only the .png exists."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "1234p.png"))
    restore.snapshot_for_restore(1234)
    os.remove(os.path.join(p["GRID_FOLDER"], "1234p.png"))
    webp = os.path.join(p["GRID_FOLDER"], "1234p.webp")
    _touch(webp)
    appfiles.register_managed_file(webp)  # the fetch's file is managed

    restore.undo_restore_point()

    assert os.path.exists(os.path.join(p["GRID_FOLDER"], "1234p.png"))
    assert not os.path.exists(webp)


def test_restore_undo_never_deletes_an_unmanaged_current_file(tmp_path, monkeypatch):
    """Undo must never delete a current file this app did not write."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    os.remove(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    unmanaged = os.path.join(p["GRID_FOLDER"], "12345p.webp")
    _touch(unmanaged)  # written by something other than this app — NOT registered

    restore.undo_restore_point()

    assert os.path.exists(unmanaged)  # untouched
    assert os.path.exists(os.path.join(p["GRID_FOLDER"], "12345p.png"))  # restored


def test_restore_undo_never_deletes_a_neighbouring_games_file(tmp_path, monkeypatch):
    """123456p.png (a different game, even though managed) must survive undoing
    12345."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    os.remove(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    neighbour = os.path.join(p["GRID_FOLDER"], "123456p.png")
    _touch(neighbour)
    appfiles.register_managed_file(neighbour)

    restore.undo_restore_point()

    assert os.path.exists(neighbour)


def test_restore_undo_writes_icon_back_when_steam_closed(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _write_shortcuts(p["SHORTCUTS_PATH"], {"0": {"appid": 12345, "icon": "/old/icon.png"}})
    restore.snapshot_for_restore(12345)
    # The fetch then set a DIFFERENT icon. Without this the assertion below is
    # vacuous: the value would still be "/old/icon.png" simply because nothing
    # ever disturbed it, so an undo that skips icons entirely would pass.
    _write_shortcuts(p["SHORTCUTS_PATH"], {"0": {"appid": 12345, "icon": "/new/icon.png"}})
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: False)

    restore.undo_restore_point()

    out = _read_shortcuts(p["SHORTCUTS_PATH"])
    assert out["0"]["icon"] == "/old/icon.png"


def test_restore_undo_queues_icon_when_steam_running(tmp_path, monkeypatch):
    """Steam running → the icon restore goes through save_pending_icons, not a
    direct vdf write."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    _write_shortcuts(p["SHORTCUTS_PATH"], {"0": {"appid": 12345, "icon": "/old/icon.png"}})
    restore.snapshot_for_restore(12345)
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: True)

    restore.undo_restore_point()

    assert steam_files.load_pending_icons() == {12345: "/old/icon.png"}


def test_restore_queued_empty_icon_survives_apply(tmp_path, monkeypatch):
    """A 'no icon' restore queued while Steam was running must survive the queue:
    apply_pending_icons filters on os.path.exists(path) today, and
    os.path.exists('') is False (steam_files.apply_pending_icons), so the restore is silently
    dropped — this gate forces the one-line fix (path == '' or exists)."""
    p = _restore_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    _write_shortcuts(p["SHORTCUTS_PATH"], {"0": {"appid": 12345, "icon": "/a/current.png"}})
    steam_files.save_pending_icons({12345: ""})  # queued while Steam was running
    monkeypatch.setattr(steam_env, "is_steam_running", lambda: False)

    n = steam_files.apply_pending_icons()

    assert n == 1
    out = _read_shortcuts(p["SHORTCUTS_PATH"])
    assert out["0"]["icon"] == ""  # the icon really was cleared


def test_restore_undo_with_no_restore_point_is_a_noop(tmp_path, monkeypatch):
    assert hasattr(restore, "undo_restore_point"), "restore.undo_restore_point does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)

    summary = restore.undo_restore_point()  # must not raise

    assert isinstance(summary, dict)
    assert summary.get("games", 0) == 0
    assert summary.get("files", 0) == 0


def test_restore_clear_restore_point_empties_store_and_size_is_float(tmp_path, monkeypatch):
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    assert hasattr(restore, "clear_restore_point"), "restore.clear_restore_point does not exist yet"
    assert hasattr(restore, "restore_point_size"), "restore.restore_point_size does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)

    assert isinstance(restore.restore_point_size(), float)
    restore.clear_restore_point()

    assert not os.path.exists(os.path.join(p["RESTORE_FOLDER"], "manifest.json"))
    assert restore.restore_point_size() == 0.0


def test_restore_legacy_backup_size_reports_the_old_pile(tmp_path, monkeypatch):
    """The Settings Storage row shows the legacy ~/.steamart_backup pile as well
    as the restore point — clear_backup() has never had a caller, so that folder
    has grown on every fetch since the feature shipped. Without this gate the
    'Older backups: Y MB' half of the row cannot be built. shortcuts.vdf.bak is
    never deleted (the only pre-app shortcut-list copy), so it must NOT count
    toward the figure the clear button is expected to zero out."""
    assert hasattr(steam_files, "legacy_backup_size"), "steam_files.legacy_backup_size does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    os.makedirs(p["BACKUP_FOLDER"], exist_ok=True)
    assert steam_files.legacy_backup_size() == 0.0
    # shortcuts.vdf.bak is preserved forever (never cleared); it must not
    # inflate the reported size or the clear button could never zero the row.
    with open(os.path.join(p["BACKUP_FOLDER"], "shortcuts.vdf.bak"), "wb") as f:
        f.write(b"x" * (300 * 1024))
    assert steam_files.legacy_backup_size() == 0.0
    with open(os.path.join(p["BACKUP_FOLDER"], "12345p.png"), "wb") as f:
        f.write(b"x" * (300 * 1024))

    size = steam_files.legacy_backup_size()

    assert isinstance(size, float)
    assert size > 0.0, "a 300 KB backup file must register as a non-zero MB figure"


def test_restore_clear_managed_artwork_wipes_restore_point(tmp_path, monkeypatch):
    """Clear All Artwork must wipe the restore point, or a later Undo silently
    re-copies and re-registers everything the operator just cleared."""
    assert hasattr(restore, "snapshot_for_restore"), "restore.snapshot_for_restore does not exist yet"
    p = _restore_setup(tmp_path, monkeypatch)
    _touch(os.path.join(p["GRID_FOLDER"], "12345p.png"))
    restore.snapshot_for_restore(12345)
    manifest_path = os.path.join(p["RESTORE_FOLDER"], "manifest.json")
    assert os.path.exists(manifest_path)

    artwork.clear_managed_artwork()

    assert not os.path.exists(manifest_path)


# ---------------------------------------------------------------------------
# Official Steam store assets (steam_official_images / get_steam_platform_data)
# and Steam-first download_all_artwork wiring.
# ---------------------------------------------------------------------------

_STEAM_META_PORTAL2 = {
    "store_asset_mtime": 1745362999,
    "clienticon": "25a5a16b2423bf7487ac5340b5b0948cef48c5f8",
    "icon": "25a5a16b2423bf7487ac5340b5b0948cef48c5f8",
    "header_image_full": {"english": "header.jpg"},
    "library_capsule_full": {"image": {"english": "library_600x900.jpg"},
                             "image2x": {"english": "library_600x900_2x.jpg"}},
    "library_hero_full": {"image": {"english": "library_hero.jpg"}},
    "library_logo_full": {"image": {"english": "logo.png"}, "image2x": {"english": "logo_2x.png"}},
}


def test_steam_official_images_old_layout_urls():
    out = sgdb.steam_official_images("620", _STEAM_META_PORTAL2)
    store = sgdb.STEAM_STORE_CDN
    community = sgdb.STEAM_COMMUNITY_CDN
    t = "?t=1745362999"

    assert out["grids"]["url"] == store + "/620/library_600x900_2x.jpg" + t
    assert out["grids_wide"]["url"] == store + "/620/header.jpg" + t
    assert out["heroes"]["url"] == store + "/620/library_hero.jpg" + t
    assert out["logos"]["url"] == store + "/620/logo_2x.png" + t
    assert out["icons"]["url"] == community + "/620/25a5a16b2423bf7487ac5340b5b0948cef48c5f8.ico"

    for slot, record in out.items():
        assert record["steam_official"] is True, slot
        assert record["style"] == "official", slot
        assert record["nsfw"] is False, slot
        assert record["humor"] is False, slot
        assert artwork.is_animated_image(record) is False, slot


def test_steam_official_images_fallbacks():
    meta = {
        "icon": "abc123",
        "header_image_full": {"english": "header.jpg"},
        "library_capsule_full": {"image": {"english": "library_600x900.jpg"}},
        "library_hero_full": {"image2x": {"english": "library_hero_2x.jpg"}},
        "library_logo_full": {"image": {"english": "logo.png"}},
    }
    out = sgdb.steam_official_images("7", meta)
    store = sgdb.STEAM_STORE_CDN
    community = sgdb.STEAM_COMMUNITY_CDN

    assert out["grids"]["url"] == store + "/7/library_600x900.jpg"
    assert out["grids_wide"]["url"] == store + "/7/header.jpg"
    assert out["heroes"]["url"] == store + "/7/library_hero_2x.jpg"
    assert out["logos"]["url"] == store + "/7/logo.png"
    assert out["icons"]["url"] == community + "/7/abc123.jpg"
    assert out["icons"]["url"].endswith(".jpg")

    for record in out.values():
        assert "?t=" not in record["url"]

    assert sgdb.steam_official_images("7", {}) == {}


def test_steam_official_images_hashed_layout_and_language():
    hashed = "2ba105370a00877459144db0dd68af5f2d338429/library_capsule_2x.jpg"
    meta = {
        "library_capsule_full": {"image2x": {"english": hashed}},
        "library_hero_full": {"image": {"german": "hero_de.jpg"}},
        "library_logo_full": {"image2x": {"english": "logo_en.png", "german": "logo_de.png"}},
    }
    out = sgdb.steam_official_images("9", meta)
    store = sgdb.STEAM_STORE_CDN

    assert out["grids"]["url"] == store + "/9/" + hashed
    assert out["heroes"]["url"] == store + "/9/hero_de.jpg"
    assert out["logos"]["url"] == store + "/9/logo_en.png"


def test_get_steam_platform_data_parses_link(monkeypatch):
    recorded = {}

    def fake_sgdb_get(url, headers=None, params=None, timeout=None, retries=3):
        recorded["url"] = url
        recorded["params"] = params
        payload = {"success": True, "data": {
            "id": 17830,
            "external_platform_data": {"steam": [{"id": "620", "metadata": _STEAM_META_PORTAL2}]},
        }}
        return types.SimpleNamespace(json=lambda p=payload: p)

    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get)
    monkeypatch.setattr(appfiles, "load_api_key", lambda *a, **k: "K")

    assert sgdb.get_steam_platform_data(17830) == ("620", _STEAM_META_PORTAL2)
    assert recorded["url"].endswith("/games/id/17830")
    assert recorded["params"] == {"platformdata": "steam"}


def test_get_steam_platform_data_none_on_missing_or_error(monkeypatch):
    monkeypatch.setattr(appfiles, "load_api_key", lambda *a, **k: "K")

    def fake_sgdb_get_no_platform(url, headers=None, params=None, timeout=None, retries=3):
        payload = {"success": True, "data": {"id": 17830}}
        return types.SimpleNamespace(json=lambda p=payload: p)

    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get_no_platform)
    assert sgdb.get_steam_platform_data(17830) is None

    def fake_sgdb_get_empty_steam(url, headers=None, params=None, timeout=None, retries=3):
        payload = {"success": True, "data": {"external_platform_data": {"steam": []}}}
        return types.SimpleNamespace(json=lambda p=payload: p)

    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get_empty_steam)
    assert sgdb.get_steam_platform_data(17830) is None

    def fake_sgdb_get_raises(url, headers=None, params=None, timeout=None, retries=3):
        raise RuntimeError("boom")

    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get_raises)
    assert sgdb.get_steam_platform_data(17830) is None

    def fake_sgdb_get_bad_json(url, headers=None, params=None, timeout=None, retries=3):
        def bad_json():
            raise ValueError("no json")
        return types.SimpleNamespace(json=bad_json)

    monkeypatch.setattr(sgdb, "_sgdb_get", fake_sgdb_get_bad_json)
    assert sgdb.get_steam_platform_data(17830) is None


def _five_static(prefix):
    return [_static(f"https://sgdb/{prefix}{i}.png") for i in range(5)]


def _steam_link_patch(monkeypatch):
    monkeypatch.setattr(sgdb, "get_steam_platform_data",
                        lambda sgdb_id, m=_STEAM_META_PORTAL2: ("620", m))


def test_download_all_artwork_steam_first_and_applied(tmp_path, monkeypatch):
    slot_assets = {
        "grids":  _five_static("g"),
        "heroes": _five_static("h"),
        "logos":  _five_static("l"),
        "icons":  _five_static("i"),
    }
    _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)
    _steam_link_patch(monkeypatch)
    steam_images = sgdb.steam_official_images("620", _STEAM_META_PORTAL2)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    for slot in ("grids", "grids_wide", "heroes", "logos", "icons"):
        sd = results.slots[slot]
        assert sd["option_urls"][0] == steam_images[slot]["url"], slot
        assert len(sd["option_urls"]) == 5, slot
        assert sd["applied_index"] == 0, slot
        assert sd["option_meta"][0]["steam"] is True, slot
        assert all(m["steam"] is False for m in sd["option_meta"][1:]), slot

    assert results.slots["grids"]["applied_path"].endswith("12345p.jpg")
    icon_uid, icon_path = results.icon_write
    assert icon_uid == 12345
    assert icon_path.endswith("12345_icon.ico")


def test_download_all_artwork_steam_fills_empty_slot(tmp_path, monkeypatch):
    _install_fetch_mocks(monkeypatch, tmp_path, {})
    _steam_link_patch(monkeypatch)
    steam_images = sgdb.steam_official_images("620", _STEAM_META_PORTAL2)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    for slot in ("grids", "grids_wide", "heroes", "logos", "icons"):
        sd = results.slots[slot]
        assert isinstance(sd, dict), slot
        assert sd["option_urls"] == [steam_images[slot]["url"]], slot
        assert sd["applied_index"] == 0, slot


def test_download_all_artwork_steam_excluded_by_official_never(tmp_path, monkeypatch):
    slot_assets = {
        "grids":  _five_static("g"),
        "heroes": _five_static("h"),
        "logos":  _five_static("l"),
        "icons":  _five_static("i"),
    }
    _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)
    _steam_link_patch(monkeypatch)
    steam_images = sgdb.steam_official_images("620", _STEAM_META_PORTAL2)

    prefs = {"logo_official": "never", "icon_official": "never", "grid_no_logo": "never"}
    results = artwork.download_all_artwork(42, 12345, prefs=prefs, defer_icon=True)

    for slot in ("logos", "icons"):
        sd = results.slots[slot]
        assert steam_images[slot]["url"] not in sd["option_urls"], slot
        assert sd["option_meta"][0]["steam"] is False, slot

    for slot in ("grids", "grids_wide", "heroes"):
        sd = results.slots[slot]
        assert sd["option_urls"][0] == steam_images[slot]["url"], slot


def test_download_all_artwork_no_steam_link_unchanged(tmp_path, monkeypatch):
    slot_assets = {
        "grids":  _five_static("g"),
        "heroes": _five_static("h"),
        "logos":  _five_static("l"),
        "icons":  _five_static("i"),
    }
    _install_fetch_mocks(monkeypatch, tmp_path, slot_assets)
    monkeypatch.setattr(sgdb, "get_steam_platform_data", lambda sgdb_id: None)

    results = artwork.download_all_artwork(42, 12345, prefs={}, defer_icon=True)

    expected = {"grids": slot_assets["grids"], "grids_wide": slot_assets["grids"],
                "heroes": slot_assets["heroes"], "logos": slot_assets["logos"],
                "icons": slot_assets["icons"]}
    for slot, assets in expected.items():
        sd = results.slots[slot]
        assert sd["option_urls"] == [a["url"] for a in assets[:5]], slot
        assert all(m["steam"] is False for m in sd["option_meta"]), slot


def _find_game(games, app_id):
    for g in games:
        if g["app_id"] == app_id:
            return g
    return None


def test_ignored_game_lands_in_hidden(tmp_path, monkeypatch):
    skip_file = tmp_path / "skip"
    grid = tmp_path / "grid"
    os.makedirs(grid, exist_ok=True)
    sc = tmp_path / "shortcuts.vdf"
    _write_shortcuts(str(sc), {
        "0": {"appid": 100, "AppName": "Game One"},
        "1": {"appid": 200, "AppName": "Game Two"},
    })
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(skip_file))
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))

    before = steam_files.get_non_steam_games()
    assert _find_game(before, 100)["skipped"] is False
    assert _find_game(before, 200)["skipped"] is False

    appfiles.add_to_skip_list(100)

    after = steam_files.get_non_steam_games()
    assert _find_game(after, 100)["skipped"] is True
    assert _find_game(after, 100)["has_art"] is True
    assert _find_game(after, 200)["skipped"] is False
    assert _find_game(after, 200)["has_art"] is False

    # Restore button: drop 100's line and re-read.
    with open(skip_file, "w", encoding="utf-8") as f:
        f.write("")

    restored = steam_files.get_non_steam_games()
    assert _find_game(restored, 100)["skipped"] is False


# ---------------------------------------------------------------------------
# download_artwork — the compressed-response length check (T6)
# ---------------------------------------------------------------------------

def test_download_artwork_gzip_length_mismatch_still_saves(tmp_path, monkeypatch):
    """A gzipped response states the COMPRESSED length while iter_content yields
    decoded bytes, so a byte-count mismatch on an encoded body must not fail the
    download — the decoded bytes are still written and swapped into place."""
    def fake_http_get(url, **kwargs):
        return _FakeStreamResp(200, [b"abcd", b"efgh"],
                               {"Content-Length": "3", "Content-Encoding": "gzip"})

    monkeypatch.setattr(sgdb, "http_get", fake_http_get)

    save_path = tmp_path / "a.ico"
    assert artwork.download_artwork("https://x/icon.ico", str(save_path), register=False) is True

    with open(save_path, "rb") as f:
        assert f.read() == b"abcdefgh"
    assert not (tmp_path / "a.ico.part").exists()


def test_download_artwork_unencoded_length_mismatch_fails(tmp_path, monkeypatch):
    """An unencoded body whose byte count disagrees with Content-Length is a
    truncated download: the .part is removed and the call returns False."""
    def fake_http_get(url, **kwargs):
        return _FakeStreamResp(200, [b"abc"], {"Content-Length": "10"})

    monkeypatch.setattr(sgdb, "http_get", fake_http_get)

    save_path = tmp_path / "b.png"
    assert artwork.download_artwork("https://x/b.png", str(save_path), register=False) is False
    assert not save_path.exists()
    assert not (tmp_path / "b.png.part").exists()


def test_download_artwork_identity_encoding_length_mismatch_fails(tmp_path, monkeypatch):
    """Content-Encoding: identity is an unencoded body, so the length check still
    applies: a mismatch fails the download and leaves no file behind."""
    def fake_http_get(url, **kwargs):
        return _FakeStreamResp(200, [b"abc"],
                               {"Content-Length": "10", "Content-Encoding": "identity"})

    monkeypatch.setattr(sgdb, "http_get", fake_http_get)

    save_path = tmp_path / "c.png"
    assert artwork.download_artwork("https://x/c.png", str(save_path), register=False) is False
    assert not save_path.exists()
    assert not (tmp_path / "c.png.part").exists()


# ---------------------------------------------------------------------------
# install_option / apply_icon — the results window's "Apply this one" (C4)
# ---------------------------------------------------------------------------

def _install_option_env(monkeypatch, installed=True, steam_running=False):
    rec = {"installed": [], "written": [], "queued": []}

    def fake_install(save_path, url, existing_files=None, animated=False,
                     progress_cb=None, status_cb=None):
        rec["installed"].append((save_path, animated))
        return installed
    monkeypatch.setattr(artwork, "install_into_slot", fake_install)
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: steam_running)
    monkeypatch.setattr(steam_files, "set_shortcut_icon",
                        lambda uid, path: rec["written"].append((uid, path)) or True)
    monkeypatch.setattr(steam_files, "save_pending_icons", lambda m: rec["queued"].append(dict(m)))
    return rec


def test_install_option_animated_icon_with_steam_closed_writes_the_vdf(monkeypatch):
    """C4: an animated icon pick is placed AND written to shortcuts.vdf."""
    rec = _install_option_env(monkeypatch, steam_running=False)
    ok, queued = artwork.install_option("/g/7_icon.png", "https://x/i.png", 7,
                                   is_icon=True, animated=True)
    assert (ok, queued) == (True, False)
    assert rec["installed"] == [("/g/7_icon.png", True)]
    assert rec["written"] == [(7, "/g/7_icon.png")]
    assert rec["queued"] == []


def test_install_option_animated_icon_with_steam_running_queues(monkeypatch):
    rec = _install_option_env(monkeypatch, steam_running=True)
    ok, queued = artwork.install_option("/g/7_icon.png", "https://x/i.png", 7,
                                   is_icon=True, animated=True)
    assert (ok, queued) == (True, True)
    assert rec["written"] == []
    assert rec["queued"] == [{7: "/g/7_icon.png"}]


def test_install_option_failed_install_writes_no_icon(monkeypatch):
    rec = _install_option_env(monkeypatch, installed=False)
    assert artwork.install_option("/g/7_icon.png", "u", 7, is_icon=True) == (False, False)
    assert rec["written"] == [] and rec["queued"] == []


def test_install_option_non_icon_slot_writes_no_icon(monkeypatch):
    rec = _install_option_env(monkeypatch)
    assert artwork.install_option("/g/7p.png", "u", 7, animated=True) == (True, False)
    assert rec["written"] == [] and rec["queued"] == []


# ---------------------------------------------------------------------------
# SGDB id overrides — ~/.steamart_ids (T5 / B8)
# ---------------------------------------------------------------------------

def test_id_overrides_round_trip_and_update(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "IDS_FILE", str(tmp_path / "ids"))
    assert appfiles.load_id_overrides() == {}
    appfiles.save_id_override(12345, 2016)
    appfiles.save_id_override(777, 5)
    appfiles.save_id_override(12345, 1993)
    assert appfiles.load_id_overrides() == {"12345": 1993, "777": 5}
    assert (tmp_path / "ids").read_text(encoding="utf-8") == "12345|1993\n777|5\n"


def test_id_overrides_skip_malformed_lines(tmp_path, monkeypatch):
    p = tmp_path / "ids"
    p.write_text("12345|1993\njunk\n55|DOOM\n|9\n 66 | 7 \n", encoding="utf-8")
    monkeypatch.setattr(appfiles, "IDS_FILE", str(p))
    assert appfiles.load_id_overrides() == {"12345": 1993, "66": 7}


def test_id_override_lives_apart_from_the_names_file(tmp_path, monkeypatch):
    """An older build reads ~/.steamart_names with split("|", 1); the id must
    never land there, or that build would search for "1993" as the name."""
    monkeypatch.setattr(appfiles, "IDS_FILE", str(tmp_path / "ids"))
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    appfiles.save_id_override(12345, 1993)
    assert not (tmp_path / "names").exists()


# ---------------------------------------------------------------------------
# appfiles — one registry, atomic writes for the app's own files (B15, H2)
# ---------------------------------------------------------------------------

def test_write_text_atomic_replaces_whole_file_and_leaves_no_temp(tmp_path):
    p = tmp_path / "f"
    p.write_text("old contents, longer than the new ones", encoding="utf-8")
    os.chmod(p, 0o640)
    appfiles.write_text_atomic(str(p), "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert os.listdir(tmp_path) == ["f"], "no temp file may be left behind"
    if os.name == "posix":   # Windows has no Unix mode bits to keep
        assert (os.stat(p).st_mode & 0o777) == 0o640, "an existing file keeps its mode"


def test_write_text_atomic_failure_keeps_the_old_file(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(appfiles.os, "replace", boom)
    with pytest.raises(OSError):
        appfiles.write_text_atomic(str(p), "new")
    assert p.read_text(encoding="utf-8") == "old"
    assert os.listdir(tmp_path) == ["f"]


def test_every_app_file_writer_goes_through_the_atomic_write(tmp_path, monkeypatch):
    """B15: the skip list, overrides, prefs, API key, theme, accent and
    first-run files were opened with "w" in place; a crash mid-write truncated
    them. Each writer must hand its whole content to write_text_atomic."""
    for name in ("SKIP_FILE", "NAMES_FILE", "IDS_FILE", "PREFS_FILE", "APIKEY_FILE",
                 "THEME_FILE", "ACCENT_FILE"):
        monkeypatch.setattr(appfiles, name, str(tmp_path / name))
    written = []
    real = appfiles.write_text_atomic
    monkeypatch.setattr(appfiles, "write_text_atomic",
                        lambda path, text: written.append(os.path.basename(path)) or real(path, text))
    import theming
    appfiles.add_to_skip_list(5)
    appfiles.remove_from_skip_list(5)
    appfiles.save_name_override(5, "N")
    appfiles.save_id_override(5, 9)
    appfiles.save_prefs({"animated": "ok"})
    appfiles.save_api_key("k")
    theming.save_theme("light")
    theming.save_accent("green")
    assert written == ["SKIP_FILE", "SKIP_FILE", "NAMES_FILE", "IDS_FILE", "PREFS_FILE",
                       "APIKEY_FILE", "THEME_FILE", "ACCENT_FILE"]
    assert theming.load_theme() == "light" and theming.load_accent() == "green"
    assert appfiles.load_api_key() == "k"


def test_add_to_skip_list_keeps_existing_lines(tmp_path, monkeypatch):
    p = tmp_path / "skip"
    p.write_text("1\n2", encoding="utf-8")   # no trailing newline
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(p))
    appfiles.add_to_skip_list(3)
    assert p.read_text(encoding="utf-8") == "1\n2\n3\n"


def test_pending_icons_and_account_file_go_through_the_atomic_write(tmp_path, monkeypatch):
    """B15: the pending-icon queue matters most — a truncated file loads as {}
    and the queued icons are silently dropped. It and the account file are
    written through write_text_atomic."""
    userdata = tmp_path / "userdata"
    (userdata / "111" / "config").mkdir(parents=True)
    (userdata / "111" / "config" / "shortcuts.vdf").write_bytes(b"")
    monkeypatch.setattr(steam_env, "get_steam_path", lambda: str(userdata))
    for name in ("STEAM_USERDATA", "STEAM_USER_ID", "SHORTCUTS_PATH", "GRID_FOLDER"):
        monkeypatch.setattr(steam_env, name, getattr(steam_env, name))
    monkeypatch.setattr(appfiles, "ACCOUNT_FILE", str(tmp_path / "account"))
    monkeypatch.setattr(appfiles, "PENDING_ICONS_FILE", str(tmp_path / "pending"))
    written = []
    real = appfiles.write_text_atomic
    monkeypatch.setattr(appfiles, "write_text_atomic",
                        lambda path, text: written.append(os.path.basename(path)) or real(path, text))

    assert steam_env.set_active_user("111")
    steam_files.save_pending_icons({5: "/i.png"})

    assert written == ["account", "pending_111"]
    assert steam_files.load_pending_icons() == {5: "/i.png"}



# ---------------------------------------------------------------------------
# T9 — the managed registry is pruned (B13); cleanups finish and report (B14)
# ---------------------------------------------------------------------------

def test_prune_managed_files_drops_duplicates_and_gone_files_only(tmp_path, monkeypatch):
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "1p.png").write_bytes(b"x")
    (grid / "2p.png").write_bytes(b"x")
    unmounted = tmp_path / "sdcard" / "grid" / "9p.png"          # folder missing
    lines = [grid / "1p.png", grid / "1p.png", grid / "gone.png", grid / "2p.png",
             unmounted, grid / "1p.png"]
    managed = tmp_path / "managed"
    managed.write_text("".join(f"{p}\n" for p in lines), encoding="utf-8")
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(managed))

    assert appfiles.prune_managed_files() == 3
    assert managed.read_text(encoding="utf-8").splitlines() == [
        str(grid / "1p.png"), str(grid / "2p.png"), str(unmounted)]
    assert sorted(os.listdir(grid)) == ["1p.png", "2p.png"], "pruning never deletes a file"


def _failing_remove(monkeypatch, fail_names):
    real = os.remove

    def remove(p):
        if os.path.basename(p) in fail_names:
            raise PermissionError(13, "in use", p)
        real(p)
    monkeypatch.setattr(os, "remove", remove)


def test_clear_managed_artwork_goes_past_a_locked_file_and_keeps_it_registered(tmp_path, monkeypatch):
    grid = tmp_path / "grid"
    grid.mkdir()
    paths = [grid / f for f in ("1p.png", "2p.png", "3p.png")]
    for p in paths:
        p.write_bytes(b"x")
    managed = tmp_path / "managed"
    managed.write_text("".join(f"{p}\n" for p in paths), encoding="utf-8")
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(managed))
    monkeypatch.setattr(appfiles, "RESTORE_FOLDER", str(tmp_path / "restore"))
    (tmp_path / "restore").mkdir()
    (tmp_path / "restore" / "manifest.json").write_text("{}", encoding="utf-8")
    _failing_remove(monkeypatch, {"2p.png"})

    assert artwork.clear_managed_artwork() == (2, 1)

    assert os.listdir(grid) == ["2p.png"], "the loop goes on past the locked file"
    assert managed.read_text(encoding="utf-8").splitlines() == [str(grid / "2p.png")], (
        "the file that could not be removed stays registered for the next Clear")
    assert not (tmp_path / "restore").exists(), "the restore point is cleared regardless"


def test_cache_and_backup_cleanups_finish_the_list_and_count_failures(tmp_path, monkeypatch):
    cache, backup = tmp_path / "cache", tmp_path / "backup"
    cache.mkdir(); backup.mkdir()
    for f in ("a.png", "b.png", "c.png"):
        (cache / f).write_bytes(b"x")
    for f in ("old1.png", "old2.png", "shortcuts.vdf.111.bak"):
        (backup / f).write_bytes(b"x")
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))
    monkeypatch.setattr(appfiles, "BACKUP_FOLDER", str(backup))
    _failing_remove(monkeypatch, {"b.png", "old1.png"})

    assert appfiles.clear_cache() == 1
    assert os.listdir(cache) == ["b.png"]
    assert steam_files.clear_backup() == 1
    assert sorted(os.listdir(backup)) == ["old1.png", "shortcuts.vdf.111.bak"]


def test_clean_old_cache_finishes_the_list_and_counts_failures(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    for f in ("a.png", "b.png", "new.png"):
        (cache / f).write_bytes(b"x")
    old = time.time() - 40 * 86400
    for f in ("a.png", "b.png"):
        os.utime(cache / f, (old, old))
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))
    _failing_remove(monkeypatch, {"a.png"})

    assert appfiles.clean_old_cache() == 1
    assert sorted(os.listdir(cache)) == ["a.png", "new.png"]


@pytest.mark.skipif(os.name != "posix", reason="Unix mode bits")
def test_write_text_atomic_new_file_gets_the_umask_mode_not_owner_only(tmp_path):
    """A new app file is created like a plain open() would (0o666 minus the
    umask), not with mkstemp's owner-only 0600 (operator ruling 2026-10-18)."""
    mask = os.umask(0)
    os.umask(mask)
    p = tmp_path / "new"
    appfiles.write_text_atomic(str(p), "x")
    assert (os.stat(p).st_mode & 0o777) == (0o666 & ~mask)
    assert os.listdir(tmp_path) == ["new"]


# ---------------------------------------------------------------------------
# One copy at a time — appfiles.acquire_instance_lock (ruling W8)
# ---------------------------------------------------------------------------

@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "LOCK_FILE", str(tmp_path / "lock"))
    yield tmp_path / "lock"
    appfiles.release_instance_lock()


_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import appfiles
appfiles.LOCK_FILE = sys.argv[2]
print("held" if appfiles.acquire_instance_lock(wait=0) else "refused", flush=True)
time.sleep(float(sys.argv[3]))
"""


def _holder(lock, seconds):
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, root, str(lock), str(seconds)],
                         stdout=subprocess.PIPE, text=True)
    return p, p.stdout.readline().strip()


def test_a_second_copy_is_refused_while_the_first_holds_the_lock(lock_file):
    p, first = _holder(lock_file, 30)
    try:
        assert first == "held"
        assert appfiles.acquire_instance_lock(wait=0) is False, "a second copy must be refused"
    finally:
        p.kill(); p.wait()


def test_the_lock_is_released_when_the_holder_crashes(lock_file):
    import signal
    p, first = _holder(lock_file, 30)
    assert first == "held"
    p.send_signal(signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    p.wait()
    assert appfiles.acquire_instance_lock(wait=0) is True, "a crashed copy must not block forever"


def test_a_relaunch_waits_for_the_old_copy_to_exit(lock_file):
    """Theme/accent relaunch: the new process may start while the old one is still
    exiting (Windows execve is spawn + exit). It retries instead of giving up."""
    p, first = _holder(lock_file, 0.8)
    assert first == "held"
    try:
        assert appfiles.acquire_instance_lock(wait=5) is True
    finally:
        p.wait()


def test_an_unusable_lock_file_never_blocks_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "LOCK_FILE", str(tmp_path / "missing-dir" / "lock"))
    assert appfiles.acquire_instance_lock(wait=0) is True


def test_full_reset_leaves_the_lock_file_alone(lock_file, tmp_path, monkeypatch):
    assert appfiles.acquire_instance_lock(wait=0)
    for name in ("APIKEY_FILE", "PREFS_FILE", "SKIP_FILE", "NAMES_FILE", "IDS_FILE",
                 "ACCOUNT_FILE", "FIRSTRUN_FILE", "THEME_FILE", "ACCENT_FILE", "GEOMETRY_FILE",
                 "PENDING_ICONS_FILE", "CACHE_FOLDER"):
        monkeypatch.setattr(appfiles, name, str(tmp_path / name))
    appfiles.full_reset()
    assert lock_file.exists(), "deleting the held lock file would let a second copy start"
