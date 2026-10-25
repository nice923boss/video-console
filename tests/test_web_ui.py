"""The page itself in headless Chromium (QA-03): the import-volume dialog, the reference picture field and
the audio meters, against a real App on a free port. Skipped when Playwright or its Chromium is missing
(pip install playwright, then python -m playwright install chromium)."""
from __future__ import annotations

import io
import json
import math
import time
import wave
from array import array
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from test_server import Client, needs_ffmpeg, png_bytes
from vc import release
from vc.app import App

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = needs_ffmpeg


def tone_wav(seconds: float, dbfs: float, hz: float = 1000.0, rate: int = 48000) -> bytes:
    """A mono sine whose peak sits at `dbfs`."""
    amp = 32767 * 10 ** (dbfs / 20)
    samples = array("h", (round(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(int(seconds * rate))))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def wait_for(read: Callable[[], Any], timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the page")
        time.sleep(0.1)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except sync_api.Error as err:
            pytest.skip(f"Playwright's Chromium is not installed: {err}")
        yield chromium
        chromium.close()


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setattr(release, "RELEASE_URL", "http://127.0.0.1:9/releases/latest")  # the page asks on start
    data = tmp_path / "data"
    data.mkdir()
    servers = [{"id": "local", "name": "x", "url": "http://127.0.0.1:9", "enabled": False}]
    (data / "settings.json").write_text(json.dumps({"comfy": {"servers": servers}}), encoding="utf-8")
    instance = App(data)
    instance.start(port=0, watchdog=False)
    yield instance
    instance.stop()


@pytest.fixture()
def client(app: App) -> Client:
    return Client(app.server.port)


@pytest.fixture()
def project(client: Client) -> dict:
    # creating it makes it the last project, so the page opens straight into it
    return client.json("POST", "/api/projects", {"name": "瀏覽器測試"})["project"]


@pytest.fixture()
def open_page(browser: Any, app: App) -> Iterator[Callable[[str], Any]]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    errors: list[str] = []

    def go(view: str) -> Any:
        page = context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"http://127.0.0.1:{app.server.port}/#{view}")
        page.wait_for_selector(".boot-screen", state="detached", timeout=15000)
        return page

    yield go
    context.close()
    assert not errors, errors


def project_of(client: Client, pid: str) -> dict:
    return client.json("GET", f"/api/projects/{pid}")["project"]


def sounds(client: Client, pid: str) -> list[dict]:
    return [m for m in project_of(client, pid)["media"].values() if m["kind"] == "audio"]


def upload(client: Client, pid: str, query: str, body: bytes) -> None:
    job = client.json("POST", f"/api/projects/{pid}/upload?{query}", body)["job"]
    assert client.wait_job(job["id"])["status"] == "done"


def test_import_volume_dialog_sets_the_sound_volume_and_remembers_the_choice(open_page, client, project):
    pid = project["id"]
    page = open_page("library")
    picker = page.locator("input[type=file][multiple]")
    wav = {"name": "line.wav", "mimeType": "audio/wav", "buffer": tone_wav(0.5, -12)}

    picker.set_input_files(wav)
    dialog = page.get_by_role("dialog", name="匯入音量")
    choices = dialog.locator(".gain-choices .btn")
    assert [t.split()[0] for t in choices.all_inner_texts()] == ["+6dB", "0dB", "-10dB"]
    assert "btn-primary" in choices.nth(1).get_attribute("class")
    choices.nth(0).click()
    assert wait_for(lambda: sounds(client, pid))[0]["gain"] == 1.9953

    # the last choice is offered first; canceling imports nothing
    picker.set_input_files(wav)
    assert "btn-primary" in choices.nth(0).get_attribute("class")
    dialog.get_by_role("button", name="取消").click()
    page.wait_for_timeout(800)
    assert len(sounds(client, pid)) == 1

    picker.set_input_files(wav)
    dialog.locator(".gain-custom input").fill("-3")
    dialog.get_by_role("button", name="套用").click()
    added = wait_for(lambda: [m for m in sounds(client, pid) if m["gain"] != 1.9953])
    assert added[0]["gain"] == 0.7079


def test_reference_field_picks_adds_reorders_and_removes_library_pictures(open_page, client, project):
    pid = project["id"]
    upload(client, pid, "name=red.png&target=library", png_bytes(rgb=(220, 40, 40)))
    upload(client, pid, "name=blue.png&target=library", png_bytes(rgb=(40, 40, 220)))
    ids = {m["name"]: m["id"] for m in project_of(client, pid)["media"].values()}
    red, blue = ids["red.png"], ids["blue.png"]
    sid = project["shots"][0]["id"]
    shot = lambda: next(s for s in project_of(client, pid)["shots"] if s["id"] == sid)  # noqa: E731

    page = open_page("board")
    page.get_by_role("tab", name="生成").click()
    page.locator(".ref-empty", has_text="選擇參考圖").click()
    picker = page.get_by_role("dialog", name="選擇參考圖")
    picker.locator(".ref-group", has_text="素材庫圖片").locator(".ref-cell", has_text="red.png").click()
    assert wait_for(lambda: shot().get("ref") == red)

    page.locator(".ref-extra-add").click()
    page.get_by_role("dialog", name="選擇參考圖").locator(".ref-cell", has_text="blue.png").click()
    assert wait_for(lambda: shot().get("extraRefs") == [blue])
    sync_api.expect(page.locator(".ref-extra")).to_have_count(2)  # the page catches up through the event stream

    # moving the second picture forward makes it the main reference
    page.locator(".ref-extra").nth(1).get_by_title("往前移").click()
    assert wait_for(lambda: shot().get("ref") == blue and shot().get("extraRefs") == [red])

    page.locator(".ref-extra").nth(1).get_by_title("移除這張參考圖").click()
    assert wait_for(lambda: shot().get("ref") == blue and not shot().get("extraRefs"))


def test_meters_read_the_mix_with_the_import_volume_applied(open_page, client, project):
    pid = project["id"]
    sid = project["shots"][0]["id"]
    upload(client, pid, f"name=frame.png&target=shot&shot={sid}", png_bytes())
    # a sine peaking at -12 dBFS, put on the track at +6 dB, should meter at -6 dB
    upload(client, pid, "name=tone.wav&track=1&start=0&gain=1.9953", tone_wav(4.5, -12))

    page = open_page("edit")
    meters = page.get_by_role("complementary", name="音量表")
    play = page.locator(".pl-play")
    play.click()
    page.wait_for_timeout(3800)
    play.click()

    peaks = meters.locator(".meters-peak").all_inner_texts()
    assert peaks == ["-6", "-6"], peaks
    short, integrated, true_peak = (float(t) for t in meters.locator(".meters-loud b").all_inner_texts())
    # a 1 kHz sine in both channels at -6 dBFS peak is -6 LUFS (EBU R128)
    assert abs(short + 6) <= 1.0 and abs(integrated + 6) <= 1.0, (short, integrated)
    assert -6.5 <= true_peak <= -5.0, true_peak
