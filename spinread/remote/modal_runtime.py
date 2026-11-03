"""Importable function bodies baked into the image; no auto-uploaded source."""


def cpu(request):
    from spinread.remote.executor import invoke
    return invoke(request)


def gpu(request):
    from spinread.remote.executor import invoke
    from spinread.remote.model_manifest import verify
    try:
        verify(request["payload"]["model_revision"], "/models/manifest.json")
    except Exception:
        return {"status": "FAILED", "error_code": "MODEL_PREFLIGHT_FAILED"}
    return invoke(request)
