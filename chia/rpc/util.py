from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Coroutine, Dict, List

import aiohttp

from chia.util.json_util import obj_to_response
from chia.wallet.conditions import Condition, conditions_from_json_dicts

log = logging.getLogger(__name__)


def wrap_http_handler(f) -> Callable:
    async def inner(request) -> aiohttp.web.Response:
        request_data = await request.json()
        try:
            res_object = await f(request_data)
            if res_object is None:
                res_object = {}
            if "success" not in res_object:
                res_object["success"] = True
        except Exception as e:
            tb = traceback.format_exc()
            log.warning(f"Error while handling message: {tb}")
            if len(e.args) > 0:
                res_object = {"success": False, "error": f"{e.args[0]}"}
            else:
                res_object = {"success": False, "error": f"{e}"}

        return obj_to_response(res_object)

    return inner


def tx_endpoint(
    func: Callable[..., Coroutine[Any, Any, Dict[str, Any]]]
) -> Callable[..., Coroutine[Any, Any, Dict[str, Any]]]:
    async def rpc_endpoint(self, request: Dict[str, Any], *args, **kwargs) -> Dict[str, Any]:
        extra_conditions: List[Condition] = []
        if "extra_conditions" in request:
            extra_conditions = conditions_from_json_dicts(request["extra_conditions"])
        return await func(self, request, *args, extra_conditions=extra_conditions, **kwargs)

    return rpc_endpoint
