"""Lazy Modal client. Importing/config-checking this module creates no resources."""
from spinread.remote.protocol import RemoteError


class ModalProvider:
    def __init__(self, app):
        self.app = app

    def submit(self, request, *, gpu):
        import modal
        function = modal.Function.from_name(self.app, "gpu" if gpu else "cpu")
        return function.spawn(request).object_id

    def poll(self, call_id):
        import modal
        try:
            return modal.FunctionCall.from_id(call_id).get(timeout=0)
        except TimeoutError:
            return None

    def cancel(self, call_id):
        import modal
        modal.FunctionCall.from_id(call_id).cancel(terminate_containers=True)


class RemotePending(RemoteError):
    """Keep the durable receipt; retry polling, never submit a second call."""


class LeaseLost(RemoteError):
    pass
