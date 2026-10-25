"""ComfyUI servers and workflow templates, cloud providers, the text model and notifications."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from .. import providers
from ..comfy.export import comfy_json, comfy_zip, file_names
from ..providers.base import duration_seconds
from ..providers.catalog import UNUSABLE, model_status, same_line
from ..server import Request, Response, Router
from ..util import AppError

if TYPE_CHECKING:
    from ..app import App
    from ..providers import Provider
    from ..providers.catalog import ModelCatalog

log = logging.getLogger("vc.api")

SID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
PROVIDER_TEST_TIMEOUT = 30


def provider_secret_names() -> set[str]:
    return {f"provider.{p.secret_owner()}.{s['key']}" for p in providers.list_all() for s in p.secrets}


def provider_info(p: Provider, stored: dict | None, catalog: ModelCatalog) -> dict[str, Any]:
    owner = p.secret_owner()
    config = p.resolve_config(stored)
    info = {**p.describe(), "config": config,
            "secretNames": {s["key"]: f"provider.{owner}.{s['key']}" for s in p.secrets}}
    models = catalog.get(p.id, p.catalog_source(config)) if p.model_keys else None
    if p.kind == "video":
        # Seconds the shot's video length picker offers per mode; None: the shot length goes out as it is.
        lengths = {mode: p.durations(config, mode, models) for mode in p.modes}
        info["durations"] = {mode: None if v is None else [duration_seconds(x) for x in v] for mode, v in lengths.items()}
    return {**info, "models": models} if p.model_keys else info


def provider_list(app: App) -> list[dict]:
    stored = app.settings.section("providers") or {}
    return [provider_info(p, stored.get(p.id), app.model_catalog) for p in providers.list_all()]


def provider_config(app: App, p: Provider, given: Any) -> dict:
    """The settings form's unsaved draft when one is sent, else the saved settings."""
    if given is not None:
        return p.validate_config(given)
    return p.resolve_config((app.settings.section("providers") or {}).get(p.id))


def provider_context(app: App, p: Provider, config: dict) -> providers.Context:
    owner = p.secret_owner()
    return providers.Context(p.id, config, lambda key: app.secrets.get(f"provider.{owner}.{key}"),
                             timeout=PROVIDER_TEST_TIMEOUT)


def pin_newest(app: App, p: Provider, fields: dict) -> dict[str, str]:
    """Model fields the user never saved follow the suggested model when the built-in default is retired or
    unusable, or when the suggestion is a newer version at the same tier, so an untouched setup does not stay
    on an old default. A model the user saved is never changed."""
    stored = (app.settings.section("providers") or {}).get(p.id)
    stored = stored if isinstance(stored, dict) else {}
    config = p.resolve_config(stored)
    pinned = {}
    for key in dict.fromkeys(p.model_keys.values()):
        suggested = (fields.get(key) or {}).get("suggested")
        if key in stored or not suggested or suggested == config[key]:
            continue
        if model_status({"fields": fields}, key, config[key]) in UNUSABLE or same_line(config[key], suggested):
            pinned[key] = suggested
    if pinned:
        app.settings.update({"providers": {p.id: {**stored, **pinned}}})
    return pinned


def workflow_users(app: App, wid: str) -> list[str]:
    """Names of the settings that point at a template (routes and the upscale default)."""
    users = []
    for route in app.settings.section("routes"):
        for kind in ("image", "video"):
            sec = route.get(kind) or {}
            if sec.get("backend") == "comfy" and wid in (sec.get("workflow"), sec.get("workflowI2I")):
                users.append(f"路由「{route['name']}」")
                break
    if app.settings.section("comfy").get("upscaleWorkflow") == wid:
        users.append("預設放大範本")
    return users


def import_object_info(app: App, server: str) -> dict | None:
    """Node definitions for an import: the chosen server, else the first online one that answers."""
    sids = [server] if server else [s["id"] for s in app.comfy.status() if s.get("online")]
    for sid in sids:
        try:
            return app.comfy.object_info(sid)
        except AppError as exc:
            log.info("No node list from ComfyUI %s: %s", sid, exc.message)
    return None


def register(router: Router, app: App) -> None:
    comfy = app.comfy

    # ------------------------------------------------------------------ ComfyUI servers
    @router.route("GET", "/api/comfy")
    def comfy_status(req: Request) -> list[dict]:
        return comfy.status()

    @router.route("POST", "/api/comfy/check")
    def comfy_check(req: Request) -> list[dict]:
        return comfy.check_all()

    @router.route("POST", "/api/comfy/test")
    def comfy_test(req: Request) -> dict:
        data = req.json_object()
        token = str(data.get("token") or "").strip()
        sid = str(data.get("sid") or "")
        if not token and SID_RE.match(sid):
            token = comfy.token(sid)  # an edited address is tested with the token saved for that server
        return comfy.test_connection(str(data.get("url") or ""), token)

    @router.route("POST", "/api/comfy/{sid}/refresh")
    def comfy_refresh(req: Request) -> dict:
        return comfy.refresh(req.params["sid"])

    @router.route("GET", "/api/comfy/{sid}/models")
    def comfy_models(req: Request) -> dict:
        return comfy.models(req.params["sid"])

    @router.route("GET", "/api/comfy/{sid}/templates")
    def comfy_templates(req: Request) -> dict:
        templates = [app.workflows.get(w["id"]) for w in app.workflows.list()]
        return comfy.template_report(req.params["sid"], templates)

    @router.route("POST", "/api/comfy/{sid}/free")
    def comfy_free(req: Request) -> None:
        comfy.free(req.params["sid"])

    @router.route("POST", "/api/comfy/{sid}/interrupt")
    def comfy_interrupt(req: Request) -> None:
        comfy.interrupt(req.params["sid"])

    # ------------------------------------------------------------------ workflow templates
    @router.route("GET", "/api/workflows")
    def workflow_list(req: Request) -> list[dict]:
        return app.workflows.list()

    @router.route("POST", "/api/workflows/import")
    def workflow_import(req: Request) -> dict:
        data = req.json_object()
        info = import_object_info(app, str(data.get("server") or ""))
        tpl, warnings = app.workflows.import_data(data.get("data"), str(data.get("name") or ""), info)
        return {"workflow": tpl, "warnings": warnings}

    @router.route("GET", "/api/workflows/comfyui.zip")
    def workflow_comfy_zip(req: Request) -> Response:
        """Every template as API-format JSON for the user's own ComfyUI, plus a readme of the models each needs."""
        templates = [app.workflows.get(t["id"]) for t in app.workflows.list()]
        return Response(comfy_zip(templates), "application/zip", download_name="Video Console ComfyUI 工作流程.zip")

    @router.route("GET", "/api/workflows/{wid}")
    def workflow_get(req: Request) -> dict:
        return app.workflows.get(req.params["wid"])

    @router.route("GET", "/api/workflows/{wid}/comfyui.json")
    def workflow_comfy_json(req: Request) -> Response:
        tpl = app.workflows.get(req.params["wid"])
        return Response(comfy_json(tpl), "application/json; charset=utf-8",
                        download_name=file_names([tpl])[tpl["id"]])

    @router.route("PUT", "/api/workflows/{wid}")
    def workflow_save(req: Request) -> dict:
        return app.workflows.save({**req.json_object(), "id": req.params["wid"]})

    @router.route("DELETE", "/api/workflows/{wid}")
    def workflow_delete(req: Request) -> None:
        wid = req.params["wid"]
        users = workflow_users(app, wid)
        if users:
            raise AppError(f"{'、'.join(users)}正在使用這個範本，請先到「引擎」頁改用其他範本", 409)
        app.workflows.delete(wid)

    @router.route("POST", "/api/workflows/{wid}/duplicate")
    def workflow_duplicate(req: Request) -> dict:
        return app.workflows.duplicate(req.params["wid"])

    # ------------------------------------------------------------------ cloud providers
    @router.route("GET", "/api/providers")
    def provider_index(req: Request) -> list[dict]:
        return provider_list(app)

    @router.route("GET", "/api/costs")
    def cost_summary(req: Request) -> dict:
        """This month's estimated cloud spend (CL-01)."""
        return app.costs.summary()

    @router.route("PUT", "/api/providers/{id}")
    def provider_save(req: Request) -> dict:
        p = providers.get(req.params["id"])
        config = p.validate_config(req.json_object().get("config"))
        app.settings.update({"providers": {p.id: config}})
        return provider_info(p, config, app.model_catalog)

    @router.route("POST", "/api/providers/{id}/test")
    def provider_test(req: Request) -> dict:
        p = providers.get(req.params["id"])
        config = provider_config(app, p, req.json_object().get("config"))
        return p.test(provider_context(app, p, config))

    @router.route("POST", "/api/providers/{id}/models")
    def provider_models(req: Request) -> dict:
        """Fetch the vendor's current models; "models" answers for the config used (the draft when sent)."""
        p = providers.get(req.params["id"])
        given = req.json_object().get("config")
        config = provider_config(app, p, given)
        entry = app.model_catalog.put(p.id, p.catalog_source(config), p.detect_models(provider_context(app, p, config)))
        pinned = pin_newest(app, p, entry["fields"]) if given is None else {}
        stored = (app.settings.section("providers") or {}).get(p.id)
        return {"provider": provider_info(p, stored, app.model_catalog), "models": entry, "pinned": pinned}

    # ------------------------------------------------------------------ text model and notifications
    @router.route("POST", "/api/llm/test")
    def llm_test(req: Request) -> dict:
        return app.llm.test()

    @router.route("POST", "/api/llm/enhance")
    def llm_enhance(req: Request) -> dict:
        data = req.json_object()
        text = app.llm.enhance(str(data.get("text") or ""), str(data.get("kind") or "image"),
                               str(data.get("target") or "same"))
        return {"text": text}

    @router.route("POST", "/api/llm/condense")
    def llm_condense(req: Request) -> dict:
        return {"items": app.llm.condense_captions(req.json_object().get("items"))}

    @router.route("POST", "/api/notify/test")
    def notify_test(req: Request) -> dict:
        return app.notifier.test()
