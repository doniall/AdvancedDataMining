#!/usr/bin/env python3
"""
H_sensecraft_push.py

Pushes the latest Solis snapshot (solis_device.json, written by
G_solis_fetch.py) AND the latest IMAGE_PUSH_COUNT rendered greyscale radar
frames (1_GreyscalePNG/, written by B_ireland_radar_greyscale.py) to
SenseCraft's own cloud API, instead of relying on the E1003's
"External API Configuration" widget to pull either from somewhere we host.

SETUP
    In the SenseCraft HMI dashboard designer, add the widget and choose
    "Push to Sensecraft" instead of "External API Configuration". It gives
    you an api-key and a device_id -- set those as SENSECRAFT_API_KEY and
    SENSECRAFT_DEVICE_ID environment variables. Treat the API key like every
    other credential in this repo: don't commit it, don't paste it into
    chat. After the first successful push, use the dashboard's own
    "Test & Load Fields" button to pick which pushed fields each widget
    should display -- Solis numbers (power_kw, battery_pct,
    weather_condition, ...) as text/number fields, image_1..image_N as
    Image-type fields (Format Type "Image URL" -- see IMAGES below).

    SenseCraft's own `data` object is a flat key/value map (their own
    example: temperature/humidity/pressure) -- it isn't documented as
    supporting nested objects, so solis_device.json's nested "weather"
    dict is flattened to weather_* keys below rather than sent as-is.

IMAGES -- WHY THESE ARE URLS, NOT PUSHED BYTES
    Confirmed live in the dashboard: an Image-type field's only Format Type
    is "Image URL" -- there's no base64/inline-bytes option. So a pushed
    image_N field has to be an actual fetchable URL; push_data doesn't let
    us skip hosting for images the way it does for the plain Solis numbers.

    _publish_images_to_github() covers that: it commits the latest
    IMAGE_PUSH_COUNT greyscale frames under fixed filenames
    (frame_1.png..frame_N.png, newest first) to a dedicated branch
    (IMAGE_BRANCH) of this repo's own origin remote, via a throwaway git
    worktree -- never touches whatever branch is actually checked out for
    development. Fixed filenames mean the resulting
    raw.githubusercontent.com URLs never change, only what they point to --
    so image_N fields, once selected in the dashboard, keep working across
    every push. Requires this repo to be public (raw.githubusercontent.com
    serves private repos' files only with an auth token, which the
    SenseCraft-side fetch has no way to supply) and that the pipeline's own
    git push access covers this repo's origin remote.

    image_N_time is that frame's own UTC timestamp (parsed from its
    filename), for confirming freshness against image_N itself.

Run:   python3 H_sensecraft_push.py
Needs: nothing beyond the standard library, and `git` on PATH with push
       access to this repo's origin remote already configured.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

API_KEY = os.environ.get("SENSECRAFT_API_KEY", "")
DEVICE_ID = os.environ.get("SENSECRAFT_DEVICE_ID", "")

PUSH_URL = "https://sensecraft-hmi-api.seeed.cc/api/v1/user/device/push_data"
REQUEST_TIMEOUT_SECONDS = 15

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_PATH = os.path.join(HERE, "solis_device.json")   # written by G_solis_fetch.py
# same literal B_ireland_radar_greyscale.py uses for GreyscaleRadarImageSubfolder --
# duplicated here rather than imported, to avoid pulling in that whole module
# (PIL, coastline/counties loading) just for a folder name
GREYSCALE_DIR = os.path.join(HERE, "1_GreyscalePNG")
LAST_PUSHED_PATH = os.path.join(HERE, "sensecraft_last_pushed.json")

IMAGE_PUSH_COUNT = 20
GIT_REMOTE = "origin"
IMAGE_BRANCH = os.environ.get("SENSECRAFT_IMAGE_BRANCH", "sensecraft-images")


def _flatten(device_view):
    """solis_device.json is already flat except for its "weather" dict --
    pull that out to weather_* keys, and drop None values (a live SolisCloud
    field candidate that came back empty), since a null in SenseCraft's
    field selector isn't something their "Test & Load Fields" flow is known
    to handle."""
    flat = {}
    for key, value in device_view.items():
        if key == "weather" and isinstance(value, dict):
            for wkey, wvalue in value.items():
                if wvalue is not None:
                    flat[f"weather_{wkey}"] = wvalue
        elif value is not None:
            flat[key] = value
    return flat


def _latest_greyscale_filenames(n=IMAGE_PUSH_COUNT):
    """Filenames are UTC timestamp stems (see B_ireland_radar_greyscale.py's
    own "src" timestamp comment), so a plain lexicographic sort is
    chronological -- same assumption that script's own frame-diffing already
    relies on. Returns the n most recent, newest first."""
    if not os.path.isdir(GREYSCALE_DIR):
        return []
    names = sorted(f for f in os.listdir(GREYSCALE_DIR) if f.lower().endswith(".png"))
    return list(reversed(names[-n:]))


def _run(args, cwd, check=True):
    # GIT_TERMINAL_PROMPT=0 makes a git call that can't authenticate fail
    # immediately with a clear error, instead of hanging on an interactive
    # username/password prompt that (a) nothing is there to answer when this
    # runs unattended, and (b) can never succeed anyway -- GitHub has
    # rejected plain password auth for git operations for years; the prompt
    # was always going to fail once you did enter something. See this
    # repo's Azure/GitHub credential setup notes for how to make pushes
    # authenticate silently instead (a Personal Access Token stored via the
    # OS credential helper, or an SSH key).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check, env=env)


def _repo_root():
    return _run(["git", "rev-parse", "--show-toplevel"], cwd=HERE).stdout.strip()


def _origin_owner_repo():
    """Parses 'owner/repo' out of the origin remote URL, for building
    raw.githubusercontent.com URLs -- handles both the SSH
    (git@github.com:owner/repo.git) and HTTPS (https://github.com/owner/repo.git)
    remote forms."""
    url = _run(["git", "remote", "get-url", GIT_REMOTE], cwd=HERE).stdout.strip()
    if url.endswith(".git"):
        url = url[:-len(".git")]
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:"):]
    if "github.com/" in url:
        return url.split("github.com/", 1)[1]
    raise RuntimeError(f"origin remote {url!r} doesn't look like a GitHub URL")


def _publish_images_to_github(filenames):
    """Commits `filenames` (from GREYSCALE_DIR, newest first) to fixed
    names (frame_1.png..frame_N.png) on IMAGE_BRANCH of this repo's origin
    remote, and returns their raw.githubusercontent.com URLs in the same
    order -- or None on any failure (git not pushable from here, network,
    etc.), so main() can skip image fields for this run instead of crashing
    the whole push. See IMAGES docstring above for why this exists."""
    if not filenames:
        return []

    try:
        repo_root = _repo_root()
        owner_repo = _origin_owner_repo()
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"couldn't resolve this repo's git remote ({e}); skipping GitHub image publish")
        return None

    # http.postBuffer is a repo-level (not worktree-level) setting, so this
    # persists in the shared .git/config across every future run -- not just
    # this one -- and works the same on any machine this ends up running on
    # (this Mac, a Pi later) without needing a one-off manual `git config`
    # first. Git's own default (1 MiB) is comfortably too small for a first
    # push of IMAGE_PUSH_COUNT real images in one commit, and fails with an
    # opaque "unexpected disconnect while reading sideband packet" rather
    # than a clear size-limit error.
    _run(["git", "config", "http.postBuffer", "524288000"], cwd=repo_root, check=False)

    _run(["git", "fetch", GIT_REMOTE, IMAGE_BRANCH], cwd=repo_root, check=False)
    # ^ best-effort -- IMAGE_BRANCH may not exist on the remote yet (first run ever)

    tmp_dir = tempfile.mkdtemp(prefix="sensecraft_images_")
    try:
        existing = _run(["git", "worktree", "add", tmp_dir, IMAGE_BRANCH], cwd=repo_root, check=False)
        if existing.returncode != 0:
            # IMAGE_BRANCH doesn't exist locally or on the remote yet -- start it fresh,
            # as an orphan branch with no history and no files from whatever HEAD is
            _run(["git", "worktree", "add", "--detach", "--no-checkout", tmp_dir], cwd=repo_root)
            _run(["git", "checkout", "--orphan", IMAGE_BRANCH], cwd=tmp_dir)
            _run(["git", "reset"], cwd=tmp_dir)   # clear the index; the (already-empty) working tree is untouched

        for old in os.listdir(tmp_dir):
            if old.lower().endswith(".png"):
                os.remove(os.path.join(tmp_dir, old))
        for i, filename in enumerate(filenames, start=1):
            shutil.copyfile(os.path.join(GREYSCALE_DIR, filename), os.path.join(tmp_dir, f"frame_{i}.png"))

        _run(["git", "add", "-A"], cwd=tmp_dir)
        status = _run(["git", "status", "--porcelain"], cwd=tmp_dir)
        if status.stdout.strip():
            _run(["git", "-c", "user.email=solis-pipeline@localhost", "-c", "user.name=Solis Pipeline",
                  "commit", "-q", "-m", f"latest {len(filenames)} greyscale frame(s)"], cwd=tmp_dir)
            _run(["git", "push", "-q", GIT_REMOTE, f"HEAD:{IMAGE_BRANCH}"], cwd=tmp_dir)
        else:
            print(f"{IMAGE_BRANCH} already has this exact frame set -- nothing to commit")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        print(f"publishing images to GitHub failed ({e}); {stderr}")
        return None
    finally:
        _run(["git", "worktree", "remove", "--force", tmp_dir], cwd=repo_root, check=False)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _run(["git", "worktree", "prune"], cwd=repo_root, check=False)

    return [
        f"https://raw.githubusercontent.com/{owner_repo}/{IMAGE_BRANCH}/frame_{i}.png"
        for i in range(1, len(filenames) + 1)
    ]


def _image_fields(urls, filenames):
    fields = {}
    for i, (url, filename) in enumerate(zip(urls, filenames), start=1):
        fields[f"image_{i}"] = url
        fields[f"image_{i}_time"] = os.path.splitext(filename)[0]
    return fields


def _load_last_pushed():
    if not os.path.exists(LAST_PUSHED_PATH):
        return {}
    try:
        with open(LAST_PUSHED_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_pushed(fetched_at, image_filenames):
    with open(LAST_PUSHED_PATH, "w") as f:
        json.dump({"fetched_at": fetched_at, "image_filenames": image_filenames}, f)


def push(data):
    """POST one combined reading to SenseCraft. Raises on failure -- callers
    (main() here, or 0_Run_Radar_And_Greyscale.py) decide whether that's
    fatal to the overall run."""
    payload = {"device_id": int(DEVICE_ID), "data": data}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        PUSH_URL,
        data=body,
        headers={"api-key": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def main():
    if not (API_KEY and DEVICE_ID):
        print("SENSECRAFT_API_KEY / SENSECRAFT_DEVICE_ID not set -- skipping SenseCraft push "
              "(see the setup notes at the top of this file)")
        return

    device_view = {}
    if os.path.exists(DEVICE_PATH):
        with open(DEVICE_PATH) as f:
            device_view = json.load(f)
    else:
        print("solis_device.json not found -- pushing images only, if any "
              "(run G_solis_fetch.py for the Solis panel data)")

    image_filenames = _latest_greyscale_filenames()
    if not device_view and not image_filenames:
        print("nothing to push yet -- no solis_device.json and no images in "
              "1_GreyscalePNG/ (run G_solis_fetch.py and/or B_ireland_radar_greyscale.py first)")
        return

    fetched_at = device_view.get("fetched_at")
    last_pushed = _load_last_pushed()
    if (fetched_at and fetched_at == last_pushed.get("fetched_at")
            and image_filenames == last_pushed.get("image_filenames")):
        print("SenseCraft already has this data (nothing new since the last push) -- skipping")
        return

    image_urls = _publish_images_to_github(image_filenames) if image_filenames else []
    if image_urls is None:
        print("couldn't publish images to GitHub this run -- pushing Solis data without image fields")
        image_urls, image_filenames = [], []

    data = _flatten(device_view)
    data.update(_image_fields(image_urls, image_filenames))

    try:
        resp = push(data)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"SenseCraft push failed ({e})")
        return

    _save_last_pushed(fetched_at, image_filenames)
    print(f"pushed {len(data)} field(s) to SenseCraft (device {DEVICE_ID}), "
          f"including {len(image_filenames)} image(s): {resp}")


if __name__ == "__main__":
    main()
